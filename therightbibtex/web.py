"""Interface web locale de therightbibtex : API JSON + page statique (static/),
servie par « therightbibtex serve » sur 127.0.0.1. Une seule personne par
installation : pas de compte, les réglages et les clés d'API sont dans config.json,
les favoris dans library.db (voir config.py)."""
from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
import os
import re
from collections import defaultdict
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
import httpx
from starlette.datastructures import Headers, MutableHeaders

from . import __version__, bibtex, config, engine, keycheck, pdfinfo, store
from . import query as query_mod
from .sources import openalex, s2, scholar
from .sources.http import Http, SourceError

logger = logging.getLogger("therightbibtex")

MAX_PDF = 40 * 1024 * 1024
MAX_QUERY = 20_000
LIST_LIMIT = 5000            # favoris affichés d'un coup (l'export, lui, est complet)
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
mimetypes.add_type("font/woff2", ".woff2")
mimetypes.add_type("application/manifest+json", ".webmanifest")
# Noms d'hôte acceptés (protection contre le DNS rebinding : une page web malveillante
# qui ferait pointer son propre domaine vers 127.0.0.1 ne lit pas la bibliothèque).
# None : tous, quand le serveur écoute volontairement sur le réseau (serve --host).
LOCAL_HOSTS = {"localhost", "127.0.0.1", "[::1]"}
allowed_hosts: set[str] | None = set(LOCAL_HOSTS)

http: Http | None = None
_global_slots = asyncio.Semaphore(4)
_active: dict[int, int] = defaultdict(int)


async def _housekeeping():
    """Une fois par jour : corbeille, copie de la base, cache HTTP."""
    while True:
        try:
            await asyncio.to_thread(store.housekeeping)
            await asyncio.to_thread(http.cache.purge)
        except Exception:
            logger.exception("Entretien quotidien en échec")
        await asyncio.sleep(86400)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global http
    store.init()
    http = Http()
    chores = asyncio.create_task(_housekeeping())
    yield
    chores.cancel()
    await http.close()


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
    # Tout vient du serveur lui-même (polices comprises) ; aucun script ni style en ligne.
    "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; font-src 'self'; "
                               "img-src 'self' data:; connect-src 'self'; manifest-src 'self'; "
                               "object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'",
}
# Les SVG statiques (favicon) n'ont ni script ni ressource externe ; seul leur <style>
# interne, qui suit le thème clair/sombre du système, doit être permis.
SVG_CSP = "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"


class SecurityHeaders:
    """ASGI pur : BaseHTTPMiddleware perturbe les réponses en flux."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        svg = scope["path"].startswith("/static/") and scope["path"].endswith(".svg")

        async def wrapped(message):
            if message["type"] == "http.response.start":
                h = MutableHeaders(scope=message)
                for k, v in SECURITY_HEADERS.items():
                    h[k] = v
                if svg:
                    h["Content-Security-Policy"] = SVG_CSP
            await send(message)

        await self.app(scope, receive, wrapped)


def host_name(host_header: str) -> str:
    """« localhost:8137 » -> « localhost », « [::1]:8137 » -> « [::1] »."""
    host = host_header.strip().lower()
    if host.startswith("["):
        return host.split("]", 1)[0] + "]"
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


class LocalOnly:
    """Refuse les requêtes adressées à un autre nom d'hôte que la machine elle-même."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and allowed_hosts is not None:
            if host_name(Headers(scope=scope).get("host", "")) not in allowed_hosts:
                return await JSONResponse({"error": "Unknown host"}, 421)(scope, receive, send)
        await self.app(scope, receive, send)


