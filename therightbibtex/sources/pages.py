"""Pages web : BibTeX officiel des sites de proceedings (NeurIPS, PMLR, CVF,
JMLR, ACL Anthology) et extraction générique des métadonnées d'une page
d'article (balises citation_*, Dublin Core, JSON-LD, BibTeX embarqué)."""
from __future__ import annotations

import html
import json
import re
from urllib.parse import urljoin, urlparse

import httpx

from .. import bibtex
from ..textutil import clean_markup, clean_title, norm_arxiv, norm_doi, parse_name, year_of

from .base import Ctx, Record, WEIGHTS
from .http import BROWSER_UA, CONNECT_TIMEOUT, SourceError

NAME = "pages"

OFFICIAL_HOSTS = ("proceedings.neurips.cc", "papers.nips.cc", "papers.neurips.cc", "proceedings.mlr.press",
                  "openaccess.thecvf.com", "jmlr.org", "www.jmlr.org", "aclanthology.org")


def is_official(url: str) -> bool:
    return urlparse(url or "").netloc.lower() in OFFICIAL_HOSTS


def _neurips_page(url: str) -> str:
    m = re.search(r"/paper(?:_files/paper)?/(\d{4})/file/([0-9a-f]{20,})-(?:Paper|Supplemental)(-[\w]+)?\.pdf", url)
    if m:
        year, h, suffix = m.groups()
        return f"https://proceedings.neurips.cc/paper_files/paper/{year}/hash/{h}-Abstract{suffix or ''}.html"
    return url


def _pmlr_page(url: str) -> str:
    m = re.search(r"proceedings\.mlr\.press/(v\d+)/([\w-]+)/\2\.pdf", url)
    return f"https://proceedings.mlr.press/{m.group(1)}/{m.group(2)}.html" if m else url


def _cvf_page(url: str) -> str:
    return url.replace("/papers/", "/html/").replace("_paper.pdf", "_paper.html") if url.endswith(".pdf") else url


def _jmlr_bib(url: str) -> str:
    m = re.search(r"jmlr\.org/papers/(?:v|volume)(\d+)/([\w.-]+?)(?:/\2)?(?:\.html|\.pdf|/)?$", url)
    return f"https://www.jmlr.org/papers/v{m.group(1)}/{m.group(2)}.bib" if m else ""


def _acl_bib(url: str) -> str:
    m = re.search(r"aclanthology\.org/([\w.-]+?)(?:\.pdf|\.bib|/)?$", url)
    return f"https://aclanthology.org/{m.group(1)}.bib" if m else ""


async def _get(ctx: Ctx, url: str, browser: bool = False, ttl: float = 86400 * 7) -> str:
    return (await _get_resp(ctx, url, browser, ttl)).text


async def _get_resp(ctx: Ctx, url: str, browser: bool = False, ttl: float = 86400 * 7):
    headers = {"User-Agent": BROWSER_UA, "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
               "Accept-Language": "en-US,en;q=0.8"} if browser else None
    return await ctx.http.request(NAME, url, headers=headers, ttl=ttl, refresh=ctx.refresh, timeout=12)


def _record_from_bib(text: str, url: str, label: str, source: str = "proceedings") -> Record | None:
    e = bibtex.parse_one(text)
    if not e:
        return None
    etype = e.type
    kind = {"article": "journal", "inproceedings": "conference", "incollection": "chapter"}.get(etype, "other")
    venue = e.text("booktitle") or e.text("journal")
    year = e.get("year")
    rec = Record(
        source=source, title=clean_title(e.text("title")), authors=e.authors(),
        year=int(year) if year.isdigit() else year_of(year), kind=kind, venue=venue,
        volume=e.get("volume"), number=e.get("number"), pages=e.get("pages"),
        publisher=e.text("publisher"), series=e.text("series"), doi=norm_doi(e.get("doi")),
        url=e.get("url") or url, status="published", link=url,
        weight=WEIGHTS.get(source, 90),
    )
    if "arxiv" in rec.venue.lower():
        return None
    rec.bibtex = bibtex.reformat(text)
    rec.bibtex_label = label
    return rec


