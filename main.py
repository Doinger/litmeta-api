import os
from typing import List, Dict, Any
from fastapi import Request, FastAPI, Query, Body
from fastapi.middleware.cors import CORSMiddleware
import httpx
import xmltodict
from datetime import datetime
from PyPDF2 import PdfReader
import base64, io, re
from fastapi.responses import JSONResponse

APP_NAME = "LitMeta"
VERSION = "1.0"

NCBI_EMAIL = os.getenv("NCBI_EMAIL", "you@example.com")  # 改成你的邮箱
CROSSREF_MAILTO = os.getenv("CROSSREF_MAILTO", "you@example.com")  # 改成你的邮箱
HTTP_TIMEOUT = float(os.getenv("HTTP_TIMEOUT", "12.0"))

app = FastAPI(title=APP_NAME, version=VERSION)

# 允许被 ChatGPT 调用（简单起见放开 CORS）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

try:
    import httpx
    _HTTPX_OK = True
except Exception:
    _HTTPX_OK = False

try:
    from PyPDF2 import PdfReader
    _PYPDF2_OK = True
except Exception:
    _PYPDF2_OK = False

MAX_B64_BYTES = 15 * 1024 * 1024   # 15MB 上限，避免 Render/网关超限
MAX_PARAS_PER_CALL = 100           # 单次最多校验多少段，超出请分批
TIMEOUT_SEC = 60

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()

def pdf_to_base64(pdf_path: str) -> str:
    """将 PDF 文件转为 Base64 字符串"""
    with open(pdf_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")
    
def make_placeholder_batch():
    """返回符合 AcademicBatchV1_1 的极小占位 JSON。"""
    return {
        "batch_id": datetime.utcnow().strftime("%Y%m%d-%H%M%S"),
        "papers": [
            {
                "paper_id": "P1",
                "filename": "",
                "meta": {"title": "", "year": "", "journal": "", "doi": "", "pmid": ""},
                "sections": [
                    {
                        "section_title": "Introduction",
                        "paragraphs": [
                            {
                                "para_index": 1,
                                "text_preview": "占位预览（≤200字）。",
                                "key_points": ["提出研究空白", "界定目标人群"],
                                "evidence": [],
                                "claims_strength": "medium",
                                "limitations_flags": []
                            }
                        ]
                    }
                ],
                "segmentation_audit": {
                    "sections_detected": 1,
                    "paragraphs_detected": 1,
                    "paragraphs_reported": 1
                }
            }
        ]
    }

# --- Actions 专用小端点：永远返回“很小”的占位 JSON ---
@app.post("/action-analyze")
async def action_analyze(_: Request):
    return make_placeholder_batch()

# （可选）如果你一定要沿用 /analyze 路径，可以加一个轻量开关：
@app.post("/analyze")
async def analyze(request: Request):
    # 轻量模式：当带上 ?mode=anchor 或头 X-Action-Anchor: 1 时，返回占位
    qp = dict(request.query_params)
    if qp.get("mode") == "anchor" or request.headers.get("X-Action-Anchor") == "1":
        return make_placeholder_batch()

    # 否则走你原有的“重逻辑”（如已存在则调用；没有就先返回占位）
    try:
        body = await request.json()
        # TODO: 这里接入你现有的 heavy analyze 逻辑
        # result = await heavy_analyze(body)
        # return result
    except Exception:
        pass
    return make_placeholder_batch()

@app.get("/health")
async def health():
    return {"status": "ok", "app": APP_NAME, "version": VERSION}

@app.get("/pubmed/search")
async def pubmed_search(query: str = Query(..., description="PubMed 查询语句"),
                        retmax: int = Query(10, ge=1, le=50)):
    """
    通过 NCBI E-utilities 搜索 PubMed，并返回简化元数据（title/authors_short/year/journal/doi_or_pmid/url）
    """
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
        es = await client.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
            params={
                "db": "pubmed", "term": query, "retmode": "json", "retmax": retmax,
                "tool": "litmeta", "email": NCBI_EMAIL
            },
        )
        es.raise_for_status()
        idlist: List[str] = es.json().get("esearchresult", {}).get("idlist", [])
        if not idlist:
            return {"results": []}

        ef = await client.get(
            "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
            params={
                "db": "pubmed", "id": ",".join(idlist), "retmode": "xml",
                "tool": "litmeta", "email": NCBI_EMAIL
            },
        )
        ef.raise_for_status()
        doc = xmltodict.parse(ef.text)
        arts = doc.get("PubmedArticleSet", {}).get("PubmedArticle", [])
        if isinstance(arts, dict):
            arts = [arts]

        results: List[Dict[str, Any]] = []
        for a in arts:
            med = a.get("MedlineCitation", {}).get("Article", {})
            title = med.get("ArticleTitle", "")
            journal = med.get("Journal", {}).get("Title", "")
            pubdate = med.get("Journal", {}).get("JournalIssue", {}).get("PubDate", {})
            year = None
            for k in ["Year", "MedlineDate"]:
                y = pubdate.get(k)
                if y:
                    ys = str(y)[:4]
                    year = int(ys) if ys.isdigit() else None
                    break

            alist = med.get("AuthorList", {}).get("Author", [])
            if isinstance(alist, dict):
                alist = [alist]
            authors_short = ""
            if alist:
                first = alist[0]
                last = first.get("LastName") or first.get("CollectiveName") or ""
                initials = first.get("Initials") or ""
                authors_short = f"{last} {initials} et al." if last else ""

            article_ids = a.get("PubmedData", {}).get("ArticleIdList", {}).get("ArticleId", [])
            if isinstance(article_ids, dict):
                article_ids = [article_ids]
            doi, pmid = "", ""
            for idobj in article_ids:
                idtype = idobj.get("@IdType")
                val = idobj.get("#text", "")
                if idtype == "doi":
                    doi = val
                if idtype == "pubmed":
                    pmid = val
            url = f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/" if pmid else ""
            results.append({
                "title": title,
                "authors_short": authors_short,
                "year": year or 0,
                "journal": journal,
                "doi_or_pmid": doi or pmid,
                "url": url
            })
        return {"results": results}

