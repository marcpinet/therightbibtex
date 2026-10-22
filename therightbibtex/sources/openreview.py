"""OpenReview (API v2 + v1 pour les anciennes conférences) : ICLR, TMLR,
NeurIPS récents, COLM... Le champ `_bibtex` est la notice officielle, et
`venue`/`venueid` disent si le papier a été accepté, rejeté ou retiré."""
from __future__ import annotations

import asyncio
import re

from .. import bibtex
from ..textutil import clean_title, is_short_name, latex_to_text, parse_name, title_match

from .base import Ctx, Query, Record, WEIGHTS
from .http import SourceError

NAME = "openreview"
V2 = "https://api2.openreview.net"
V1 = "https://api.openreview.net"

_REJECT_RE = re.compile(r"(reject|withdraw|desk|submitted to|under review|submission$|blind|"
                        r"/submission|/withdrawn|/rejected|/desk_rejected)", re.I)


# Groupes OpenReview de vraies conférences/revues (pas les espaces d'archives).
_VENUE_GROUP_RE = re.compile(r"(/Conference|/Workshop|/Track|^TMLR|^JMLR|^DMLR|^TACL|/Journal|"
                             r"^ICLR|^NeurIPS|^ICML|^COLM|^MIDL|^AKBC|^CoRL|^UAI|^AISTATS|^ACM|^IEEE|"
                             r"^thecvf|^aclweb|^ISMIR|^logconference|^automl)", re.I)


def _val(c: dict, k: str):
    v = c.get(k)
    return v.get("value") if isinstance(v, dict) else v


def _parse(n: dict, api: str, rank: int = 0) -> Record | None:
    c = n.get("content") or {}
    title = clean_title(latex_to_text(_val(c, "title") or ""))
    if not title:
        return None
    inv = " ".join(n.get("invitations") or [n.get("invitation") or ""])
    if ("dblp.org/-/record" in inv.lower() or "/Public_Article" in inv or "Archive" in inv
            or "Direct_Upload" in inv):
        return None  # copies DBLP / dépôts d'auteurs hors processus de relecture
    venue = (_val(c, "venue") or "").strip()
    venueid = (_val(c, "venueid") or "").strip()
    raw_bib = _val(c, "_bibtex") or ""
    accepted = (bool(venue) and not _REJECT_RE.search(venue) and not _REJECT_RE.search(venueid)
                and bool(_VENUE_GROUP_RE.search(venueid or inv)) and not re.search(r"\barxiv\b", venue, re.I))
    entry = bibtex.parse_one(raw_bib) if raw_bib else None
    is_journal = bool(entry and entry.type == "article") or "TMLR" in venueid or "Transactions" in venue
    kind = ("journal" if is_journal else "conference") if accepted else "preprint"
    full_venue = ""
    if entry:
        full_venue = entry.text("booktitle") or entry.text("journal")
    year = None
    if entry and entry.get("year").isdigit():
        year = int(entry.get("year"))
    if not year:
        m = re.search(r"(20\d\d)", venue or venueid)
        year = int(m.group(1)) if m else None
    forum = n.get("forum") or n.get("id")
    status_note = venue or ("submission not accepted" if not accepted else "")
    rec = Record(
        source=NAME, title=title,
        authors=[parse_name(a) for a in (_val(c, "authors") or []) if isinstance(a, str)],
        year=year, kind=kind, venue=full_venue if accepted else "OpenReview",
        venue_short=re.sub(r"\s+(poster|oral|spotlight|notable.*|top.*)$", "", venue, flags=re.I) if accepted else "",
        ids={"openreview": forum}, url=f"https://openreview.net/forum?id={forum}",
        pdf_url=f"https://openreview.net/pdf?id={forum}",
        status="published" if accepted else "submitted", status_note=status_note,
        abstract=latex_to_text(_val(c, "abstract") or ""),
        link=f"https://openreview.net/forum?id={forum}",
        weight=WEIGHTS[NAME] if accepted else 45, rank=rank,
    )
    if raw_bib and accepted:
        rec.bibtex = bibtex.reformat(raw_bib)
        rec.bibtex_label = "OpenReview"
    return rec


async def _search_api(ctx: Ctx, base: str, term: str, limit: int) -> list[dict]:
    data = await ctx.http.get_json(
        NAME, f"{base}/notes/search",
        params={"term": term, "type": "terms", "content": "all", "group": "all", "source": "forum",
                "limit": limit},
        ttl=86400, refresh=ctx.refresh, timeout=12,
    )
    return data.get("notes", [])


async def search(ctx: Ctx, q: Query) -> list[Record]:
    title = q.title or (q.raw if q.kind == "title" else "")
    if not title or (len(title) < 8 and not (q.kind == "title" and is_short_name(title))):
        return []
    term = re.sub(r"[^\w\s'-]", " ", title)[:200]
    res = await asyncio.gather(_search_api(ctx, V2, term, 20), _search_api(ctx, V1, term, 12),
                               return_exceptions=True)
    out = []
    errors = [r for r in res if isinstance(r, Exception)]
    if len(errors) == 2:
        raise errors[0] if isinstance(errors[0], SourceError) else SourceError("http", str(errors[0]))
    for api, notes in zip(("v2", "v1"), res):
        if isinstance(notes, Exception):
            continue
        for i, n in enumerate(notes):
            rec = _parse(n, api, i)
            if rec and title_match(rec.title, title) >= 0.8:
                out.append(rec)
    return out


async def lookup(ctx: Ctx, note_id: str) -> list[Record]:
    """Lecture directe d'une note. Depuis 2026, OpenReview exige souvent une
    vérification anti-robot sur /notes (la recherche reste ouverte) : le
    moteur passe alors par DBLP, qui indexe les pages OpenReview."""
    blocked = 0
    for base, api in ((V2, "v2"), (V1, "v1")):
        try:
            data = await ctx.http.get_json(NAME, f"{base}/notes", params={"id": note_id},
                                           ttl=86400, refresh=ctx.refresh, timeout=10, neg_ttl=3600)
        except SourceError as e:
            blocked += e.status == 403
            if blocked == 2:
                raise SourceError("blocked", "direct read refused (anti-bot check)")
            continue
        notes = data.get("notes") or []
        if notes:
            rec = _parse(notes[0], api)
            if rec:
                return [rec]
        # note absente de cette API (v1 <-> v2) : on essaie l'autre
    return []
