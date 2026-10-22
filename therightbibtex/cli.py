"""Ligne de commande : therightbibtex « titre, DOI, arXiv, lien, référence ou PDF »
imprime le BibTeX de la version à citer ; serve ouvre l'interface web locale ;
config et keys gèrent config.json. Le BibTeX va sur la sortie standard, tout le
reste (progression, avertissements) sur la sortie d'erreur : la commande se
redirige telle quelle vers un fichier .bib."""
from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import logging
import sys
import threading
import time
import webbrowser
from pathlib import Path

from . import __version__, bibtex, config, engine, keycheck, pdfinfo
from . import query as query_mod
from .sources.http import Http

COMMANDS = ("search", "batch", "serve", "config", "keys")
CONFIDENCE_WORD = {"high": "high confidence", "medium": "check it: other papers look similar", "low": "uncertain match"}
EXIT_NOT_FOUND = 1


# ================================================================== sortie

class Out:
    """Messages sur la sortie d'erreur ; la ligne de progression n'est réécrite que sur
    un terminal (pas dans un fichier ni une CI)."""

    def __init__(self, quiet: bool = False):
        self.quiet = quiet
        self.tty = sys.stderr.isatty()
        self._progress = False

    def progress(self, text: str):
        if self.quiet or not self.tty:
            return
        width = 78
        sys.stderr.write("\r" + text[:width].ljust(width))
        sys.stderr.flush()
        self._progress = True

    def clear(self):
        if self._progress:
            sys.stderr.write("\r" + " " * 78 + "\r")
            sys.stderr.flush()
            self._progress = False

    def info(self, text: str):
        if not self.quiet:
            self.clear()
            sys.stdout.flush()
            print(text, file=sys.stderr)

    def warn(self, text: str):
        self.clear()
        sys.stdout.flush()
        print(f"warning: {text}", file=sys.stderr)


def _utf8_streams():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


# ================================================================== recherche

def _settings(overrides: list[str]) -> dict:
    """Réglages de config.json, plus ceux passés en -s nom=valeur pour cette commande."""
    s = config.get_settings()
    for item in overrides or []:
        name, sep, value = item.partition("=")
        if not sep:
            raise SystemExit(f"--setting expects name=value, got {item!r}")
        s.update({name.strip(): _parse_value(name.strip(), value.strip(), bibtex.DEFAULT_SETTINGS)})
    return bibtex.merged_settings(s)


def _parse_value(name: str, value: str, defaults: dict):
    if name not in defaults:
        raise SystemExit(f"Unknown setting: {name} (known: {', '.join(sorted(defaults))})")
    kind = type(defaults[name])
    if kind is bool:
        low = value.lower()
        if low not in ("1", "0", "true", "false", "yes", "no", "on", "off"):
            raise SystemExit(f"{name} expects true or false")
        return low in ("1", "true", "yes", "on")
    if kind is int:
        try:
            return int(value)
        except ValueError:
            raise SystemExit(f"{name} expects a number") from None
    return value


def _query_from(text: str) -> query_mod.Query:
    path = Path(text)
    if text.lower().endswith(".pdf") and path.is_file():
        data = path.read_bytes()
        if not data.startswith(b"%PDF-"):
            raise SystemExit(f"{text} is not a PDF")
        try:
            info = pdfinfo.extract_isolated(data)
        except ValueError as e:
            raise SystemExit(f"{text}: {e}") from None
        q = engine.query_from_pdf(info, path.name)
        if not (q.title or q.doi or q.arxiv):
            raise SystemExit(f"{text}: couldn't read a title, DOI or arXiv ID in this PDF")
        return q
    return query_mod.parse_input(text)


async def _search(http: Http, q, settings: dict, refresh: bool, on_event=None) -> list[dict]:
    s = engine.Search(http, q, refresh=refresh, settings=settings, keys=config.get_api_keys(),
                      scholar_direct=config.get_option("scholar_direct"))
    works: list[dict] = []
    async for ev in s.stream():
        if ev["type"] == "results":
            works = ev["works"]
        if on_event:
            on_event(ev, s)
    return works


def recommended(work: dict) -> dict:
    return next(v for v in work["versions"] if v["id"] == work["recommended"])


def _progress_line(s) -> str:
    states = [st["state"] for st in s.status.values() if st["state"] != "skipped"]
    done = sum(1 for x in states if x not in ("pending", "running"))
    return f"Searching {len(states)} sources... {done} answered"


