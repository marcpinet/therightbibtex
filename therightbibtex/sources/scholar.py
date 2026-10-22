"""Google Scholar, par SerpAPI quand une clé est configurée : SerpAPI franchit le
captcha de Google et renvoie les résultats en JSON. Son quota (offre gratuite : 250
recherches par mois) peut servir à d'autres usages de la même clé : therightbibtex
en garde une réserve, répartit le reste sur les jours qui restent, et une réponse en
cache ne coûte rien.

L'accès direct à Google Scholar (sans SerpAPI) n'est tenté que si l'option
scholar_direct est activée : une seule page de résultats, et dès que Google demande
de prouver qu'on n'est pas un robot, pause de quelques heures, sans contournement."""
from __future__ import annotations

import hashlib
import html
import re
from datetime import date
from urllib.parse import quote_plus

from .. import bibtex
from ..textutil import clean_title, parse_name, year_of

from .base import Ctx, Query, Record, WEIGHTS
from .http import BROWSER_UA, SourceError

NAME = "scholar"
BASE = "https://scholar.google.com"
SERPAPI = "https://serpapi.com"
HEADERS = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9",
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"}
BLOCK_MARKERS = ("gs_captcha", "not a robot", "unusual traffic", "/sorry/", "recaptcha")
PAUSE = 6 * 3600
# Réserve laissée aux autres usages de la même clé SerpAPI (scripts, autres outils).
RESERVE_FIXED = 15
RESERVE_PER_DAY = 3
CACHE_TTL = 86400 * 7

_used: dict[str, int] = {}   # recherches SerpAPI lancées ici, par clé et par jour


def _usage_key(key: str, day: date) -> str:
    return hashlib.sha256(key.encode()).hexdigest()[:12] + ":" + day.isoformat()


def search_url(text: str) -> str:
    return f"{BASE}/scholar?hl=en&q={quote_plus(text)}"


def _strip(s: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).replace("\xa0", " ").strip()


def _record(rank: int, title: str, url: str, cid: str, byline: str, cited: int | None, pdf: str = "") -> Record:
    """Une ligne de résultat : « A Vaswani, N Shazeer… - Advances in neural …, 2017 - neurips.cc »."""
    parts = [p.strip() for p in byline.split(" - ")]
    authors_part = parts[0] if parts else ""
    venue_part = parts[1] if len(parts) > 2 else ""
    venue = re.sub(r",?\s*\b(1[5-9]|20)\d\d\b", "", venue_part).strip(" ,")
    preprint = "arxiv" in venue.lower()
    return Record(
        source=NAME, title=title,
        authors=[parse_name(a) for a in authors_part.replace("…", "").split(",") if a.strip()],
        year=year_of(venue_part) or year_of(byline),
        venue=venue if "…" not in venue else "", venue_short=venue,
        kind="preprint" if preprint else "other", status="preprint" if preprint else "",
        citations=cited, ids={"scholar": cid} if cid else {}, url=url, pdf_url=pdf,
        link=search_url(title),
        weight=WEIGHTS[NAME], aggregate=True, incomplete_authors=True, rank=rank,
    )


def _clean_title(t: str) -> str:
    return clean_title(re.sub(r"^\[(PDF|HTML|BOOK|B|CITATION|C)\]\s*", "", t.strip()).lstrip("[]").strip())


# ----------------------------------------------------------------- SerpAPI

def _parse_serpapi(data: dict) -> list[Record]:
    out = []
    for i, res in enumerate((data.get("organic_results") or [])[:10]):
        title = _clean_title(res.get("title") or "")
        if not title:
            continue
        pdf = next((r["link"] for r in res.get("resources") or [] if r.get("file_format") == "PDF" and r.get("link")), "")
        out.append(_record(i, title, res.get("link") or "", res.get("result_id") or "",
                           (res.get("publication_info") or {}).get("summary") or "",
                           ((res.get("inline_links") or {}).get("cited_by") or {}).get("total"),
                           re.sub(r"\?src_trk=[^&]*$", "", pdf)))
    return out


def _days_left(today: date) -> int:
    first_next = date(today.year + (today.month == 12), today.month % 12 + 1, 1)
    return (first_next - today).days


async def _quota_note(ctx: Ctx, key: str) -> str:
    """« » si une recherche SerpAPI peut partir, sinon la raison. Le compte SerpAPI
    (account.json : gratuit, gardé 10 min) donne les recherches restantes ; on
    s'en tient à sa part du jour au-delà de la réserve des autres usages."""
    data = await ctx.http.get_json("scholar_quota", f"{SERPAPI}/account.json", params={"api_key": key},
                                   ttl=600, timeout=8)
    left = int(data.get("total_searches_left") or data.get("plan_searches_left") or 0)
    today = date.today()
    days = _days_left(today)
    reserve = RESERVE_FIXED + RESERVE_PER_DAY * days
    used = _used.get(_usage_key(key, today), 0)
    if left <= reserve or used >= (left + used - reserve) / days:
        return f"SerpAPI quota kept for other uses ({left} searches left this month)"
    return ""


