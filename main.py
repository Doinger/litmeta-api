import os
from typing import List, Dict, Any
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx
import xmltodict

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