@app.get("/crossref/by-title")
async def crossref_by_title(title: str = Query(..., description="论文标题")):
    """
    使用 Crossref REST API 通过标题查 DOI 等元数据
    """
    headers = {"User-Agent": f"litmeta/1.0 (mailto:{CROSSREF_MAILTO})"}
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers=headers) as client:
        r = await client.get(
            "https://api.crossref.org/works",
            params={"query.title": title, "rows": 1, "select": "DOI,title,container-title,author,issued,URL"}
        )
        r.raise_for_status()
        items = r.json().get("message", {}).get("items", [])
        if not items:
            return {}
        it = items[0]
        doi = it.get("DOI", "")
        journal = (it.get("container-title") or [""])[0]
        issued = (it.get("issued") or {}).get("date-parts") or []
        year = issued[0][0] if issued and issued[0] else 0
        auth = it.get("author") or []
        authors_short = ""
        if auth:
            first = auth[0]
            family = first.get("family", "")
            given = first.get("given", "")
            initials = "".join([p[0] for p in given.split()]) if given else ""
            authors_short = f"{family} {initials} et al." if family else ""
        return {
            "doi": doi,
            "journal": journal,
            "year": year,
            "authors_short": authors_short,
            "url": it.get("URL", "")
        }

@app.post("/upload-and-validate")
async def upload_and_validate(request: Request):
    """
    用户上传 PDF 文件 + 段落信息，自动转成 pdf_b64，再调用 validate-quotes
    """
    form = await request.form()
    pdf_file = form["file"]
    pdf_bytes = await pdf_file.read()
    pdf_b64 = base64.b64encode(pdf_bytes).decode("utf-8")

    # 获取段落数据（前端上传时应一并传）
    paragraphs = form.get("paragraphs")
    if isinstance(paragraphs, str):
        import json
        paragraphs = json.loads(paragraphs)

    # 调用 validate_quotes 内部逻辑
    return validate_quotes({"pdf_b64": pdf_b64, "paragraphs": paragraphs})

