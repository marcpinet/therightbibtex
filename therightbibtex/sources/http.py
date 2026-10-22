"""Client HTTP partagé par toutes les sources : cache SQLite, cadence par
source (chaque API a ses limites), disjoncteurs, erreurs typées."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from .. import __version__, config

logger = logging.getLogger(__name__)

CACHE_PATH = config.home() / "cache.db"

# Adresse de contact facultative (config.json) : Crossref et OpenAlex servent plus vite
# les clients qui se présentent (« pool poli »).
try:
    CONTACT = config.get_option("contact_email").strip()
except config.ConfigError:
    CONTACT = ""
API_UA = (f"therightbibtex/{__version__} (+https://github.com/marcpinet/therightbibtex"
          + (f"; mailto:{CONTACT}" if CONTACT else "") + ")")
BROWSER_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/126.0 Safari/537.36")

# Intervalle minimal entre deux requêtes vers une même source (secondes).
MIN_INTERVAL = {
    "arxiv": 3.0,          # demandé par arXiv
    "s2": 1.05,            # 1 req/s avec clé
    "s2_public": 3.0,
    "scholar": 4.0,
    "dblp": 0.3,
    "openreview": 0.25,
    "inspire": 0.35,
    "zbmath": 0.5,
    "pages": 0.3,
}
DEFAULT_INTERVAL = 0.05
# Requêtes simultanées max par source (au-delà, file d'attente).
MAX_PARALLEL = {"crossref": 4, "doi": 4, "openalex": 3, "pages": 4, "datacite": 3, "europepmc": 3, "hal": 3}
# Un hôte injoignable doit échouer vite, quel que soit le délai de lecture accordé.
CONNECT_TIMEOUT = 5.0
# Un appel abandonné par le moteur (budget épuisé) après ce délai compte comme un
# échec : une source qui répond toujours trop tard finit coupée par son disjoncteur.
SLOW_CANCEL = 8.0
# Clé d'API passée dans l'URL (SerpAPI) : masquée avant d'écrire l'URL dans le cache.
_SECRET_PARAM_RE = re.compile(r"(?<=[?&])(api_key|apikey)=[^&]*")


def redact(url: str) -> str:
    return _SECRET_PARAM_RE.sub(r"\1=***", url)


class SourceError(Exception):
    """kind : timeout | http | blocked | ratelimited | parse | unavailable ;
    status : code HTTP de la réponse, s'il y en a une."""

    def __init__(self, kind: str, message: str = "", status: int | None = None):
        super().__init__(message or kind)
        self.kind = kind
        self.message = message or kind
        self.status = status


@dataclass
class Resp:
    status: int
    text: str
    url: str
    headers: dict
    cached: bool = False

    def json(self):
        return json.loads(self.text)


class _Cache:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("""CREATE TABLE IF NOT EXISTS http_cache (
            key TEXT PRIMARY KEY, status INTEGER, url TEXT, body BLOB, headers TEXT,
            fetched REAL, expires REAL)""")
        self.lock = threading.Lock()

    def get(self, key: str) -> Resp | None:
        with self.lock:
            row = self.conn.execute(
                "SELECT status, url, body, headers, expires FROM http_cache WHERE key=?", (key,)
            ).fetchone()
        if not row or row[4] < time.time():
            return None
        return Resp(row[0], row[2].decode("utf-8", "replace") if isinstance(row[2], bytes) else row[2],
                    row[1], json.loads(row[3] or "{}"), cached=True)

    def put(self, key: str, resp: Resp, ttl: float):
        now = time.time()
        with self.lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO http_cache VALUES (?,?,?,?,?,?,?)",
                (key, resp.status, resp.url, resp.text.encode("utf-8"),
                 json.dumps({k: v for k, v in resp.headers.items() if k in ("content-type",)}), now, now + ttl),
            )

    def purge(self):
        with self.lock:
            self.conn.execute("DELETE FROM http_cache WHERE expires < ?", (time.time(),))


class _Breaker:
    def __init__(self, threshold: int = 3, cooldown: float = 600):
        self.threshold, self.cooldown = threshold, cooldown
        self.failures = 0
        self.open_until = 0.0
        self.reason = ""

    def is_open(self) -> bool:
        if self.open_until and time.time() < self.open_until:
            return True
        if self.open_until:
            self.open_until, self.failures = 0.0, 0
        return False

    def fail(self, reason: str, hard: bool = False, cooldown: float | None = None):
        self.failures += 1
        self.reason = reason
        if hard or self.failures >= self.threshold:
            self.open_until = time.time() + (cooldown or self.cooldown)
            logger.warning("Source coupée %ss : %s", int(cooldown or self.cooldown), reason)

    def ok(self):
        self.failures = 0


