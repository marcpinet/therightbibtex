"""DataCite : DOI des dépôts (arXiv, Zenodo, figshare, HAL...)."""
from __future__ import annotations

from ..textutil import (
    TITLE_STOPWORDS, Author, arxiv_from_doi, clean_markup, clean_title, norm_doi, norm_title, parse_name, year_of,
)

from .base import Ctx, Query, Record, WEIGHTS
from .http import SourceError

NAME = "datacite"
API = "https://api.datacite.org/dois"

_KIND = {"JournalArticle": "journal", "ConferencePaper": "conference", "ConferenceProceeding": "book",
         "BookChapter": "chapter", "Book": "book", "Dissertation": "thesis", "Report": "report",
         "Preprint": "preprint", "Dataset": "dataset", "Software": "software", "Text": "other"}


def _parse(item: dict, rank: int = 0) -> Record | None:
    a = item.get("attributes") or {}
    titles = a.get("titles") or []
    title = clean_title(titles[0].get("title", "")) if titles else ""
    if not title:
        return None
    doi = norm_doi(a.get("doi") or item.get("id", ""))
    authors = []
    for c in a.get("creators") or []:
        if c.get("nameType") == "Organizational":
            authors.append(Author("", clean_markup(c.get("name", "")), corporate=True))
        elif c.get("familyName"):
            authors.append(Author(clean_markup(c.get("givenName", "")), clean_markup(c["familyName"])))
        elif c.get("name"):
            authors.append(parse_name(c["name"]))
    types = a.get("types") or {}
    general = types.get("resourceTypeGeneral", "")
    arxiv = arxiv_from_doi(doi)
    kind = "preprint" if arxiv else _KIND.get(general, "other")
    publisher = a.get("publisher")
    if isinstance(publisher, dict):
        publisher = publisher.get("name", "")
    container = (a.get("container") or {}).get("title", "")
    rec = Record(
        source=NAME, title=title, authors=authors, year=year_of(a.get("publicationYear")),
        kind=kind, venue="arXiv" if arxiv else (container or publisher or ""),
        publisher=publisher or "", doi=doi, arxiv=arxiv, url=a.get("url", ""),
        status="preprint" if kind == "preprint" else "published",
        link=f"https://commons.datacite.org/doi.org/{doi}", weight=WEIGHTS[NAME], rank=rank,
    )
    for d in a.get("descriptions") or []:
        if d.get("descriptionType") == "Abstract":
            rec.abstract = clean_markup(d.get("description", ""))
            break
    for rel in a.get("relatedIdentifiers") or []:
        rtype = rel.get("relationType", "")
        if rel.get("relatedIdentifierType") == "DOI" and rtype in (
                "IsVersionOf", "HasVersion", "IsIdenticalTo", "IsPreviousVersionOf", "IsNewVersionOf",
                "IsVariantFormOf", "IsOriginalFormOf", "IsPublishedIn"):
            rel_doi = norm_doi(rel.get("relatedIdentifier", ""))
            if rel_doi:
                rec.related.append(("doi", rel_doi, rtype))
    return rec


async def search(ctx: Ctx, q: Query) -> list[Record]:
    title = q.title or (q.raw if q.kind == "title" else "")
    if not title or len(title) < 8:
        return []
    safe = title.replace('"', " ").replace("\\", " ")
    data = await ctx.http.get_json(
        NAME, API, params={"query": f'titles.title:"{safe}"', "page[size]": 8},
        ttl=86400, refresh=ctx.refresh, timeout=12,
    )
    recs = [r for i, it in enumerate(data.get("data", [])) if (r := _parse(it, i))]
    words = [w for w in norm_title(title).split() if len(w) >= 3 and w not in TITLE_STOPWORDS]
    if recs or not 2 <= len(words) <= 8:
        return recs
    # Saisie en mots-clés : chaque mot est dans le titre ou est un nom d'auteur
    # (« dosovitskiy image worth words ») ; ~1 tolère un pluriel ou une faute.
    clauses = [f"(titles.title:{w}{'~1' if len(w) >= 6 else ''} OR creators.familyName:{w} OR creators.name:{w})"
               for w in words]
    data = await ctx.http.get_json(
        NAME, API, params={"query": " AND ".join(clauses), "page[size]": 8},
        ttl=86400, refresh=ctx.refresh, timeout=12,
    )
    return [r for i, it in enumerate(data.get("data", [])) if (r := _parse(it, i))]


async def fuzzy_titles(ctx: Ctx, words: list[str]) -> list[str]:
    """Titres proches d'une requête mal orthographiée (recherche floue
    Elasticsearch de DataCite, qui indexe notamment tous les DOI arXiv)."""
    terms = []
    for w in words[:10]:
        w = "".join(c for c in w if c.isalnum())
        if len(w) >= 4:
            terms.append(f"{w}~{1 if len(w) <= 6 else 2}")
        elif len(w) >= 3:
            terms.append(f"{w}~1")   # « ned » pour « need »
    if len(terms) < 2:
        return []
    data = await ctx.http.get_json(
        NAME, API, params={"query": f"titles.title:({' AND '.join(terms)})", "page[size]": 25},
        ttl=86400 * 3, refresh=ctx.refresh, timeout=10,
    )
    out = []
    for it in data.get("data", []):
        for t in (it.get("attributes") or {}).get("titles") or []:
            if t.get("title"):
                out.append(t["title"])
    return out


async def lookup(ctx: Ctx, doi: str) -> Record | None:
    try:
        data = await ctx.http.get_json(NAME, f"{API}/{doi}", ttl=86400 * 7, refresh=ctx.refresh,
                                       neg_ttl=86400, timeout=10)
    except SourceError as e:
        if e.status == 404:
            return None
        raise
    return _parse(data.get("data") or {})
