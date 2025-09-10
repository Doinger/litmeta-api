import os
from typing import List, Dict, Any
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
import httpx
import xmltodict
from difflib import SequenceMatcher
import re

APP_NAME = "LitMeta"
VERSION = "1.0"

NCBI_EMAIL = os.getenv("NCBI_EMAIL", "zyp_cau@163.com")  # 改成你的邮箱
CROSSREF_MAILTO = os.getenv("CROSSREF_MAILTO", "zyp_cau@163.com")  # 改成你的邮箱
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
    # === DOI 解析：通过 doi.org 获取文献元数据 ===
@app.get("/doi/resolve")
async def doi_resolve(doi: str):
    doi = doi.strip()
    if not re.match(r"^10\.\d{4,9}/\S+$", doi):
        return {"ok": False, "error": "invalid_doi_format"}

    headers = {"Accept": "application/vnd.citationstyles.csl+json"}
    async with httpx.AsyncClient(timeout=20, headers=headers) as client:
        r = await client.get(f"https://doi.org/{doi}")
        if r.status_code != 200:
            return {"ok": False, "status": r.status_code}

        meta = r.json()
        title = meta.get("title")
        if isinstance(title, list):
            title = title[0] if title else ""
        journal = meta.get("container-title")
        if isinstance(journal, list):
            journal = journal[0] if journal else ""
        issued = meta.get("issued", {}).get("date-parts", [])
        year = issued[0][0] if issued and issued[0] else None
        authors = meta.get("author") or []
        first_author = (authors[0].get("family") if authors else "")

        return {
            "ok": True,
            "doi": doi,
            "title": title or "",
            "journal": journal or "",
            "year": year or 0,
            "first_author": first_author or "",
            "source_url": f"https://doi.org/{doi}"
        }
# === 引文核验：交叉比对 Crossref / PubMed / DOI.org ===
@app.get("/cite/verify")
async def cite_verify(title: str, year: int = 0, first_author: str = "", journal: str = "", doi: str = ""):
    from difflib import SequenceMatcher
    import httpx
    out = {"inputs": {"title": title, "year": year, "first_author": first_author, "journal": journal, "doi": doi}}
    votes, sources = [], []

    # 1) Crossref 查询
    try:
        headers = {"User-Agent": "litmeta/1.0 (mailto:your_email@example.com)"}
        async with httpx.AsyncClient(timeout=20, headers=headers) as client:
            r = await client.get("https://api.crossref.org/works",
                                 params={"query.title": title, "rows": 1})
            if r.status_code == 200 and r.json().get("message", {}).get("items"):
                it = r.json()["message"]["items"][0]
                cr_title = (it.get("title") or [""])[0]
                cr_jour  = (it.get("container-title") or [""])[0]
                cr_year  = (it.get("issued", {}).get("date-parts") or [[0]])[0][0]
                sim = SequenceMatcher(None, title.lower(), cr_title.lower()).ratio()
                match = (sim >= 0.9) and ((year==0) or (abs(cr_year - year) <= 1))
                votes.append(("crossref", match))
                sources.append({"via":"crossref","title":cr_title,"journal":cr_jour,"year":cr_year,"doi":it.get("DOI",""),"url":it.get("URL",""),"similarity":sim})
    except Exception as e:
        sources.append({"via":"crossref","error":str(e)})

    # 2) PubMed 查询
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            es = await client.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi",
                                  params={"db":"pubmed","term":f"{title}[ti]","retmode":"json","retmax":1})
            pmids = es.json().get("esearchresult", {}).get("idlist", [])
            if pmids:
                pmid = pmids[0]
                ef = await client.get("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi",
                                      params={"db":"pubmed","id":pmid,"retmode":"xml"})
                import xmltodict
                art = xmltodict.parse(ef.text)["PubmedArticleSet"]["PubmedArticle"]
                med = art["MedlineCitation"]["Article"]
                pm_title = med.get("ArticleTitle","")
                pm_jour  = med.get("Journal",{}).get("Title","")
                y = med.get("Journal",{}).get("JournalIssue",{}).get("PubDate",{}).get("Year") or "0"
                pm_year = int(str(y)[:4]) if str(y)[:4].isdigit() else 0
                sim = SequenceMatcher(None, title.lower(), pm_title.lower()).ratio()
                match = (sim >= 0.9) and ((year==0) or (abs(pm_year - year) <= 1))
                votes.append(("pubmed", match))
                sources.append({"via":"pubmed","title":pm_title,"journal":pm_jour,"year":pm_year,"pmid":pmid,"url":f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/","similarity":sim})
    except Exception as e:
        sources.append({"via":"pubmed","error":str(e)})

    # 3) DOI.org 直解（如提供了 DOI）
    if doi:
        try:
            dr = await doi_resolve(doi)
            if isinstance(dr, dict) and dr.get("ok"):
                sim = SequenceMatcher(None, title.lower(), (dr["title"] or "").lower()).ratio()
                match = (sim >= 0.9) and ((year==0) or (abs((dr["year"] or 0) - year) <= 1))
                votes.append(("doi.org", match))
                sources.append({"via":"doi.org","title":dr["title"],"journal":dr["journal"],"year":dr["year"],"doi":doi,"url":dr["source_url"],"similarity":sim})
        except Exception as e:
            sources.append({"via":"doi.org","error":str(e)})

    ok_votes = sum(1 for _,m in votes if m)
    status = "verified" if ok_votes >= 1 else ("mismatch" if votes else "unverified")
    return {"status": status, "votes": votes, "sources": sources}


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
