"""Crossref (api.crossref.org) : métadonnées déposées par les éditeurs pour
leurs DOI, relations prépublication <-> version publiée, rétractations."""
from __future__ import annotations

import re

from ..textutil import Author, clean_markup, clean_title, is_preprint_doi, norm_doi

from .base import Ctx, Query, Record, WEIGHTS
from .http import CONTACT, SourceError

NAME = "crossref"
API = "https://api.crossref.org/works"

_KIND = {
    "journal-article": "journal", "proceedings-article": "conference", "book-chapter": "chapter",
    "book-section": "chapter", "book-part": "chapter", "reference-entry": "chapter",
    "book": "book", "monograph": "book", "edited-book": "book", "reference-book": "book",
    "posted-content": "preprint", "dissertation": "thesis", "report": "report",
    "report-component": "report", "dataset": "dataset", "standard": "report", "other": "other",
}
_SKIP = {"peer-review", "component", "grant", "journal-issue", "journal-volume", "journal",
         "proceedings", "proceedings-series", "book-series", "book-set", "book-track", "database"}
_RETRACT = {"retraction", "withdrawal", "removal", "retracted"}
# Recommandations Faculty Opinions, notices de test, revues de livres... : jamais l'article lui-même.
_NOISE_RE = re.compile(r"^(faculty opinions recommendation of|test publication\b|review of\b|book review\b|"
                       r"recommendation of\b)", re.I)
_NOISE_PREFIXES = ("10.3410/",)


def _date(it: dict, *keys) -> list:
    for k in keys:
        parts = (it.get(k) or {}).get("date-parts") or []
        if parts and parts[0] and parts[0][0]:
            return parts[0]
    return []


# Ouvrages de référence : leurs entrées ne sont jamais l'article cherché.
_REFERENCE_WORK = re.compile(r"\b(dictionary|encyclopedia|encyclopaedia|accessscience|music online|"
                             r"oxford reference|reference module)\b", re.I)