class Http:
    def __init__(self):
        self.client = httpx.AsyncClient(
            timeout=httpx.Timeout(12.0, connect=6.0),
            follow_redirects=True,
            headers={"User-Agent": API_UA, "Accept-Encoding": "gzip, deflate"},
            limits=httpx.Limits(max_connections=40, max_keepalive_connections=20),
        )
        self.cache = _Cache(CACHE_PATH)
        self._last: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._sems: dict[str, asyncio.Semaphore] = {}
        self.breakers: dict[str, _Breaker] = {}

    def breaker(self, source: str) -> _Breaker:
        if source not in self.breakers:
            self.breakers[source] = _Breaker()
        return self.breakers[source]

    async def _pace(self, source: str):
        interval = MIN_INTERVAL.get(source, DEFAULT_INTERVAL)
        lock = self._locks.setdefault(source, asyncio.Lock())
        async with lock:
            wait = self._last.get(source, 0) + interval - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
            self._last[source] = time.monotonic()

    @staticmethod
    def cache_key(method: str, url: str, params, headers, data) -> str:
        h = hashlib.sha256()
        h.update(method.encode())
        h.update(url.encode())
        h.update(json.dumps(params or {}, sort_keys=True, default=str).encode())
        h.update(json.dumps({k.lower(): v for k, v in (headers or {}).items()
                             if k.lower() in ("accept",)}, sort_keys=True).encode())
        h.update(json.dumps(data or {}, sort_keys=True, default=str).encode())
        return h.hexdigest()

    async def request(
        self, source: str, url: str, *, params=None, headers=None, data=None, method: str = "GET",
        ttl: float = 86400, refresh: bool = False, timeout: float | None = None,
        ok_status=(200,), cache_status=(200,), neg_ttl: float = 0, pace_key: str | None = None,
    ) -> Resp:
        key = self.cache_key(method, url, params, headers, data)
        if not refresh:
            hit = self.cache.get(key)
            if hit is not None:
                if hit.status not in ok_status:
                    raise SourceError("http", f"HTTP {hit.status} (cache)", status=hit.status)
                return hit
        br = self.breaker(pace_key or source)
        if br.is_open():
            raise SourceError("unavailable", br.reason or "source temporarily disabled")
        sem = self._sems.setdefault(source, asyncio.Semaphore(MAX_PARALLEL.get(source, 2)))
        async with sem:
            await self._pace(pace_key or source)
            t = timeout or 12.0
            started = time.monotonic()
            try:
                r = await self.client.request(
                    method, url, params=params, headers=headers, data=data,
                    timeout=httpx.Timeout(t, connect=min(CONNECT_TIMEOUT, t)),
                )
            except httpx.TimeoutException as e:
                br.fail("timed out")
                raise SourceError("timeout", "could not connect" if isinstance(e, httpx.ConnectTimeout)
                                  else "timed out")
            except httpx.HTTPError as e:
                br.fail(type(e).__name__)
                raise SourceError("http", f"network error ({type(e).__name__})")
            except asyncio.CancelledError:
                if time.monotonic() - started >= SLOW_CANCEL:
                    br.fail("too slow (abandoned)")
                raise
        resp = Resp(r.status_code, r.text, redact(str(r.url)), {k.lower(): v for k, v in r.headers.items()})
        if r.status_code == 429:
            br.fail("rate limited (429)", cooldown=60)
            raise SourceError("ratelimited", "rate limited", status=429)
        if r.status_code in ok_status:
            br.ok()
            if r.status_code in cache_status:
                self.cache.put(key, resp, ttl)
            return resp
        if neg_ttl and r.status_code == 404:
            self.cache.put(key, resp, neg_ttl)
        if r.status_code >= 500:
            br.fail(f"HTTP {r.status_code}")
        raise SourceError("http", f"HTTP {r.status_code}", status=r.status_code)

    async def get_json(self, source: str, url: str, **kw):
        r = await self.request(source, url, **kw)
        try:
            return r.json()
        except ValueError:
            raise SourceError("parse", "unreadable JSON response")

    async def close(self):
        await self.client.aclose()