async def official(ctx: Ctx, url: str) -> Record | None:
    """BibTeX publié par le site de proceedings lui-même."""
    host = urlparse(url).netloc.lower()
    if host in ("proceedings.neurips.cc", "papers.nips.cc", "papers.neurips.cc"):
        resp = await _get_resp(ctx, _neurips_page(url))
        page, page_url = resp.text, resp.url  # URL finale (les anciennes adresses redirigent)
        # Deux formats selon les années : « ...-Bibtex.bib » ou « /paper_files/paper/NNNN-/bibtex ».
        m = re.search(r"href=['\"]([^'\"]*(?:Bibtex[^'\"]*\.bib|/bibtex))['\"]", page)
        if not m:
            rec = generic_from_html(page, page_url)
            if rec:
                rec.source, rec.weight = "proceedings", WEIGHTS["proceedings"] - 5
            return rec
        bib = await _get(ctx, urljoin(page_url, m.group(1)))
        rec = _record_from_bib(bib, page_url, "NeurIPS Proceedings")
        if rec:
            rec.url = page_url
        return rec
    if host == "proceedings.mlr.press":
        resp = await _get_resp(ctx, _pmlr_page(url))
        page, page_url = resp.text, resp.url
        m = re.search(r'<code[^>]*id="bibtex"[^>]*>(.*?)</code>', page, re.S)
        if not m:
            return generic_from_html(page, page_url)
        return _record_from_bib(html.unescape(re.sub(r"<[^>]+>", "", m.group(1))), page_url, "PMLR")
    if host == "openaccess.thecvf.com":
        resp = await _get_resp(ctx, _cvf_page(url))
        page, page_url = resp.text, resp.url
        m = re.search(r'<div class="bibref[^"]*">(.*?)</div>', page, re.S)
        if not m:
            return generic_from_html(page, page_url)
        text = html.unescape(re.sub(r"<br\s*/?>", "\n", m.group(1)))
        rec = _record_from_bib(re.sub(r"<[^>]+>", "", text), page_url, "CVF Open Access")
        if rec:
            rec.url = page_url
        return rec
    if host in ("jmlr.org", "www.jmlr.org"):
        bib_url = _jmlr_bib(url)
        if bib_url:
            return _record_from_bib(await _get(ctx, bib_url), bib_url.replace(".bib", ".html"), "JMLR")
    if host == "aclanthology.org":
        bib_url = _acl_bib(url)
        if bib_url:
            rec = _record_from_bib(await _get(ctx, bib_url), bib_url[:-4] + "/", "ACL Anthology", "acl")
            return rec
    return None


async def jmlr_by_title(ctx: Ctx, volume: str, title: str) -> Record | None:
    """Les anciens articles JMLR ne sont liés par DBLP qu'à l'ACM DL : on
    retrouve la notice officielle dans l'index du volume sur jmlr.org."""
    from ..textutil import title_similarity
    if not str(volume).isdigit():
        return None
    index_url = f"https://www.jmlr.org/papers/v{volume}/"
    page = await _get(ctx, index_url, ttl=86400 * 30)
    best, best_sim = None, 0.0
    # <dd> n'est jamais refermé sur jmlr.org : on lit jusqu'au <dt> suivant.
    for m in re.finditer(r"<dt>(.*?)</dt>(.*?)(?=<dt>|</dl>)", page, re.S | re.I):
        t = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
        bib = re.search(r"href=[\"']([^\"']+\.bib)[\"']", m.group(2))
        if not bib:
            continue
        sim = title_similarity(t, title)
        if sim > best_sim:
            best, best_sim = bib.group(1), sim
    if not best or best_sim < 0.9:
        return None
    bib_url = urljoin(index_url, best)
    return _record_from_bib(await _get(ctx, bib_url), bib_url.replace(".bib", ".html"), "JMLR")


async def acl_by_id(ctx: Ctx, acl_id: str) -> Record | None:
    return await official(ctx, f"https://aclanthology.org/{acl_id}/")


# ----------------------------------------------------------------- générique

