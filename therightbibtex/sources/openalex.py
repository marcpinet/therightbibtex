"""OpenAlex : index ouvert (ex-Microsoft Academic), une notice par version.
Sans clé, un petit budget quotidien partagé par toute l'adresse IP (épuisé, tout
est refusé jusqu'à minuit UTC) ; chaque clé (gratuite) a le sien."""
from __future__ import annotations

import hashlib
import logging

from ..textutil import arxiv_from_doi, clean_title, is_short_name, main_title_match, norm_arxiv, norm_doi, parse_name

from .base import Ctx, Query, Record, WEIGHTS
from .http import CONTACT, SourceError

logger = logging.getLogger(__name__)

NAME = "openalex"
API = "https://api.openalex.org/works"
SELECT = ("id,doi,display_name,publication_year,publication_date,type,authorships,primary_location,"
          "locations,biblio,ids,is_retracted,cited_by_count")

_KIND = {"article": "journal", "book-chapter": "chapter", "book": "book", "dissertation": "thesis",
         "preprint": "preprint", "report": "report", "dataset": "dataset", "review": "journal",
         "letter": "journal", "editorial": "journal", "standard": "report", "other": "other"}


def _params(extra: dict) -> dict:
    p = dict(extra)
    if CONTACT:
        p["mailto"] = CONTACT
    return p


_rejected: set[str] = set()   # clés refusées (401/403) depuis le démarrage


async def get(ctx: Ctx, url: str, params: dict, **kw) -> dict:
    """Chaque clé configurée, dans l'ordre (une clé refusée est écartée, une clé au
    budget épuisé passe la main), puis le budget partagé de l'adresse IP."""
    for key in [k for k in ctx.openalex_keys if k not in _rejected]:
        try:
            return await ctx.http.get_json(NAME, url, params={**params, "api_key": key},
                                           pace_key="openalex:" + hashlib.sha1(key.encode()).hexdigest()[:10], **kw)
        except SourceError as e:
            if e.status in (401, 403):
                _rejected.add(key)
                logger.warning("Clé OpenAlex …%s refusée (%s) : clé suivante", key[-4:], e.status)
            elif not (e.status == 429 or e.kind in ("ratelimited", "unavailable")):
                raise
    return await ctx.http.get_json(NAME, url, params=params, **kw)


def _parse(w: dict, rank: int = 0) -> Record | None:
    title = clean_title(w.get("display_name") or w.get("title") or "")
    if not title:
        return None
    doi = norm_doi(w.get("doi") or "")
    prim = w.get("primary_location") or {}
    src = prim.get("source") or {}
    stype = src.get("type", "")
    arxiv = arxiv_from_doi(doi)
    for loc in w.get("locations") or []:
        url = loc.get("landing_page_url") or ""
        if "arxiv.org/abs/" in url and not arxiv:
            arxiv = norm_arxiv(url)
    wtype = w.get("type", "")
    kind = _KIND.get(wtype, "other")
    if stype == "conference" and kind in ("journal", "other"):
        kind = "conference"
    # Un entrepôt (HAL, Lirias...) héberge des prépublications, mais aussi des jeux de données.
    if wtype == "preprint" or (stype == "repository" and kind not in ("dataset", "software")) or doi.startswith("10.48550/"):
        kind = "preprint"
    biblio = w.get("biblio") or {}
    fp, lp = biblio.get("first_page") or "", biblio.get("last_page") or ""
    ids = {"openalex": (w.get("id") or "").rsplit("/", 1)[-1]}
    wids = w.get("ids") or {}
    if wids.get("pmid"):
        ids["pmid"] = wids["pmid"].rsplit("/", 1)[-1]
    if wids.get("mag"):
        ids["mag"] = str(wids["mag"])
    authors = []
    for a in w.get("authorships") or []:
        name = a.get("raw_author_name") or (a.get("author") or {}).get("display_name") or ""
        if name:
            authors.append(parse_name(name))
    rec = Record(
        source=NAME, title=title, authors=authors, year=w.get("publication_year"),
        date=w.get("publication_date") or "", kind=kind,
        venue="arXiv" if kind == "preprint" and arxiv else (src.get("display_name") or ""),
        volume=biblio.get("volume") or "", number=biblio.get("issue") or "",
        pages=f"{fp}--{lp}" if fp and lp and fp != lp else fp,
        publisher=src.get("host_organization_name") or "",
        doi=doi if not doi.startswith("10.48550/") else "", arxiv=arxiv, ids=ids,
        url=prim.get("landing_page_url") or "", citations=w.get("cited_by_count"),
        status="preprint" if kind == "preprint" else "published",
        retracted=bool(w.get("is_retracted")), link=w.get("id", ""),
        weight=WEIGHTS[NAME], rank=rank,
    )
    if rec.retracted:
        rec.notices.append({"type": "retraction", "label": "Retraction", "doi": "", "date": "",
                            "source": "OpenAlex"})
    return rec


async def search(ctx: Ctx, q: Query) -> list[Record]:
    text = (q.title or q.text or q.raw)[:300]
    if q.kind == "title" and is_short_name(text):
        return await _search_short_name(ctx, text)
    if len(text) < 6:
        return []
    data = await get(ctx, API, _params({"search": text, "per_page": 8, "select": SELECT}),
                     ttl=86400, refresh=ctx.refresh, timeout=12)
    return [r for i, w in enumerate(data.get("results", [])) if (r := _parse(w, i))]


async def _search_short_name(ctx: Ctx, name: str) -> list[Record]:
    """« BERT », « U-Net » : les titres qui commencent par ce nom (« BERT: Pre-training
    of... »), les plus cités d'abord. La recherche plein texte, elle, noie le papier
    sous les « LoRa » des réseaux radio ou les entrées de dictionnaire."""
    data = await get(
        ctx, API, _params({"filter": f"title.search:{name.replace(',', ' ')}", "sort": "cited_by_count:desc",
                           "per_page": 50, "select": SELECT}),
        ttl=86400, refresh=ctx.refresh, timeout=12)
    out = []
    for w in data.get("results", []):
        r = _parse(w, len(out))
        if r and main_title_match(name, r.title):
            out.append(r)
    return out[:6]


async def lookup(ctx: Ctx, doi: str) -> Record | None:
    try:
        data = await get(ctx, f"{API}/doi:{doi}", _params({"select": SELECT}),
                         ttl=86400 * 3, refresh=ctx.refresh, neg_ttl=86400, timeout=10)
    except SourceError as e:
        if e.status == 404:
            return None
        raise
    return _parse(data)