async def _search_serpapi(ctx: Ctx, text: str, key: str) -> list[Record] | str:
    """Les notices, ou la raison pour laquelle SerpAPI n'a pas été utilisé."""
    url = f"{SERPAPI}/search.json"
    params = {"engine": "google_scholar", "q": text[:250], "num": 10, "hl": "en", "api_key": key}
    cached = not ctx.refresh and ctx.http.cache.get(ctx.http.cache_key("GET", url, params, None, None)) is not None
    if not cached:
        note = await _quota_note(ctx, key)
        if note:
            return note
    data = await ctx.http.get_json(NAME, url, params=params, ttl=CACHE_TTL, refresh=ctx.refresh, timeout=20,
                                   pace_key="serpapi")
    if not cached:
        day = _usage_key(key, date.today())
        _used[day] = _used.get(day, 0) + 1
    error = data.get("error") or ""
    if error and "hasn't returned any results" not in error:   # résultat vide : réponse valable
        raise SourceError("http", f"SerpAPI: {error[:120]}")
    return _parse_serpapi(data)


# ----------------------------------------------------------------- accès direct

def _parse_page(page: str) -> list[Record]:
    out = []
    blocks = re.split(r'<div class="gs_r gs_or gs_scl', page)[1:]
    for i, b in enumerate(blocks[:10]):
        cid = re.search(r'data-cid="([^"]+)"', b)
        h3 = re.search(r'<h3 class="gs_rt"[^>]*>(.*?)</h3>', b, re.S)
        if not h3:
            continue
        h3_html = re.sub(r'<span class="gs_ct[cg]?\d?"[^>]*>.*?</span>', "", h3.group(1), flags=re.S)
        h3_html = re.sub(r'<span class="gs_ctc">.*?</span>\s*</span>', "", h3_html, flags=re.S)
        link = re.search(r'<a[^>]+href="([^"]+)"', h3_html)
        ga = re.search(r'<div class="gs_a"[^>]*>(.*?)</div>', b, re.S)
        cited = re.search(r">Cited by (\d+)<", b)
        out.append(_record(i, _clean_title(_strip(h3_html)), html.unescape(link.group(1)) if link else "",
                           cid.group(1) if cid else "", _strip(ga.group(1)) if ga else "",
                           int(cited.group(1)) if cited else None))
    return out


def _blocked(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in BLOCK_MARKERS)


async def _search_direct(ctx: Ctx, text: str) -> list[Record]:
    br = ctx.http.breaker(NAME)
    try:
        r = await ctx.http.request(NAME, f"{BASE}/scholar", params={"hl": "en", "q": text[:250]},
                                   headers=HEADERS, ttl=86400 * 3, refresh=False, timeout=12,
                                   ok_status=(200,), cache_status=())
    except SourceError as e:
        if e.status in (403, 429) or e.kind == "ratelimited":
            br.fail("Google Scholar blocks automated requests", hard=True, cooldown=PAUSE)
            raise SourceError("blocked", "blocked by Google (captcha)")
        raise
    if _blocked(r.text) or "gs_ri" not in r.text:
        br.fail("Google Scholar asks for a captcha", hard=True, cooldown=PAUSE)
        raise SourceError("blocked", "blocked by Google (captcha)")
    recs = _parse_page(r.text)
    # Page valide : on la met en cache nous-mêmes (les pages captcha ne doivent jamais l'être).
    ctx.http.cache.put(ctx.http.cache_key("GET", f"{BASE}/scholar", {"hl": "en", "q": text[:250]}, HEADERS, None),
                       r, 86400 * 3)
    return recs


async def search(ctx: Ctx, q: Query) -> list[Record]:
    text = q.title or q.text or q.raw
    if len(text) < 6:
        return []
    notes = []
    # Chaque clé SerpAPI configurée, dans l'ordre : une clé épuisée (réserve atteinte)
    # ou refusée passe la main à la suivante, puis à l'accès direct.
    for i, key in enumerate(ctx.serpapi_keys):
        try:
            res = await _search_serpapi(ctx, text, key)
            if isinstance(res, list):
                return res
            why = res
        except SourceError as e:
            why = f"SerpAPI unavailable ({e.message})"
        notes.append(f"key {i + 1}: {why}" if len(ctx.serpapi_keys) > 1 else why)
    note = "; ".join(notes)
    if not ctx.scholar_direct:
        raise SourceError("unavailable", note or "no SerpAPI key")
    try:
        return await _search_direct(ctx, text)
    except SourceError as e:
        raise SourceError(e.kind, f"{note}; direct access: {e.message}" if note else e.message, status=e.status)


async def try_bibtex(ctx: Ctx, cid: str) -> str:
    """Export BibTeX de Scholar (souvent refusé en 403 hors navigateur ; SerpAPI ne
    le fournit pas : son moteur « cite » revient vide)."""
    br = ctx.http.breaker("scholar_bib")
    if br.is_open():
        return ""
    try:
        r = await ctx.http.request("scholar_bib", f"{BASE}/scholar",
                                   params={"q": f"info:{cid}:scholar.google.com/", "output": "cite",
                                           "scirp": "0", "hl": "en"},
                                   headers=HEADERS, ttl=86400 * 7, timeout=10, pace_key=NAME)
        m = re.search(r'href="([^"]*scholar\.bib[^"]*)"', r.text)
        if not m:
            raise SourceError("parse", "no BibTeX link")
        r2 = await ctx.http.request("scholar_bib", html.unescape(m.group(1)), headers=HEADERS,
                                    ttl=86400 * 7, timeout=10, pace_key=NAME)
    except SourceError:
        br.fail("Scholar BibTeX export refused", hard=True, cooldown=PAUSE)
        return ""
    return bibtex.reformat(r2.text) if r2.text.strip().startswith("@") else ""
