"""Configuration locale : un dossier par utilisateur, qui contient config.json
(réglages BibTeX, clés d'API, options), la bibliothèque de favoris (library.db)
et le cache HTTP (cache.db).

Emplacement : le dossier therightbibtex dans %APPDATA% (Windows), dans
~/Library/Application Support (macOS) ou dans $XDG_CONFIG_HOME, par défaut
~/.config (Linux). La variable THERIGHTBIBTEX_HOME le remplace (installation
portable, tests)."""
from __future__ import annotations

import json
import os
import re
import sys
import threading
from pathlib import Path

from . import bibtex

APP = "therightbibtex"
# SerpAPI (Google Scholar sans captcha), Semantic Scholar (1 requête/s au lieu du pool
# public) et OpenAlex (budget quotidien propre au lieu de celui de l'adresse IP).
API_SERVICES = ("serpapi", "s2", "openalex")
API_KEY_RE = re.compile(r"[A-Za-z0-9_.\-]{8,200}")
MAX_KEYS_PER_SERVICE = 5
DEFAULT_PORT = 8137
# Options en dehors des réglages BibTeX, avec leur type.
OPTIONS = {
    "contact_email": str,     # transmis à Crossref et OpenAlex (pool « poli », plus rapide)
    "scholar_direct": bool,   # lire Google Scholar sans SerpAPI (souvent bloqué, désactivé par défaut)
    "port": int,              # port du serveur local (therightbibtex serve)
}
_DEFAULTS = {"settings": {}, "keys": {}, "contact_email": "", "scholar_direct": False, "port": DEFAULT_PORT}
_lock = threading.Lock()


class ConfigError(ValueError):
    pass


def home() -> Path:
    env = os.getenv("THERIGHTBIBTEX_HOME", "").strip()
    if env:
        return Path(env).expanduser()
    if sys.platform == "win32":
        base = Path(os.getenv("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.getenv("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / APP


def path() -> Path:
    return home() / "config.json"


def load() -> dict:
    try:
        data = json.loads(path().read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError) as e:
        raise ConfigError(f"Unreadable config file {path()}: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError(f"Unreadable config file {path()}: not a JSON object")
    out = {**_DEFAULTS, **data}
    keys = out.get("keys") if isinstance(out.get("keys"), dict) else {}
    out["keys"] = {s: [k for k in v if isinstance(k, str) and k] for s, v in keys.items()
                   if s in API_SERVICES and isinstance(v, list)}
    for name, kind in OPTIONS.items():
        if not isinstance(out.get(name), kind):
            out[name] = _DEFAULTS[name]
    return out


def save(cfg: dict) -> None:
    """Écriture atomique ; le fichier contient des clés d'API, il n'est lisible que par
    son propriétaire là où le système le permet."""
    p = path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        os.chmod(tmp, 0o600)
    except OSError:
        pass
    os.replace(tmp, p)


def _update(change) -> dict:
    with _lock:
        cfg = load()
        change(cfg)
        save(cfg)
        return cfg


# ----------------------------------------------------------------- réglages BibTeX

def get_settings() -> dict:
    return bibtex.merged_settings(load().get("settings") or {})


def save_settings(data: dict) -> dict:
    """Les champs reçus remplacent les anciens ; les autres sont gardés (la fenêtre des
    réglages et la poignée de la notice enregistrent chacune les siens)."""
    merged = bibtex.merged_settings({**get_settings(), **(data if isinstance(data, dict) else {})})
    _update(lambda cfg: cfg.__setitem__("settings", merged))
    return merged


# ----------------------------------------------------------------- options

def get_option(name: str):
    return load()[name]


def set_option(name: str, value) -> None:
    kind = OPTIONS.get(name)
    if kind is None:
        raise ConfigError(f"Unknown option: {name}")
    if not isinstance(value, kind) or (kind is int and isinstance(value, bool)):
        raise ConfigError(f"{name} expects {kind.__name__}")
    _update(lambda cfg: cfg.__setitem__(name, value))


# ----------------------------------------------------------------- clés d'API

def get_api_keys() -> dict[str, list[str]]:
    """Clés de chaque service, dans l'ordre où elles seront essayées."""
    return {s: list(v) for s, v in load()["keys"].items() if v}


def key_error(service: str, value: str) -> str | None:
    if service not in API_SERVICES:
        return "Unknown service"
    if not API_KEY_RE.fullmatch(value):
        return "Invalid key (letters, digits, _ . -; 8 to 200 characters)"
    return None


def add_api_key(service: str, value: str) -> str | None:
    """Ajoute une clé après celles du service (déjà là : rien ne change). Renvoie une erreur."""
    value = (value or "").strip()
    err = key_error(service, value)
    if err:
        return err
    known = load()["keys"].get(service, [])
    if value in known:
        return None
    if len(known) >= MAX_KEYS_PER_SERVICE:
        return f"At most {MAX_KEYS_PER_SERVICE} keys per service"
    _update(lambda cfg: cfg["keys"].setdefault(service, []).append(value))
    return None


def delete_api_key(service: str, index: int) -> bool:
    keys = load()["keys"].get(service, [])
    if not 0 <= index < len(keys):
        return False
    _update(lambda cfg: cfg["keys"][service].pop(index))
    return True


def api_keys_public() -> dict[str, list[dict]]:
    """Ce que l'interface peut voir des clés : leurs 4 derniers caractères."""
    return {s: [{"id": i, "hint": "…" + k[-4:]} for i, k in enumerate(v)] for s, v in load()["keys"].items()}