def cmd_search(args, out: Out) -> int:
    text = " ".join(args.query).strip()
    if not text:
        raise SystemExit("Nothing to search: give a title, DOI, arXiv ID, link, reference or PDF")
    path = Path(text)
    if path.is_file() and not text.lower().endswith(".pdf"):
        args.file = text
        return cmd_batch(args, out)
    settings = _settings(args.setting)
    q = _query_from(text)

    async def go():
        http = Http()
        try:
            return await _search(http, q, settings, args.refresh,
                                 lambda ev, s: out.progress(_progress_line(s)) if ev["type"] == "source" else None)
        finally:
            await http.close()

    works = asyncio.run(go())
    out.clear()
    if args.json:
        print(json.dumps(works, ensure_ascii=False, indent=2, default=str))
        return 0 if works else EXIT_NOT_FOUND
    if not works:
        out.warn("no paper found: try the exact title, a DOI or the arXiv ID")
        return EXIT_NOT_FOUND
    w = works[0]
    v = recommended(w)
    shown = [x for x in w["versions"] if not x.get("suspicious")] if args.all else [v]
    for i, x in enumerate(shown):
        if args.all:
            tag = "recommended" if x["id"] == v["id"] else x["status"]
            print(f"% {x['label']} ({tag})")
        print(x["bibtex"])
        if i < len(shown) - 1:
            print()
    others = [x["label"] for x in w["versions"] if x["id"] != v["id"] and not x.get("suspicious")]
    out.info(f"{v['label']} ({v['status']}), {CONFIDENCE_WORD.get(w['confidence'], w['confidence'] or 'unrated')}"
             + (f"; other versions: {', '.join(others)} (--all shows them)" if others and not args.all else ""))
    if w.get("retracted"):
        out.warn("a version of this paper was retracted or withdrawn: check before citing it")
    if w["confidence"] != "high" and len(works) > 1:
        out.info("other candidates: " + "; ".join(f"{x['title'][:70]} ({x.get('year') or '?'})" for x in works[1:4]))
    return 0


# ================================================================== lots

