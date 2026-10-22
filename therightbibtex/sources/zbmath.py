"""zbMATH Open : mathématiques et statistiques (revues indexées depuis 1868)."""
from __future__ import annotations

from ..textutil import clean_title, latex_to_text, norm_arxiv, norm_doi, parse_name, year_of

from .base import Ctx, Query, Record, WEIGHTS
from .http import SourceError

NAME = "zbmath"
API = "https://api.zbmath.org/v1/document/_search"
_KIND = {"j": "journal", "a": "chapter", "b": "book", "p": "preprint"}


def _parse(r: dict, rank: int = 0) -> Record | None:
    t = r.get("title") or {}
    title = clean_title(latex_to_text(t.get("title") or ""))
    if not title:
        return None
    if t.get("subtitle"):
        title = f"{title}: {latex_to_text(t['subtitle'])}"
    src = r.get("source") or {}
    series = (src.get("series") or [{}])[0] if src.get("series") else {}
    doi = arxiv = ""
    for link in r.get("links") or []:
        if link.get("type") == "doi" and not doi:
            doi = norm_doi(link.get("identifier", ""))
        elif link.get("type") == "arxiv" and not arxiv:
            arxiv = norm_arxiv(link.get("identifier", ""))
    code = (r.get("document_type") or {}).get("code", "")
    kind = _KIND.get(code, "other")
    zid = r.get("identifier") or str(r.get("id", ""))
    return Record(
        source=NAME, title=title,
        authors=[parse_name(a.get("name", "")) for a in (r.get("contributors") or {}).get("authors", [])],
        year=year_of(r.get("year")), kind=kind, venue=series.get("title", ""),
        venue_short=series.get("short_title") or "", volume=series.get("volume") or "",
        number=series.get("issue") or "", pages=src.get("pages") or "",
        publisher=series.get("publisher") or "", doi=doi, arxiv=arxiv, ids={"zbmath": zid},
        url=f"https://zbmath.org/?q=an:{zid}", status="preprint" if kind == "preprint" else "published",
        link=f"https://zbmath.org/?q=an:{zid}", weight=WEIGHTS[NAME], rank=rank,
    )


async def search(ctx: Ctx, q: Query) -> list[Record]:
    title = q.title or (q.raw if q.kind == "title" else "")
    if not title or len(title) < 10:
        return []
    safe = title.replace('"', " ")
    try:
        data = await ctx.http.get_json(NAME, API, params={"search_string": f'ti:"{safe}"', "results_per_page": 4},
                                       ttl=86400, refresh=ctx.refresh, timeout=10)
    except SourceError as e:
        if e.status == 404:  # zbMATH répond 404 quand il n'y a aucun résultat
            return []
        raise
    return [rec for i, r in enumerate(data.get("result") or []) if (rec := _parse(r, i))]
