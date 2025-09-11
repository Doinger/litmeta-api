import os
from typing import List, Dict, Any
from fastapi import Request, FastAPI, Query, Body
from fastapi.middleware.cors import CORSMiddleware
import httpx
import xmltodict
from datetime import datetime
from PyPDF2 import PdfReader
import base64, io, re

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

def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()
    
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

@app.post("/validate-quotes")
def validate_quotes(payload: dict = Body(...)):
    """
    校验每段的 source_quote 是否能在指定 source_page 找到
    """
    pdf_b64 = payload.get("pdf_b64")
    page_texts = payload.get("page_texts")
    if not page_texts:
        assert pdf_b64, "必须提供 pdf_b64 或 page_texts"
        pdf_bytes = base64.b64decode(pdf_b64)
        reader = PdfReader(io.BytesIO(pdf_bytes))
        page_texts = [(p.extract_text() or "") for p in reader.pages]

    checked, matched, mismatches = 0, 0, []
    for item in payload.get("paragraphs", []):
        checked += 1
        q = norm(item.get("source_quote", ""))
        pg = int(item.get("source_page", 1)) - 1
        if pg < 0 or pg >= len(page_texts) or not q:
            mismatches.append({**item, "reason": "invalid_page_or_empty_quote"})
            continue
        hay = norm(page_texts[pg])
        if q and q in hay:
            matched += 1
        else:
            mismatches.append({**item, "reason": "quote_not_found_on_page"})
    return {"checked": checked, "matched": matched, "mismatches": mismatches}
