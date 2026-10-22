"""arXiv : API Atom (export.arxiv.org) + BibTeX officiel (arxiv.org/bibtex)."""
from __future__ import annotations

import re
from xml.etree import ElementTree as ET

from .. import bibtex
from ..textutil import arxiv_version, clean_title, latex_to_text, norm_arxiv, norm_doi, norm_title, parse_name

from .base import Ctx, Query, Record, WEIGHTS
from .http import SourceError

NAME = "arxiv"
API = "https://export.arxiv.org/api/query"
NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}

# Commentaires/journal_ref qui annoncent une publication.
ACCEPT_RE = re.compile(
    r"(accepted|to appear|published|presented|camera[- ]ready|in proceedings|oral|spotlight|poster)"
    r"[^.;]{0,80}?\b((?:at|in|by|to|for)\b[^.;]{2,90})", re.I)


def _text(el, path: str) -> str:
    x = el.find(path, NS)
    return re.sub(r"\s+", " ", x.text or "").strip() if x is not None and x.text else ""


def _parse(xml: str) -> list[Record]:
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        raise SourceError("parse", "unreadable Atom feed")
    out = []
    for i, e in enumerate(root.findall("a:entry", NS)):
        id_url = _text(e, "a:id")
        if "/abs/" not in id_url:
            continue  # entrée d'erreur
        raw_id = id_url.split("/abs/", 1)[1]
        aid = norm_arxiv(raw_id)
        published = _text(e, "a:published")
        updated = _text(e, "a:updated")
        comment = _text(e, "arxiv:comment")
        jref = _text(e, "arxiv:journal_ref")
        doi = norm_doi(_text(e, "arxiv:doi"))
        prim = e.find("arxiv:primary_category", NS)
        rec = Record(
            source=NAME,
            title=clean_title(latex_to_text(_text(e, "a:title"))),
            authors=[parse_name(_text(a, "a:name")) for a in e.findall("a:author", NS)],
            year=int(published[:4]) if published[:4].isdigit() else None,
            month=int(published[5:7]) if published[5:7].isdigit() else None,
            date=published[:10], kind="preprint", venue="arXiv",
            arxiv=aid, arxiv_version=arxiv_version(raw_id),
            arxiv_class=prim.get("term", "") if prim is not None else "",
            url=f"https://arxiv.org/abs/{aid}", pdf_url=f"https://arxiv.org/pdf/{aid}",
            abstract=latex_to_text(_text(e, "a:summary")), status="preprint",
            link=f"https://arxiv.org/abs/{aid}", weight=WEIGHTS[NAME], rank=i,
        )
        rec.ids["arxiv_updated"] = updated[:10]
        if doi and not doi.startswith("10.48550/"):
            rec.related.append(("doi", doi, "arxiv-doi"))
        notes = []
        if jref:
            notes.append(f"journal-ref: {jref}")
            rec.ids["journal_ref"] = jref
        if comment:
            m = ACCEPT_RE.search(comment)
            if m:
                notes.append(f"comment: {comment[:160]}")
        rec.status_note = " · ".join(notes)
        if comment:
            rec.ids["comment"] = comment[:300]
        out.append(rec)
    return out


def _words_query(title: str) -> str:
    """Tous les mots, cherchés partout (titre, résumé, auteurs) : rattrape une
    ponctuation différente du titre exact, et une saisie en mots-clés qui mêle un
    nom d'auteur au titre (« dosovitskiy image worth words »)."""
    words = [w for w in norm_title(title).split() if len(w) > 1][:12]
    return " AND ".join(f"all:{w}" for w in words)


async def search(ctx: Ctx, q: Query) -> list[Record]:
    title = q.title or (q.raw if q.kind == "title" else "")
    if not title or len(title) < 6:
        return []
    phrase = re.sub(r"[\"():\[\]{}]", " ", title)
    phrase = re.sub(r"\s+", " ", phrase).strip()
    xml = (await ctx.http.request(
        NAME, API, params={"search_query": f'ti:"{phrase}"', "max_results": 6},
        ttl=86400, refresh=ctx.refresh, timeout=15)).text
    recs = _parse(xml)
    if not recs:
        tq = _words_query(title)
        if tq:
            xml = (await ctx.http.request(
                NAME, API, params={"search_query": tq, "max_results": 6},
                ttl=86400, refresh=ctx.refresh, timeout=15)).text
            recs = _parse(xml)
    return recs


async def lookup(ctx: Ctx, ids: list[str]) -> list[Record]:
    ids = [i for i in dict.fromkeys(ids) if i][:20]
    if not ids:
        return []
    xml = (await ctx.http.request(
        NAME, API, params={"id_list": ",".join(ids), "max_results": len(ids)},
        ttl=86400 * 2, refresh=ctx.refresh, timeout=15)).text
    return _parse(xml)


async def official_bibtex(ctx: Ctx, aid: str) -> str:
    """BibTeX produit par arXiv lui-même (bouton « Export BibTeX Citation »)."""
    r = await ctx.http.request("arxiv_web", f"https://arxiv.org/bibtex/{aid}", ttl=86400 * 7,
                               refresh=ctx.refresh, timeout=10)
    text = r.text.strip()
    return bibtex.reformat(text) if text.startswith("@") else ""