def parse_item(it: dict, rank: int = 0) -> Record | None:
    ctype = it.get("type", "")
    if ctype in _SKIP or "update-to" in it:
        return None  # notices de correction, composants, revues par les pairs...
    title = clean_title((it.get("title") or [""])[0])
    if not title or _NOISE_RE.match(title) or (it.get("DOI") or "").lower().startswith(_NOISE_PREFIXES):
        return None
    sub = clean_markup((it.get("subtitle") or [""])[0]) if it.get("subtitle") else ""
    if sub and sub.lower() not in title.lower():
        title = f"{title}: {sub}"
    authors = []
    for a in it.get("author", []) or []:
        if a.get("family"):
            authors.append(Author(clean_markup(a.get("given", "")), clean_markup(a["family"])))
        elif a.get("name"):
            authors.append(Author("", clean_markup(a["name"]), corporate=True))
    kind = _KIND.get(ctype, "other")
    doi_raw = it.get("DOI", "")
    doi = norm_doi(doi_raw)
    if kind != "preprint" and is_preprint_doi(doi):
        kind = "preprint"
    # Année de citation : version imprimée (celle du volume) avant la mise en ligne.
    d = _date(it, "published-print", "issued", "published", "published-online", "created") \
        if kind != "preprint" else _date(it, "posted", "issued", "published", "created")
    containers = [clean_markup(c) for c in it.get("container-title") or [] if c]
    series = ""
    # Chapitres d'actes (LNCS, CCIS...) : Crossref donne [collection, titre du livre].
    if len(containers) >= 2 and kind in ("chapter", "conference"):
        series, venue = containers[0], containers[-1]
    else:
        venue = containers[0] if containers else ""
    if not venue and kind == "conference":
        venue = clean_markup((it.get("event") or {}).get("name", ""))
    if ctype in ("reference-entry", "reference-book") or _REFERENCE_WORK.search(venue):
        kind = "reference"   # entrée de dictionnaire ou d'encyclopédie (« Bert, J. », Benezit)
    if kind == "preprint" and not venue:
        venue = clean_markup(it.get("institution", [{}])[0].get("name", "")) if it.get("institution") else ""
        venue = venue or it.get("publisher", "")
    rec = Record(
        source=NAME, title=title, authors=authors,
        year=d[0] if d else None, month=d[1] if len(d) > 1 else None,
        date="-".join(f"{x:02d}" if i else str(x) for i, x in enumerate(d)) if d else "",
        kind=kind, venue=venue,
        venue_short=clean_markup((it.get("short-container-title") or [""])[0]),
        volume=str(it.get("volume", "") or ""), number=str(it.get("issue", "") or ""),
        pages=str(it.get("page", "") or it.get("article-number", "") or ""),
        publisher=it.get("publisher", ""), series=series,
        isbn=(it.get("ISBN") or [""])[0], issn=(it.get("ISSN") or [""])[0],
        doi=doi, ids={"doi_raw": doi_raw} if doi_raw else {},
        url=(it.get("resource") or {}).get("primary", {}).get("URL", "") or it.get("URL", ""),
        abstract=clean_markup(it.get("abstract", "")),
        citations=it.get("is-referenced-by-count"),
        status="preprint" if kind == "preprint" else "published",
        link=f"https://api.crossref.org/works/{doi}" if doi else "",
        weight=WEIGHTS[NAME], rank=rank,
    )
    if kind == "thesis":
        rec.school = clean_markup((it.get("institution") or [{}])[0].get("name", "")) if it.get("institution") else ""
    for rel, items in (it.get("relation") or {}).items():
        if rel in ("is-preprint-of", "has-preprint", "is-version-of", "has-version",
                   "is-identical-to", "is-manifestation-of", "has-manifestation"):
            for x in items if isinstance(items, list) else [items]:
                if x.get("id-type") == "doi" and x.get("id"):
                    rec.related.append(("doi", norm_doi(x["id"]), rel))
                elif x.get("id-type") == "arxiv" and x.get("id"):
                    rec.related.append(("arxiv", x["id"], rel))
    for up in it.get("updated-by", []) or []:
        utype = (up.get("type") or "").lower()
        when = _date(up, "updated")
        label = {"retraction": "Retraction", "withdrawal": "Withdrawal", "removal": "Removal",
                 "correction": "Correction", "erratum": "Erratum", "corrigendum": "Corrigendum",
                 "expression_of_concern": "Expression of concern",
                 "addendum": "Addendum", "new_version": "New version"}.get(utype, utype or "Update")
        if utype in _RETRACT:
            rec.retracted = True
        rec.notices.append({
            "type": utype, "label": label, "doi": norm_doi(up.get("DOI", "")),
            "date": "-".join(str(x) for x in when) if when else "", "source": "Crossref",
        })
    return rec


async def search(ctx: Ctx, q: Query) -> list[Record]:
    text = q.text or q.title or q.raw
    if q.kind == "title" and q.authors:
        text = f"{text} {' '.join(q.authors)}"
    params = {"query.bibliographic": text[:600], "rows": 12}
    if CONTACT:
        params["mailto"] = CONTACT
    data = await ctx.http.get_json(NAME, API, params=params, ttl=86400, refresh=ctx.refresh, timeout=12)
    out = []
    for i, it in enumerate(data.get("message", {}).get("items", [])):
        rec = parse_item(it, i)
        if rec:
            out.append(rec)
    return out


async def lookup(ctx: Ctx, doi: str) -> Record | None:
    params = {"mailto": CONTACT} if CONTACT else None
    try:
        data = await ctx.http.get_json(NAME, f"{API}/{doi}", params=params, ttl=86400 * 3,
                                       refresh=ctx.refresh, neg_ttl=86400, timeout=10)
    except SourceError as e:
        if e.status == 404:
            return None
        raise
    return parse_item(data.get("message", {}))
