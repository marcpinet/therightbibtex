"""Semantic Scholar (Graph API). Avec clé : 1 req/s par clé. Une clé refusée (403)
est mise de côté pour la suivante, puis le pool public (plus lent, souvent 429)."""
from __future__ import annotations

import hashlib
import logging

from .. import bibtex
from ..textutil import clean_title, is_short_name, norm_arxiv, norm_doi, parse_name, year_of

from .base import Ctx, Query, Record, WEIGHTS
from .http import SourceError

logger = logging.getLogger(__name__)

NAME = "s2"
API = "https://api.semanticscholar.org/graph/v1"
FIELDS = ("paperId,corpusId,externalIds,title,authors,year,publicationDate,venue,publicationVenue,"
          "journal,publicationTypes,citationCount,url,citationStyles")
ARXIV_VENUES = {"arxiv", "arxiv.org", "corr", "biorxiv", "medrxiv", "ssrn", "research square"}

_rejected: set[str] = set()   # clés refusées depuis le démarrage


def key_status(key: str) -> str:
    if not key:
        return "missing"
    return "rejected (403)" if key in _rejected else "ok"


def _pace_key(key: str) -> str:
    """Rythme (1 req/s) et disjoncteur propres à chaque clé, sans la clé elle-même."""
    return "s2:" + hashlib.sha1(key.encode()).hexdigest()[:10]


async def _call(ctx: Ctx, path: str, *, params=None, data=None, method="GET", ttl=86400):
    """Chaque clé configurée, dans l'ordre : une clé refusée (403) est écartée, une clé
    saturée (429) passe la main à la suivante ; à défaut, le pool public."""
    for key in [k for k in ctx.s2_keys if k not in _rejected]:
        try:
            return await ctx.http.get_json(
                NAME, API + path, params=params, headers={"x-api-key": key}, data=data, method=method, ttl=ttl,
                refresh=ctx.refresh, timeout=10, pace_key=_pace_key(key), ok_status=(200, 404), cache_status=(200,),
            )
        except SourceError as e:
            if e.status == 403:
                _rejected.add(key)
                logger.warning("Clé Semantic Scholar …%s refusée (403) : clé suivante", key[-4:])
            elif not (e.status == 429 or e.kind in ("ratelimited", "unavailable")):
                raise
    return await ctx.http.get_json(
        NAME, API + path, params=params, data=data, method=method, ttl=ttl,
        refresh=ctx.refresh, timeout=10, pace_key="s2_public", ok_status=(200, 404), cache_status=(200,),
    )


def _parse(p: dict, rank: int = 0) -> Record | None:
    if not p or not p.get("title"):
        return None
    ext = p.get("externalIds") or {}
    venue = (p.get("venue") or "").strip()
    pv = p.get("publicationVenue") or {}
    journal = p.get("journal") or {}
    jname = (journal.get("name") or "").strip()
    types = p.get("publicationTypes") or []
    full_venue = pv.get("name") or venue or jname
    is_preprint_venue = (full_venue or "").lower() in ARXIV_VENUES or (jname.lower() in ARXIV_VENUES and not venue)
    if jname.lower() in ARXIV_VENUES or str(journal.get("volume") or "").startswith("abs/"):
        journal = {}  # « revue » = arXiv : volume/pages sont ceux de la prépublication
    if not full_venue or is_preprint_venue:
        kind, status = "preprint", "preprint"
    elif "Conference" in types or pv.get("type") == "conference":
        kind, status = "conference", "published"
    elif "JournalArticle" in types or pv.get("type") == "journal":
        kind, status = "journal", "published"
    else:
        kind, status = "other", "published"
    doi = norm_doi(ext.get("DOI", ""))
    arxiv = norm_arxiv(ext.get("ArXiv", ""))
    ids = {"s2": p.get("paperId", "")}
    for k, n in (("DBLP", "dblp"), ("ACL", "acl"), ("PubMed", "pmid"), ("PubMedCentral", "pmcid"),
                 ("CorpusId", "corpus"), ("MAG", "mag")):
        if ext.get(k):
            ids[n] = str(ext[k])
    rec = Record(
        source=NAME, title=clean_title(p["title"]),
        authors=[parse_name(a.get("name", "")) for a in p.get("authors") or [] if a.get("name")],
        year=p.get("year") or year_of(p.get("publicationDate")), date=p.get("publicationDate") or "",
        kind=kind, venue="" if is_preprint_venue else full_venue,
        venue_short=venue if venue and venue != full_venue else "",
        volume=(journal.get("volume") or "").strip() if not is_preprint_venue else "",
        pages=(journal.get("pages") or "").strip().replace(" ", "") if not is_preprint_venue else "",
        doi=doi if not doi.startswith("10.48550/") else "", arxiv=arxiv, ids=ids,
        citations=p.get("citationCount"), status=status, link=p.get("url", ""),
        weight=WEIGHTS[NAME], aggregate=True, rank=rank,
    )
    bib = (p.get("citationStyles") or {}).get("bibtex") or ""
    if bib.startswith("@"):
        # S2 écrit parfois le type comme une liste Python : @['JournalArticle', 'Conference']{...
        import re
        bib = re.sub(r"^@\[[^\]]*\]", "@inproceedings" if kind == "conference" else
                     "@article" if kind == "journal" else "@misc", bib)
        rec.bibtex = bibtex.reformat(bib)
        rec.bibtex_label = "Semantic Scholar"
    return rec


async def search(ctx: Ctx, q: Query) -> list[Record]:
    text = (q.title or q.text or q.raw)[:300]
    short = q.kind == "title" and is_short_name(text)   # « BERT » : pas un titre entier à apparier
    if len(text) < 6 and not short:
        return []
    out: list[Record] = []
    if q.title and not short:
        data = await _call(ctx, "/paper/search/match", params={"query": q.title[:300], "fields": FIELDS})
        for i, p in enumerate((data or {}).get("data", [])[:1]):
            if (r := _parse(p, i)):
                out.append(r)
    if not out or any(k not in _rejected for k in ctx.s2_keys):
        data = await _call(ctx, "/paper/search", params={"query": text, "limit": 6, "fields": FIELDS})
        seen = {r.ids.get("s2") for r in out}
        for i, p in enumerate((data or {}).get("data", [])):
            if p.get("paperId") not in seen and (r := _parse(p, i + 1)):
                out.append(r)
    return out


async def lookup(ctx: Ctx, ids: list[str]) -> list[Record]:
    """ids au format S2 : DOI:..., ARXIV:..., ACL:..., PMID:..., CorpusId:..., ou paperId."""
    ids = [i for i in dict.fromkeys(ids) if i][:20]
    if not ids:
        return []
    out = []
    # GET unitaires (mis en cache) plutôt que /paper/batch en POST ; au plus 3 essais.
    for i, pid in enumerate(ids[:3]):
        try:
            p = await _call(ctx, f"/paper/{pid}", params={"fields": FIELDS}, ttl=86400 * 3)
        except SourceError:
            if i == 0:
                raise
            break
        if p and p.get("paperId") and (r := _parse(p, i)):
            out.append(r)
            break  # un seul identifiant suffit à retrouver l'œuvre
    return out