@app.post("/validate-quotes")
def validate_quotes(payload: dict = Body(...)):
    """
    校验 paragraphs[*].source_quote 是否真实出现在指定的 source_page。
    入参优先级：page_texts > pdf_b64 > pdf_url
    返回始终为 200，携带 status 与 errors，避免 500 影响调用链。
    """
    try:
        # -------- 参数预检 --------
        if not isinstance(payload, dict):
            return JSONResponse({"status": "error", "errors": ["invalid_json"]}, status_code=200)

        paragraphs = payload.get("paragraphs") or []
        if not isinstance(paragraphs, list) or len(paragraphs) == 0:
            return JSONResponse({"status": "error", "errors": ["paragraphs_required"]}, status_code=200)

        if len(paragraphs) > MAX_PARAS_PER_CALL:
            return JSONResponse({
                "status": "error",
                "errors": [f"too_many_paragraphs:{len(paragraphs)} > {MAX_PARAS_PER_CALL}"],
                "advice": f"请分批调用，每次不超过 {MAX_PARAS_PER_CALL} 段"
            }, status_code=200)

        page_texts = payload.get("page_texts")
        pdf_b64 = payload.get("pdf_b64")
        pdf_url = payload.get("pdf_url")

        # -------- 获取 page_texts --------
        if isinstance(page_texts, list) and page_texts:
            pass  # 已有每页文本
        elif pdf_b64:
            if not _PYPDF2_OK:
                return JSONResponse({"status": "error", "errors": ["PyPDF2_not_installed"]}, status_code=200)
            try:
                raw = base64.b64decode(pdf_b64, validate=True)
            except Exception as e:
                return JSONResponse({"status": "error", "errors": [f"b64_decode_failed:{e}"]}, status_code=200)
            if len(raw) > MAX_B64_BYTES:
                return JSONResponse({"status": "error", "errors": ["pdf_too_large"], "limit_bytes": MAX_B64_BYTES}, status_code=200)
            try:
                reader = PdfReader(io.BytesIO(raw))
                page_texts = [(p.extract_text() or "") for p in reader.pages]
            except Exception as e:
                return JSONResponse({"status": "error", "errors": [f"pdf_extract_failed:{e}"]}, status_code=200)
        elif pdf_url:
            if not _HTTPX_OK:
                return JSONResponse({"status": "error", "errors": ["httpx_not_installed"]}, status_code=200)
            if not str(pdf_url).lower().startswith("https://"):
                return JSONResponse({"status": "error", "errors": ["pdf_url_must_be_https"]}, status_code=200)
            try:
                with httpx.Client(timeout=TIMEOUT_SEC, follow_redirects=True) as cli:
                    r = cli.get(pdf_url)
                if r.status_code != 200 or "pdf" not in r.headers.get("content-type","").lower():
                    return JSONResponse({"status": "error", "errors": ["download_failed_or_not_pdf"]}, status_code=200)
                if len(r.content) > MAX_B64_BYTES:
                    return JSONResponse({"status": "error", "errors": ["pdf_too_large"], "limit_bytes": MAX_B64_BYTES}, status_code=200)
                if not _PYPDF2_OK:
                    return JSONResponse({"status": "error", "errors": ["PyPDF2_not_installed"]}, status_code=200)
                reader = PdfReader(io.BytesIO(r.content))
                page_texts = [(p.extract_text() or "") for p in reader.pages]
            except Exception as e:
                return JSONResponse({"status": "error", "errors": [f"download_or_extract_failed:{e}"]}, status_code=200)
        else:
            return JSONResponse({"status": "error", "errors": ["pdf_b64_or_page_texts_or_pdf_url_required"]}, status_code=200)

        if not page_texts or not isinstance(page_texts, list):
            return JSONResponse({"status": "error", "errors": ["empty_page_texts"]}, status_code=200)

        # -------- 逐段匹配 --------
        checked, matched = 0, 0
        mismatches = []
        for item in paragraphs:
            checked += 1
            q = norm(item.get("source_quote"))
            try:
                pg = int(item.get("source_page", 1)) - 1
            except Exception:
                pg = -1

            if pg < 0 or pg >= len(page_texts) or not q:
                mismatches.append({**item, "reason": "invalid_page_or_empty_quote"})
                continue

            hay = norm(page_texts[pg])
            if q and q in hay:
                matched += 1
            else:
                mismatches.append({**item, "reason": "quote_not_found_on_page"})

        return JSONResponse({
            "status": "ok",
            "checked": checked,
            "matched": matched,
            "mismatches": mismatches,
            "pages": len(page_texts)
        }, status_code=200)

    except Exception as e:
        # 兜底：永不 500，把异常变成结构化错误返回
        return JSONResponse({"status": "error", "errors": [f"unexpected:{type(e).__name__}:{e}"]}, status_code=200)
