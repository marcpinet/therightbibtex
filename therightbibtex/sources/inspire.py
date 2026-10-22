"""INSPIRE-HEP : physique des hautes énergies (notices et clés TeX de référence)."""
from __future__ import annotations

from .. import bibtex
from ..textutil import clean_title, latex_to_text, norm_arxiv, norm_doi, parse_name, year_of

from .base import Ctx, Query, Record, WEIGHTS

NAME = "inspire"
API = "https://inspirehep.net/api/literature"
FIELDS = ("titles,authors.full_name,publication_info,dois,arxiv_eprints,control_number,document_type,"
          "imprints,preprint_date,texkeys,collaborations,citation_count")


def _parse(hit: dict, rank: int = 0) -> Record | None:
    m = hit.get("metadata") or {}
    titles = m.get("titles") or []
    title = clean_title(latex_to_text(titles[0].get("title", ""))) if titles else ""
    if not title:
        return None
    pubs = [p for p in m.get("publication_info") or [] if p.get("journal_title")]
    pub = pubs[0] if pubs else {}
    doi = norm_doi(((m.get("dois") or [{}])[0]).get("value", ""))
    arx = (m.get("arxiv_eprints") or [{}])[0]
    arxiv = norm_arxiv(arx.get("value", ""))
    dtype = (m.get("document_type") or [""])[0]
    kind = "journal" if pub else ("conference" if dtype == "conference paper" else
                                  "thesis" if dtype == "thesis" else "preprint" if arxiv else "other")
    authors = [parse_name(a.get("full_name", "")) for a in m.get("authors") or []]
    collab = [c.get("value") for c in m.get("collaborations") or [] if c.get("value")]
    ps, pe = pub.get("page_start", ""), pub.get("page_end", "")
    cn = m.get("control_number")
    rec = Record(
        source=NAME, title=title, authors=authors,
        year=pub.get("year") or year_of(m.get("preprint_date")) or year_of((m.get("imprints") or [{}])[0].get("date")),
        kind=kind, venue=pub.get("journal_title", "") if pub else ("arXiv" if arxiv else ""),
        volume=pub.get("journal_volume", ""), number=pub.get("journal_issue", ""),
        pages=f"{ps}--{pe}" if ps and pe else (ps or pub.get("artid", "")),
        doi=doi, arxiv=arxiv, arxiv_class=(arx.get("categories") or [""])[0],
        ids={"inspire": str(cn), **({"texkey": m["texkeys"][0]} if m.get("texkeys") else {})},
        url=f"https://inspirehep.net/literature/{cn}", citations=m.get("citation_count"),
        status="published" if pub else "preprint", link=f"https://inspirehep.net/literature/{cn}",
        weight=WEIGHTS[NAME], rank=rank,
    )
    if collab:
        rec.ids["collaboration"] = collab[0]
    return rec


async def search(ctx: Ctx, q: Query) -> list[Record]:
    title = q.title or (q.raw if q.kind == "title" else "")
    if not title or len(title) < 8:
        return []
    safe = title.replace('"', " ")
    data = await ctx.http.get_json(NAME, API, params={"q": f't "{safe}"', "size": 5, "fields": FIELDS},
                                   ttl=86400, refresh=ctx.refresh, timeout=12)
    return [r for i, h in enumerate((data.get("hits") or {}).get("hits", [])) if (r := _parse(h, i))]


async def fetch_bibtex(ctx: Ctx, control_number: str) -> str:
    r = await ctx.http.request(NAME, f"{API}/{control_number}", params={"format": "bibtex"},
                               ttl=86400 * 7, refresh=ctx.refresh, timeout=10)
    return bibtex.reformat(r.text) if r.text.strip().startswith("@") else ""