def _metas(page: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for m in re.finditer(r"<meta\s+[^>]*>", page, re.I):
        tag = m.group(0)
        name = re.search(r"(?:name|property)\s*=\s*['\"]([^'\"]+)['\"]", tag, re.I)
        content = re.search(r"content\s*=\s*(\"[^\"]*\"|'[^']*')", tag, re.I | re.S)
        if name and content:
            out.setdefault(name.group(1).strip().lower(), []).append(html.unescape(content.group(1)[1:-1]).strip())
    return out


def _first(m: dict, *names) -> str:
    for n in names:
        for v in m.get(n, []):
            if v:
                return v
    return ""


def _embedded_bibtex(page: str) -> str:
    for m in re.finditer(r"<(pre|code|textarea|div)[^>]*>\s*(@(?:article|inproceedings|misc|incollection|"
                         r"book|inbook|phdthesis|techreport|conference)\s*\{.*?)</\1>", page, re.S | re.I):
        text = html.unescape(re.sub(r"<br\s*/?>", "\n", m.group(2)))
        text = re.sub(r"<[^>]+>", "", text)
        if bibtex.parse_one(text):
            return text.strip()
    return ""


def _jsonld(page: str) -> dict:
    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', page, re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for it in items:
            t = it.get("@type") if isinstance(it, dict) else None
            types = t if isinstance(t, list) else [t]
            if any(x in ("ScholarlyArticle", "Article", "Report", "Thesis", "Chapter", "Book") for x in types):
                return it
    return {}


def generic_from_html(page: str, url: str) -> Record | None:
    m = _metas(page)
    bib = _embedded_bibtex(page)
    title = _first(m, "citation_title", "dc.title", "prism.title", "og:title")
    if not title and not bib:
        ld = _jsonld(page)
        if not ld:
            return None
        title = ld.get("headline") or ld.get("name") or ""
        authors = []
        for a in ld.get("author") or []:
            if isinstance(a, dict) and a.get("name"):
                authors.append(parse_name(a["name"]))
        part = ld.get("isPartOf") or {}
        return Record(source="web", title=clean_title(title), authors=authors,
                      year=year_of(ld.get("datePublished")), kind="other",
                      venue=part.get("name", "") if isinstance(part, dict) else "",
                      doi=norm_doi(str(ld.get("identifier") or ld.get("sameAs") or "")), url=url,
                      status="published", link=url, weight=WEIGHTS["web"]) if title else None
    authors = [parse_name(a) for a in m.get("citation_author", [])] or \
              [parse_name(a) for a in m.get("dc.creator", [])]
    journal = _first(m, "citation_journal_title", "prism.publicationname")
    conf = _first(m, "citation_conference_title", "citation_inbook_title", "citation_book_title")
    fp, lp = _first(m, "citation_firstpage"), _first(m, "citation_lastpage")
    doi = norm_doi(_first(m, "citation_doi", "prism.doi", "dc.identifier", "bepress_citation_doi"))
    arxiv = norm_arxiv(_first(m, "citation_arxiv_id"))
    kind = "conference" if conf else "journal" if journal else (
        "thesis" if _first(m, "citation_dissertation_institution") else
        "report" if _first(m, "citation_technical_report_institution") else "other")
    if journal and "arxiv" in journal.lower():
        kind, journal = "preprint", "arXiv"
    rec = Record(
        source="web", title=clean_title(title), authors=authors,
        year=year_of(_first(m, "citation_publication_date", "citation_date", "dc.date",
                            "citation_online_date", "prism.publicationdate")),
        kind=kind, venue=conf or journal, volume=_first(m, "citation_volume", "prism.volume"),
        number=_first(m, "citation_issue", "prism.number"),
        pages=f"{fp}--{lp}" if fp and lp and fp != lp else fp,
        publisher=_first(m, "citation_publisher", "dc.publisher"),
        isbn=_first(m, "citation_isbn"), issn=_first(m, "citation_issn", "prism.issn"),
        school=_first(m, "citation_dissertation_institution"),
        institution=_first(m, "citation_technical_report_institution"),
        doi=doi, arxiv=arxiv, url=_first(m, "citation_abstract_html_url") or url,
        pdf_url=_first(m, "citation_pdf_url"), abstract=clean_markup(_first(m, "citation_abstract", "dc.description")),
        status="preprint" if kind == "preprint" else "published", link=url, weight=WEIGHTS["web"],
    )
    if bib:
        from_bib = _record_from_bib(bib, url, "Article page", "web")
        if from_bib and (not rec.title or not rec.authors):
            from_bib.doi = from_bib.doi or doi
            return from_bib
        if from_bib:
            rec.bibtex, rec.bibtex_label = from_bib.bibtex, "Article page"
    return rec if rec.title else None


MAX_PAGE = 3_000_000                # au-delà, la page est tronquée (les métadonnées sont en tête)
MAX_PDF = 40 * 1024 * 1024


async def _check_public(url: str) -> str:
    """Refuse les URL qui viseraient le réseau local du serveur (SSRF) et
    renvoie l'adresse vérifiée, à laquelle on se connecte ensuite."""
    import asyncio
    import ipaddress
    import socket
    u = urlparse(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise SourceError("http", "unsupported URL")
    if u.port not in (None, 80, 443, 8080, 8443):
        raise SourceError("http", "port not allowed")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(u.hostname, None, type=socket.SOCK_STREAM)
    except OSError:
        raise SourceError("http", "domain name not found")
    addrs = []
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        except ValueError:
            raise SourceError("http", "non-public address refused")
        if not ip.is_global or ip.is_multicast:
            raise SourceError("http", "non-public address refused")
        addrs.append(str(ip))
    if not addrs:
        raise SourceError("http", "domain name not found")
    return addrs[0]


async def _safe_get(ctx: Ctx, url: str, headers: dict, timeout: float = 30):
    """GET en flux qui suit les redirections à la main en revérifiant chaque
    saut. La connexion se fait à l'adresse vérifiée (le nom reste dans Host et
    dans la négociation TLS) : un DNS qui changerait de réponse entre la
    vérification et la connexion ne peut pas viser le réseau local.
    Renvoie (réponse, URL logique)."""
    from urllib.parse import urlunparse
    for _ in range(6):
        ip = await _check_public(url)
        u = urlparse(url)
        try:
            host = u.hostname.encode("idna").decode("ascii")
        except UnicodeError:
            raise SourceError("http", "invalid domain name")
        port = f":{u.port}" if u.port else ""
        target = urlunparse(u._replace(netloc=(f"[{ip}]" if ":" in ip else ip) + port))
        req = ctx.http.client.build_request(
            "GET", target, headers={**headers, "Host": host + port},
            timeout=httpx.Timeout(timeout, connect=CONNECT_TIMEOUT), extensions={"sni_hostname": host})
        r = await ctx.http.client.send(req, stream=True, follow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
            await r.aclose()
            url = urljoin(url, r.headers["location"])
            continue
        return r, url
    raise SourceError("http", "too many redirects")


async def generic(ctx: Ctx, url: str) -> tuple[Record | None, str, bytes]:
    """Récupère une page quelconque en un seul téléchargement plafonné.
    Renvoie (notice, type de contenu, octets du PDF quand c'en est un)."""
    headers = {"User-Agent": BROWSER_UA, "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.9,*/*;q=0.8",
               "Accept-Language": "en-US,en;q=0.8"}
    try:
        r, final_url = await _safe_get(ctx, url, headers)
    except SourceError:
        raise
    except Exception as e:  # httpx
        raise SourceError("http", f"page unreachable ({type(e).__name__})")
    chunks: list[bytes] = []
    try:
        if r.status_code in (401, 403, 429, 503):
            raise SourceError("blocked", f"the site refuses access (HTTP {r.status_code})", status=r.status_code)
        if r.status_code != 200:
            raise SourceError("http", f"HTTP {r.status_code}", status=r.status_code)
        is_pdf = "pdf" in r.headers.get("content-type", "").lower()
        size = 0
        async for chunk in r.aiter_bytes():
            if not chunks and chunk[:5] == b"%PDF-":
                is_pdf = True
            size += len(chunk)
            if size > (MAX_PDF if is_pdf else MAX_PAGE):
                if is_pdf:
                    raise SourceError("http", "PDF too large (40 MB max)")
                chunks.append(chunk[:len(chunk) - (size - MAX_PAGE)])
                break
            chunks.append(chunk)
    except httpx.HTTPError as e:
        raise SourceError("http", f"page unreachable ({type(e).__name__})")
    finally:
        await r.aclose()
    data = b"".join(chunks)
    if is_pdf:
        if not data.startswith(b"%PDF-"):
            raise SourceError("parse", "the downloaded file is not a PDF")
        return None, "pdf", data
    text = data.decode(r.charset_encoding or "utf-8", errors="replace")
    return generic_from_html(text, final_url), "html", b""
