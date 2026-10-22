"""Enregistrement et rejeu des réponses HTTP des sources, pour tester le moteur
hors ligne et de façon reproductible (la CI rejoue ces tests à chaque modification).

Un jeu d'essai (tests/fixtures/<nom>.json.gz) contient la requête et, pour chaque
appel HTTP de la recherche, la réponse ou l'erreur obtenue. La clé d'un appel est
celle du cache HTTP, sans le paramètre « mailto » (contact_email varie selon la
configuration)."""
from __future__ import annotations

import gzip
import json
import re
from pathlib import Path

from therightbibtex.sources.http import Http, Resp, SourceError

FIXTURES = Path(__file__).resolve().parent / "fixtures"
_VALUES_RE = re.compile(r"(VALUES \?\w+ \{ )(.*?)( \})", re.S)
_ITEM_RE = re.compile(r'<[^>]*>|"(?:[^"\\]|\\.)*"')


def _canonical_sparql(query: str) -> str:
    """Valeurs d'un bloc VALUES triées : l'ordre des variantes n'a pas de sens."""
    return _VALUES_RE.sub(lambda m: m.group(1) + " ".join(sorted(_ITEM_RE.findall(m.group(2)))) + m.group(3), query)


def replay_key(method: str, url: str, params, headers, data) -> str:
    params = {k: v for k, v in (params or {}).items() if k != "mailto"}
    if "query" in params and "sparql" in url:
        params["query"] = _canonical_sparql(params["query"])
    return Http.cache_key(method, url, params, headers, data)


def load(name: str) -> dict:
    with gzip.open(FIXTURES / f"{name}.json.gz", "rt", encoding="utf-8") as f:
        return json.load(f)


def save(name: str, query: str, responses: dict):
    FIXTURES.mkdir(exist_ok=True)
    with gzip.open(FIXTURES / f"{name}.json.gz", "wt", encoding="utf-8", compresslevel=9) as f:
        json.dump({"query": query, "responses": responses}, f, ensure_ascii=False, sort_keys=True)


class ReplayHttp(Http):
    """Ne touche jamais au réseau : un appel absent du jeu d'essai échoue."""

    def __init__(self, responses: dict):
        super().__init__()
        self.responses = responses
        self.missing: list[tuple[str, str]] = []

    async def request(self, source, url, *, params=None, headers=None, data=None, method="GET",
                      ok_status=(200,), **_kw) -> Resp:
        hit = self.responses.get(replay_key(method, url, params, headers, data))
        if hit is None:
            self.missing.append((source, url))
            raise SourceError("unavailable", "absent du jeu d'essai")
        if "error" in hit:
            kind, message, status = hit["error"]
            raise SourceError(kind, message, status=status)
        if hit["status"] not in ok_status:
            raise SourceError("http", f"HTTP {hit['status']}", status=hit["status"])
        return Resp(hit["status"], hit["text"], hit["url"], hit["headers"])


class RecordingHttp(Http):
    """Client réel (cache compris) qui note chaque réponse. Google Scholar n'est
    pas enregistré (pages de Google, et source de toute façon bloquée)."""

    def __init__(self):
        super().__init__()
        self.log: dict[str, dict] = {}

    async def request(self, source, url, *, params=None, headers=None, data=None, method="GET", **kw) -> Resp:
        key = replay_key(method, url, params, headers, data)
        if source.startswith("scholar"):
            self.log[key] = {"error": ["blocked", "non enregistré", None]}
            raise SourceError("blocked", "non enregistré")
        try:
            r = await super().request(source, url, params=params, headers=headers, data=data, method=method, **kw)
        except SourceError as e:
            self.log[key] = {"error": [e.kind, e.message, e.status]}
            raise
        self.log[key] = {"status": r.status, "text": r.text, "url": r.url, "headers": r.headers}
        return r
