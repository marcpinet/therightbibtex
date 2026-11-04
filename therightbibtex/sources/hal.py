"""HAL (archive ouverte française) : notices et BibTeX produits par HAL."""
from __future__ import annotations

from .. import bibtex
from ..textutil import Author, arxiv_from_doi, clean_title, norm_arxiv, norm_doi

from .base import Ctx, Query, Record, WEIGHTS

NAME = "hal"
API = "https://api.archives-ouvertes.fr/search/"
FL = ("halId_s,docType_s,title_s,subTitle_s,authFirstName_s,authLastName_s,producedDateY_i,"
      "publicationDateY_i,journalTitle_s,conferenceTitle_s,bookTitle_s,doiId_s,arxivId_s,volume_s,"
      "issue_s,page_s,publisher_s,label_bibtex,uri_s,proceedings_s,version_i")

_KIND = {"ART": "journal", "COMM": "conference", "POSTER": "conference", "OUV": "book",
         "DOUV": "book", "COUV": "chapter", "THESE": "thesis", "HDR": "thesis", "MEM": "thesis",
         "REPORT": "report", "UNDEFINED": "preprint", "PREPRINT": "preprint", "WORKINGPAPER": "preprint",
         "OTHER": "other", "LECTURE": "other", "SOFTWARE": "software"}


def _first(v):
    return (v[0] if v else "") if isinstance(v, list) else (v or "")


def _parse(d: dict, rank: int = 0) -> Record | None:
    title = clean_title(_first(d.get("title_s")))
    if not title:
        return None
    sub = _first(d.get("subTitle_s"))
    if sub and sub.lower() not in title.lower():
        title = f"{title}: {sub}"
    firsts, lasts = d.get("authFirstName_s") or [], d.get("authLastName_s") or []
    authors = [Author(firsts[i] if i < len(firsts) else "", lasts[i]) for i in range(len(lasts))]
    kind = _KIND.get(d.get("docType_s", ""), "other")
    venue = d.get("journalTitle_s") or d.get("conferenceTitle_s") or _first(d.get("bookTitle_s")) or ""
    doi = norm_doi(d.get("doiId_s", ""))
    arxiv = norm_arxiv(d.get("arxivId_s", "")) or arxiv_from_doi(doi)
    rec = Record(
        source=NAME, title=title, authors=authors,
        year=d.get("publicationDateY_i") or d.get("producedDateY_i"), kind=kind,
        venue="HAL" if kind == "preprint" else venue, volume=_first(d.get("volume_s")),
        number=_first(d.get("issue_s")), pages=_first(d.get("page_s")),
        publisher=_first(d.get("publisher_s")), doi=doi, arxiv=arxiv,
        ids={"hal": d.get("halId_s", "")}, url=d.get("uri_s", ""),
        status="preprint" if kind == "preprint" else "published",
        link=d.get("uri_s", ""), weight=WEIGHTS[NAME], rank=rank,
    )
    if d.get("version_i"):
        rec.ids["hal_version"] = f"v{d['version_i']}"
    if d.get("label_bibtex"):
        rec.bibtex = bibtex.reformat(d["label_bibtex"])
        rec.bibtex_label = "HAL"
    return rec


async def _query(ctx: Ctx, q: str, rows: int = 5) -> list[Record]:
    data = await ctx.http.get_json(NAME, API, params={"q": q, "wt": "json", "rows": rows, "fl": FL},
                                   ttl=86400, refresh=ctx.refresh, timeout=12)
    return [r for i, d in enumerate((data.get("response") or {}).get("docs", [])) if (r := _parse(d, i))]


async def search(ctx: Ctx, q: Query) -> list[Record]:
    title = q.title or (q.raw if q.kind == "title" else "")
    if not title or len(title) < 8:
        return []
    safe = title.replace('"', " ").replace("\\", " ")
    return await _query(ctx, f'title_t:"{safe}"')


async def lookup(ctx: Ctx, doi: str = "", hal: str = "") -> list[Record]:
    if hal:
        return await _query(ctx, f"halId_s:{hal}", 1)
    if doi:
        return await _query(ctx, f'doiId_s:"{doi}"', 2)
    return []