class SameOrigin:
    """Refuse les requêtes qui modifient quelque chose quand elles viennent d'un autre
    site (une page web ouverte dans le même navigateur peut viser 127.0.0.1).
    Sec-Fetch-Site est posé par le navigateur lui-même ; à défaut, on compare Origin à
    l'hôte demandé. Les clients hors navigateur (scripts) n'envoient ni l'un ni l'autre."""

    SAFE = {"GET", "HEAD", "OPTIONS"}

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope["method"] not in self.SAFE:
            h = Headers(scope=scope)
            site, origin = h.get("sec-fetch-site"), h.get("origin")
            if site is not None:
                refused = site not in ("same-origin", "none")
            else:
                refused = origin is not None and urlparse(origin).netloc.lower() != h.get("host", "").lower()
            if refused:
                return await JSONResponse({"error": "Request from another site refused"}, 403)(scope, receive, send)
        await self.app(scope, receive, send)


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(SameOrigin)
app.add_middleware(LocalOnly)
app.add_middleware(SecurityHeaders)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ================================================================== helpers

def _err(msg: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": msg}, status)


def _busy(msg: str, wait: int) -> JSONResponse:
    """429 avec le délai d'attente (le mode lot s'en sert pour patienter)."""
    return JSONResponse({"error": msg, "retry_after": wait}, 429, headers={"Retry-After": str(wait)})


async def _body(request: Request) -> dict:
    try:
        b = await request.json()
    except Exception:
        return {}
    return b if isinstance(b, dict) else {}


def _folder_arg(v):
    """None (sans dossier) ou id entier."""
    if v in (None, "", "inbox", "null", 0, "0"):
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return "invalid"


# ================================================================== pages

def _asset_version(name: str) -> str:
    import hashlib
    try:
        with open(os.path.join(STATIC_DIR, name), "rb") as f:
            return hashlib.sha1(f.read()).hexdigest()[:10]
    except OSError:
        return "0"


@app.get("/")
async def index():
    """La page est servie sans cache ; ses scripts/styles portent l'empreinte de leur
    contenu, pour qu'une mise à jour du paquet ne reste pas masquée par le cache."""
    with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as f:
        page = f.read()
    for asset in ("style.css", "app.js", "prefs.js", "favicon.svg", "icons/apple-touch-icon.png", "manifest.webmanifest"):
        page = page.replace(f"/static/{asset}?v=1", f"/static/{asset}?v={_asset_version(asset)}")
    return Response(page, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-store"})


@app.get("/healthz")
async def healthz():
    return {"ok": True, "version": __version__}


@app.get("/api/me")
async def me():
    return {"authenticated": True, "settings": config.get_settings(), "version": __version__,
            "home": str(config.home())}


@app.get("/api/settings")
async def get_settings():
    return config.get_settings()


@app.put("/api/settings")
async def put_settings(request: Request):
    return config.save_settings(await _body(request))


# ================================================================== recherche

async def _read_pdf(upload) -> bytes:
    chunks, size = [], 0
    while True:
        chunk = await upload.read(1024 * 1024)
        if not chunk:
            break
        size += len(chunk)
        if size > MAX_PDF:
            raise ValueError("PDF too large (40 MB max)")
        chunks.append(chunk)
    data = b"".join(chunks)
    if not data.startswith(b"%PDF-"):
        raise ValueError("This file is not a PDF")
    return data


@app.post("/api/search")
async def search(request: Request):
    uid = store.LOCAL_UID
    if _active[uid] >= 2:
        return _busy("Two searches are already running", 3)
    ctype = request.headers.get("content-type", "")
    refresh, filename = False, ""
    try:
        if ctype.startswith("multipart/form-data"):
            form = await request.form()
            refresh = form.get("refresh") in ("1", "true")
            upload = form.get("pdf")
            raw = str(form.get("q") or "")[:MAX_QUERY]
            if upload is not None and hasattr(upload, "read"):
                filename = getattr(upload, "filename", "") or "document.pdf"
                data = await _read_pdf(upload)
                info = await asyncio.to_thread(pdfinfo.extract_isolated, data)
                q = engine.query_from_pdf(info, filename)
                if not (q.title or q.doi or q.arxiv):
                    return _err("Couldn't read a title, DOI or arXiv ID in this PDF")
            else:
                q = query_mod.parse_input(raw)
        else:
            b = await _body(request)
            raw = str(b.get("q", ""))[:MAX_QUERY]
            refresh = bool(b.get("refresh"))
            q = query_mod.parse_input(raw)
    except ValueError as e:
        return _err(str(e))
    if not (q.title or q.text or q.doi or q.arxiv or q.ids or q.url):
        return _err("Empty query")
    s = engine.Search(http, q, refresh=refresh, settings=config.get_settings(), keys=config.get_api_keys(),
                      scholar_direct=config.get_option("scholar_direct"))

    async def gen():
        _active[uid] += 1
        top_title = ""
        try:
            async with _global_slots:
                async for ev in s.stream():
                    if ev["type"] == "results" and ev.get("works"):
                        top_title = ev["works"][0]["title"]
                    yield (json.dumps(ev, ensure_ascii=False) + "\n").encode()
                    if await request.is_disconnected():
                        break
        finally:
            _active[uid] -= 1
            history_q = filename and f"PDF: {filename}" or (q.raw if q.kind != "pdf" else "")
            if history_q and q.kind != "bibtex":
                await asyncio.to_thread(store.add_history, uid, history_q[:1000], q.kind, top_title)

    return StreamingResponse(gen(), media_type="application/x-ndjson",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@app.post("/api/render")
async def render(request: Request):
    """Réécrit une notice fusionnée avec d'autres réglages (sans relancer la recherche)."""
    b = await _body(request)
    specs = b.get("specs")
    settings = b.get("settings") or config.get_settings()
    if not isinstance(specs, list) or len(specs) > 200:
        return _err("Invalid request")
    out = []
    for spec in specs:
        if not isinstance(spec, dict):
            out.append(None)
            continue
        try:
            out.append(bibtex.render(spec, settings))
        except Exception:
            out.append(None)
    return {"results": out}


@app.get("/api/status")
async def status():
    keys = config.get_api_keys()
    br = {k: {"open": v.is_open(), "reason": v.reason} for k, v in http.breakers.items()}
    scholar_mode = "SerpAPI" if keys.get("serpapi") else "direct access" if config.get_option("scholar_direct") else "off"
    return {"s2_keys": [s2.key_status(k) for k in keys.get("s2", [])], "scholar": scholar_mode, "breakers": br}


# ================================================================== clés d'API (config.json)

async def _check_key(service: str, value: str) -> str:
    return await keycheck.check_key(http.client, service, value)


async def _key_state(service: str, value: str) -> dict:
    """État d'une clé pour les réglages : recherches restantes (SerpAPI), budget du
    jour (OpenAlex) ou refus."""
    info = {}
    if service == "s2" and s2.key_status(value) != "ok":
        info["status"] = "rejected by Semantic Scholar (403)"
    if service == "serpapi":
        try:
            data = await http.get_json("scholar_quota", f"{scholar.SERPAPI}/account.json",
                                       params={"api_key": value}, ttl=600, timeout=8)
            info["quota"] = data.get("total_searches_left")
        except SourceError:
            info["status"] = "quota unavailable for now"
    if service == "openalex":
        if value in openalex._rejected:
            info["status"] = "rejected by OpenAlex"
        else:
            # Budget du jour (en dollars, remis à zéro à minuit UTC) lu dans les en-têtes.
            try:
                r = await http.client.get(f"{openalex.API}/W2626778328", params={"select": "id", "api_key": value},
                                          timeout=8)
                if r.status_code in (401, 403):
                    info["status"] = "rejected by OpenAlex"
                elif r.headers.get("x-ratelimit-remaining-usd"):
                    info["budget"] = float(r.headers["x-ratelimit-remaining-usd"])
            except (httpx.HTTPError, ValueError):
                info["status"] = "budget unavailable for now"
    return info


async def _keys_view() -> dict:
    """Clés de chaque service, dans l'ordre où elles sont essayées (jamais en clair)."""
    keys, public = config.get_api_keys(), config.api_keys_public()
    out = {}
    for service in config.API_SERVICES:
        values, infos = keys.get(service, []), public.get(service, [])
        states = await asyncio.gather(*(_key_state(service, v) for v in values))
        out[service] = [{**info, **state} for info, state in zip(infos, states)]
    return {"keys": out, "max": config.MAX_KEYS_PER_SERVICE}


@app.get("/api/keys")
async def get_keys():
    return await _keys_view()


@app.post("/api/keys/{service}")
async def add_key(service: str, request: Request):
    """Ajoute une clé après celles du service : elle servira si les précédentes échouent."""
    if service not in config.API_SERVICES:
        return _err("Unknown service", 404)
    value = str((await _body(request)).get("value", "")).strip()
    err = config.key_error(service, value) or await _check_key(service, value) or config.add_api_key(service, value)
    if err:
        return _err(err)
    s2._rejected.discard(value)
    openalex._rejected.discard(value)
    return await _keys_view()


@app.delete("/api/keys/{service}/{key_id}")
async def delete_key(service: str, key_id: int):
    if not config.delete_api_key(service, key_id):
        return _err("Key not found", 404)
    return await _keys_view()


# ================================================================== favoris

@app.get("/api/folders")
async def folders(request: Request):
    uid = store.LOCAL_UID
    return store.list_folders(uid)


@app.post("/api/folders")
async def create_folder(request: Request):
    uid = store.LOCAL_UID
    b = await _body(request)
    parent = _folder_arg(b.get("parent_id"))
    if parent == "invalid":
        return _err("Invalid parent folder")
    color = b.get("color")
    res = store.create_folder(uid, b.get("name", ""), parent, color if isinstance(color, str) else None)
    return _err(res) if isinstance(res, str) else {"id": res}


@app.patch("/api/folders/{fid}")
async def update_folder(fid: int, request: Request):
    uid = store.LOCAL_UID
    b = await _body(request)
    parent = "keep"
    if "parent_id" in b:
        parent = _folder_arg(b.get("parent_id"))
        if parent == "invalid":
            return _err("Invalid parent folder")
    color = b.get("color")
    err = store.update_folder(uid, fid, b.get("name"), parent, color if isinstance(color, str) else None)
    return _err(err) if err else {"ok": True}


@app.delete("/api/folders/{fid}")
async def delete_folder(fid: int, request: Request):
    uid = store.LOCAL_UID
    token = store.delete_folder(uid, fid)
    return {"undo": token} if token else _err("Folder not found", 404)


@app.post("/api/undo")
async def undo(request: Request):
    uid = store.LOCAL_UID
    token = str((await _body(request)).get("token", ""))
    return {"restored": store.undo(uid, token)} if token else _err("Missing token")


@app.get("/api/favorites")
async def favorites(request: Request, folder: str = "all", q: str = "", recursive: int = 1):
    uid = store.LOCAL_UID
    if folder not in ("all", "inbox") and not folder.isdigit():
        return _err("Invalid folder")
    rows = store.list_favorites(uid, folder, q.strip()[:200], bool(recursive), limit=LIST_LIMIT + 1)
    return {"favorites": rows[:LIST_LIMIT], "truncated": len(rows) > LIST_LIMIT}


@app.post("/api/favorites")
async def add_favorite(request: Request):
    uid = store.LOCAL_UID
    b = await _body(request)
    folder = _folder_arg(b.get("folder_id"))
    if folder == "invalid":
        return _err("Invalid folder")
    res = store.add_favorite(uid, folder, str(b.get("bibtex", "")), str(b.get("note", "")), bool(b.get("force")))
    if res.get("error"):
        return _err(res["error"])
    return res


@app.patch("/api/favorites/{fav_id}")
async def update_favorite(fav_id: int, request: Request):
    uid = store.LOCAL_UID
    b = await _body(request)
    folder = "keep"
    if "folder_id" in b:
        folder = _folder_arg(b.get("folder_id"))
        if folder == "invalid":
            return _err("Invalid folder")
    err = store.update_favorite(uid, fav_id, b.get("bibtex"), folder, b.get("note"))
    return _err(err) if err else {"ok": True}


@app.post("/api/favorites/bulk")
async def bulk(request: Request):
    uid = store.LOCAL_UID
    b = await _body(request)
    ids = [int(i) for i in b.get("ids", []) if str(i).isdigit()]
    if b.get("action") == "delete":
        return {"undo": store.delete_favorites(uid, ids)}
    if b.get("action") == "move":
        folder = _folder_arg(b.get("folder_id"))
        if folder == "invalid":
            return _err("Invalid folder")
        err = store.move_favorites(uid, ids, folder)
        return _err(err) if err else {"ok": True}
    return _err("Unknown action")


@app.delete("/api/favorites/{fav_id}")
async def delete_favorite(fav_id: int, request: Request):
    uid = store.LOCAL_UID
    return {"undo": store.delete_favorites(uid, [fav_id])}


def _slug(name: str) -> str:
    import unicodedata
    ascii_name = "".join(c for c in unicodedata.normalize("NFKD", name) if not unicodedata.combining(c))
    return re.sub(r"[^A-Za-z0-9._-]+", "-", ascii_name).strip("-")[:60] or "favorites"


def _disposition(name: str, ext: str = "bib") -> str:
    from urllib.parse import quote
    return f"attachment; filename=\"{_slug(name)}.{ext}\"; filename*=UTF-8''{quote(name[:60] or 'favorites')}.{ext}"


@app.get("/api/export")
async def export(request: Request, folder: str = "all", recursive: int = 1, ids: str = "", format: str = "bib"):
    """Export complet (aucune limite d'affichage) en BibTeX, ou en CSL-JSON
    (format=csl) pour Zotero, Pandoc ou un traitement de texte."""
    uid = store.LOCAL_UID
    if ids:
        rows = store.list_favorites(uid, ids=[int(i) for i in ids.split(",") if i.isdigit()][:store.MAX_FAVORITES])
        name = "selection"
    else:
        if folder not in ("all", "inbox") and not folder.isdigit():
            return _err("Invalid folder")
        rows = store.list_favorites(uid, folder, "", bool(recursive))
        name = "favorites" if folder == "all" else "unfiled" if folder == "inbox" else next(
            (f["name"] for f in store.list_folders(uid)["folders"] if str(f["id"]) == folder), "folder")
    if format == "csl":
        return Response(json.dumps(store.export_csl(rows), ensure_ascii=False, indent=2),
                        media_type="application/vnd.citationstyles.csl+json; charset=utf-8",
                        headers={"Content-Disposition": _disposition(name, "json")})
    return Response(store.export_bib(rows), media_type="application/x-bibtex; charset=utf-8",
                    headers={"Content-Disposition": _disposition(name)})


@app.post("/api/import")
async def import_bib(request: Request):
    uid = store.LOCAL_UID
    b = await _body(request)
    folder = _folder_arg(b.get("folder_id"))
    if folder == "invalid":
        return _err("Invalid folder")
    text = str(b.get("bibtex", ""))
    if len(text) > 5_000_000:
        return _err("File too large")
    return await asyncio.to_thread(store.import_bib, uid, folder, text)


@app.get("/api/history")
async def history(request: Request):
    uid = store.LOCAL_UID
    return {"history": store.list_history(uid)}


@app.delete("/api/history")
async def clear_history(request: Request, id: int = 0):
    uid = store.LOCAL_UID
    store.clear_history(uid, id or None)
    return {"ok": True}


def run(host: str = "127.0.0.1", port: int = config.DEFAULT_PORT, log_level: str = "warning") -> None:
    """Lance le serveur local (therightbibtex serve). Écouter sur le réseau (0.0.0.0,
    une adresse du réseau local) lève la protection par nom d'hôte : la bibliothèque
    devient lisible par les autres machines, sans mot de passe."""
    global allowed_hosts
    import uvicorn
    allowed_hosts = set(LOCAL_HOSTS) if host in ("127.0.0.1", "localhost", "::1") else None
    uvicorn.run(app, host=host, port=port, proxy_headers=False, log_level=log_level)
