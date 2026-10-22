"""Europe PMC : PubMed, PMC et prépublications biomédicales (bioRxiv, medRxiv...)."""
from __future__ import annotations

from ..textutil import Author, clean_markup, clean_title, norm_doi, year_of

from .base import Ctx, Query, Record, WEIGHTS

NAME = "europepmc"
API = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"


def _parse(r: dict, rank: int = 0) -> Record | None:
    title = clean_title(r.get("title", ""))
    if not title:
        return None
    authors = []
    for a in (r.get("authorList") or {}).get("author", []):
        if a.get("collectiveName"):
            authors.append(Author("", clean_markup(a["collectiveName"]), corporate=True))
        elif a.get("lastName"):
            authors.append(Author(clean_markup(a.get("firstName") or a.get("initials") or ""),
                                  clean_markup(a["lastName"])))
    ji = r.get("journalInfo") or {}
    journal = (ji.get("journal") or {})
    src = r.get("source", "")
    preprint = src == "PPR"
    book = r.get("bookOrReportDetails") or {}
    pub_types = [t.lower() for t in (r.get("pubTypeList") or {}).get("pubType", [])]
    retracted = any("retracted publication" in t for t in pub_types)
    venue = journal.get("title") or ""
    if preprint:
        venue = (book.get("publisher") if isinstance(book, dict) else "") or "Preprint"
    rec = Record(
        source=NAME, title=title, authors=authors,
        year=ji.get("yearOfPublication") or year_of(r.get("pubYear")),
        month=ji.get("monthOfPublication") or None,
        date=r.get("firstPublicationDate", ""),
        kind="preprint" if preprint else ("book" if book and not venue else "journal"),
        venue=venue, venue_short=journal.get("isoabbreviation") or journal.get("medlineAbbreviation") or "",
        volume=ji.get("volume") or "", number=ji.get("issue") or "", pages=r.get("pageInfo") or "",
        issn=journal.get("issn") or journal.get("essn") or "", doi=norm_doi(r.get("doi", "")),
        ids={k: r[k] for k in ("pmid", "pmcid") if r.get(k)},
        abstract=clean_markup(r.get("abstractText", "")), citations=r.get("citedByCount"),
        status="preprint" if preprint else "published", retracted=retracted,
        link=f"https://europepmc.org/article/{src}/{r.get('id')}" if r.get("id") else "",
        weight=WEIGHTS[NAME], rank=rank,
    )
    if retracted:
        rec.notices.append({"type": "retraction", "label": "Retraction", "doi": "", "date": "",
                            "source": "Europe PMC"})
    for cc in (r.get("commentCorrectionList") or {}).get("commentCorrection", []):
        if (cc.get("type") or "").lower().startswith("retraction in"):
            rec.retracted = True
            rec.notices.append({"type": "retraction", "label": "Retraction", "doi": "",
                                "date": "", "source": "Europe PMC", "ref": cc.get("reference", "")})
    return rec


async def _query(ctx: Ctx, query: str, size: int = 6) -> list[Record]:
    data = await ctx.http.get_json(
        NAME, API, params={"query": query, "format": "json", "resultType": "core", "pageSize": size},
        ttl=86400, refresh=ctx.refresh, timeout=12,
    )
    return [rec for i, r in enumerate((data.get("resultList") or {}).get("result", []))
            if (rec := _parse(r, i))]


async def search(ctx: Ctx, q: Query) -> list[Record]:
    if q.title:
        safe = q.title.replace('"', " ")
        return await _query(ctx, f'TITLE:"{safe}"')
    text = (q.text or q.raw).replace('"', " ")[:300]
    return await _query(ctx, text) if len(text) > 8 else []


async def lookup(ctx: Ctx, doi: str = "", pmid: str = "") -> list[Record]:
    if doi:
        return await _query(ctx, f'DOI:"{doi}"', 2)
    if pmid:
        return await _query(ctx, f"EXT_ID:{pmid} AND SRC:MED", 1)
    return []