def _batch_items(text: str) -> tuple[list[str], bool]:
    """Les entrées d'un fichier .bib, ou une recherche par ligne non vide."""
    entries = bibtex.parse(text)
    if entries:
        return [e.raw or bibtex.write_entry(e.type, e.key, list(e.fields.items())) for e in entries], True
    return [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")], False


def cmd_batch(args, out: Out) -> int:
    path = Path(args.file)
    text = sys.stdin.read() if args.file == "-" else path.read_text(encoding="utf-8", errors="replace")
    items, is_bib = _batch_items(text)
    if not items:
        raise SystemExit(f"{args.file}: no BibTeX entry and no reference to look up")
    settings = _settings(args.setting)
    keep_keys = is_bib and not getattr(args, "new_keys", False)
    results: list[str | None] = [None] * len(items)
    missing = 0

    async def go():
        http = Http()
        queue = list(enumerate(items))
        done = 0

        async def worker():
            nonlocal done, missing
            while queue:
                i, item = queue.pop(0)
                works = await _search(http, query_mod.parse_input(item), settings, args.refresh)
                done += 1
                if works and works[0]["confidence"] in ("high", "medium"):
                    v = recommended(works[0])
                    entry = v["bibtex"]
                    if keep_keys:
                        old = bibtex.parse_one(item)
                        if old and old.key:
                            entry = bibtex.set_key(entry, old.key)
                    results[i] = entry
                    note = "" if works[0]["confidence"] == "high" else " (check it)"
                    out.info(f"[{done}/{len(items)}] {v['label']}{note}: {works[0]['title'][:60]}")
                else:
                    missing += 1
                    results[i] = (item if is_bib else f"% not found: {item}")
                    out.info(f"[{done}/{len(items)}] not found, kept as is: {item.splitlines()[0][:60]}")

        try:
            await asyncio.gather(worker(), worker())
        finally:
            await http.close()

    asyncio.run(go())
    body = "\n\n".join(r for r in results if r) + "\n"
    if args.output:
        Path(args.output).write_text(body, encoding="utf-8")
        out.info(f"Wrote {len(items) - missing} entries to {args.output}" + (f", {missing} kept as they were" if missing else ""))
    else:
        sys.stdout.write(body)
    return 0 if missing < len(items) else EXIT_NOT_FOUND


# ================================================================== serveur

def cmd_serve(args, out: Out) -> int:
    from . import web
    port = args.port or config.get_option("port")
    url = f"http://{'localhost' if args.host in ('127.0.0.1', 'localhost') else args.host}:{port}"
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        out.warn(f"listening on {args.host}: anyone on your network can read and change your library")
    print(f"therightbibtex {__version__} running at {url} (Ctrl+C to stop)", file=sys.stderr)
    if not args.no_browser:
        threading.Thread(target=lambda: (time.sleep(1.2), webbrowser.open(url)), daemon=True).start()
    try:
        web.run(host=args.host, port=port, log_level="info" if args.verbose else "warning")
    except KeyboardInterrupt:
        pass
    return 0


# ================================================================== configuration

def _masked(cfg: dict) -> dict:
    out = dict(cfg)
    out["keys"] = {s: ["…" + k[-4:] for k in v] for s, v in cfg["keys"].items()}
    return out


def cmd_config(args, out: Out) -> int:
    action = args.action or "show"
    if action == "path":
        print(config.home())
        return 0
    if action == "show":
        print(f"# {config.path()}", file=sys.stderr)
        cfg = config.load()
        cfg["settings"] = config.get_settings()
        print(json.dumps(_masked(cfg), ensure_ascii=False, indent=2))
        return 0
    if not args.name:
        raise SystemExit(f"config {action} expects a setting name")
    name = args.name
    if action == "set":
        if args.value is None:
            raise SystemExit("config set expects a value")
        if name in config.OPTIONS:
            config.set_option(name, _parse_value(name, args.value, {k: config._DEFAULTS[k] for k in config.OPTIONS}))
        else:
            value = _parse_value(name, args.value, bibtex.DEFAULT_SETTINGS)
            saved = config.save_settings({name: value})
            if saved.get(name) != value:
                raise SystemExit(f"Invalid value for {name}: {args.value!r}")
        out.info(f"{name} = {args.value}")
        return 0
    if action == "unset":
        if name in config.OPTIONS:
            config.set_option(name, config._DEFAULTS[name])
        elif name in bibtex.DEFAULT_SETTINGS:
            config.save_settings({name: bibtex.DEFAULT_SETTINGS[name]})
        else:
            raise SystemExit(f"Unknown setting: {name}")
        out.info(f"{name} reset to its default")
        return 0
    raise SystemExit(f"Unknown config action: {action}")


def cmd_keys(args, out: Out) -> int:
    action = args.action or "list"
    if action == "list":
        public = config.api_keys_public()
        for service in config.API_SERVICES:
            keys = public.get(service, [])
            print(f"{service:<9} " + (", ".join(f"{k['id'] + 1}. {k['hint']}" for k in keys) if keys else "none"))
        return 0
    if args.service not in config.API_SERVICES:
        raise SystemExit(f"Service: {', '.join(config.API_SERVICES)}")
    if action == "add":
        value = args.key or (getpass.getpass(f"{args.service} key: ") if sys.stdin.isatty() else sys.stdin.readline())
        value = value.strip()
        err = config.key_error(args.service, value)
        if not err and not args.no_check:
            import httpx

            async def check():
                async with httpx.AsyncClient() as client:
                    return await keycheck.check_key(client, args.service, value)
            err = asyncio.run(check())
        err = err or config.add_api_key(args.service, value)
        if err:
            raise SystemExit(err)
        out.info(f"{args.service} key …{value[-4:]} added")
        return 0
    if action == "remove":
        try:
            index = int(args.key) - 1
        except (TypeError, ValueError):
            raise SystemExit("keys remove expects the key number shown by `therightbibtex keys`") from None
        if not config.delete_api_key(args.service, index):
            raise SystemExit("No such key")
        out.info(f"{args.service} key {index + 1} removed")
        return 0
    raise SystemExit(f"Unknown keys action: {action}")


# ================================================================== analyse des arguments

def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="therightbibtex",
        description="The right BibTeX for any paper, checked against every source.",
        epilog="Examples:\n"
               '  therightbibtex "Attention is all you need"\n'
               "  therightbibtex 2010.11929 --all\n"
               "  therightbibtex paper.pdf\n"
               "  therightbibtex refs.bib -o refs-checked.bib\n"
               "  therightbibtex serve",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"therightbibtex {__version__}")
    sub = p.add_subparsers(dest="command")

    def search_opts(sp):
        sp.add_argument("-s", "--setting", action="append", metavar="NAME=VALUE",
                        help="override a BibTeX setting for this run (e.g. key_style=dblp)")
        sp.add_argument("--refresh", action="store_true", help="ignore the local cache")
        sp.add_argument("-q", "--quiet", action="store_true", help="only print BibTeX")
        sp.add_argument("-v", "--verbose", action="store_true", help="log what each source does")

    sp = sub.add_parser("search", help="find the BibTeX of one paper (the default command)")
    sp.add_argument("query", nargs="+", help="title, DOI, arXiv ID, link, reference, BibTeX entry or PDF file")
    sp.add_argument("-a", "--all", action="store_true", help="print every version (published, preprint...)")
    sp.add_argument("--json", action="store_true", help="print the full results as JSON")
    sp.add_argument("-o", "--output", help="with a file query: where to write the BibTeX")
    search_opts(sp)

    sp = sub.add_parser("batch", help="check a .bib file, or one reference per line")
    sp.add_argument("file", help="a .bib file, a text file with one reference per line, or - for stdin")
    sp.add_argument("-o", "--output", help="write the result to this file instead of stdout")
    sp.add_argument("--new-keys", action="store_true", help="give .bib entries new citation keys")
    search_opts(sp)

    sp = sub.add_parser("serve", help="open the web interface on this computer")
    sp.add_argument("--port", type=int, help=f"port (default: {config.DEFAULT_PORT}, or the port option)")
    sp.add_argument("--host", default="127.0.0.1", help="address to listen on (default: 127.0.0.1)")
    sp.add_argument("--no-browser", action="store_true", help="don't open the browser")
    sp.add_argument("-v", "--verbose", action="store_true")

    sp = sub.add_parser("config", help="show or change settings (config.json)")
    sp.add_argument("action", nargs="?", choices=("show", "path", "set", "unset"))
    sp.add_argument("name", nargs="?")
    sp.add_argument("value", nargs="?")

    sp = sub.add_parser("keys", help="manage API keys (SerpAPI, Semantic Scholar, OpenAlex)")
    sp.add_argument("action", nargs="?", choices=("list", "add", "remove"))
    sp.add_argument("service", nargs="?", help=", ".join(config.API_SERVICES))
    sp.add_argument("key", nargs="?", help="the key (add; asked for if omitted) or its number (remove)")
    sp.add_argument("--no-check", action="store_true", help="save the key without asking the service")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _utf8_streams()
    # Sans sous-commande, ce qui suit est la recherche : therightbibtex "Attention is all you need".
    if argv and argv[0] not in COMMANDS and argv[0] not in ("-h", "--help", "--version"):
        argv.insert(0, "search")
    args = _parser().parse_args(argv)
    if not args.command:
        _parser().print_help()
        return 0
    verbose = getattr(args, "verbose", False)
    logging.basicConfig(level=logging.INFO if verbose else logging.ERROR, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    out = Out(quiet=getattr(args, "quiet", False))
    try:
        if args.command == "search" and getattr(args, "output", None):
            return _search_to_file(args, out)
        return {"search": cmd_search, "batch": cmd_batch, "serve": cmd_serve,
                "config": cmd_config, "keys": cmd_keys}[args.command](args, out)
    except config.ConfigError as e:
        raise SystemExit(str(e)) from None
    except KeyboardInterrupt:
        out.clear()
        return 130


def _search_to_file(args, out: Out) -> int:
    """search QUERY -o FILE : le BibTeX dans le fichier (ou un lot si la requête est un fichier)."""
    text = " ".join(args.query).strip()
    if Path(text).is_file() and not text.lower().endswith(".pdf"):
        args.file = text
        return cmd_batch(args, out)
    buf = []
    real = sys.stdout

    class Capture:
        def write(self, s):
            buf.append(s)

        def flush(self):
            pass

    sys.stdout = Capture()
    try:
        code = cmd_search(args, out)
    finally:
        sys.stdout = real
    Path(args.output).write_text("".join(buf), encoding="utf-8")
    out.info(f"Wrote {args.output}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
