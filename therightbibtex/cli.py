"""Ligne de commande : therightbibtex « titre, DOI, arXiv, lien, référence ou PDF »
imprime le BibTeX de la version à citer ; serve ouvre l'interface web locale ;
config et keys gèrent config.json. Le BibTeX va sur la sortie standard, tout le
reste (progression, avertissements) sur la sortie d'erreur : la commande se
redirige telle quelle vers un fichier .bib."""
from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
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

COMMANDS = ("search", "batch", "pick", "serve", "config", "keys")
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
    return _write_results(args, out, results, len(items) - missing, missing)


def _write_results(args, out: Out, results: list, found: int, kept: int) -> int:
    body = "\n\n".join(r for r in results if r) + "\n"
    if args.output:
        Path(args.output).write_text(body, encoding="utf-8")
        out.info(f"Wrote {found} entries to {args.output}" + (f", {kept} kept as they were" if kept else ""))
    else:
        sys.stdout.write(body)
    return 0 if found else EXIT_NOT_FOUND


# ================================================================== lots avec choix

def choices(works: list[dict]) -> list[dict]:
    """Ce que l'utilisateur peut garder : les versions du papier trouvé (la recommandée
    d'abord), puis les autres papiers candidats quand la correspondance n'est pas sûre."""
    if not works:
        return []
    w = works[0]
    rec = recommended(w)
    out = [{"label": f"{rec['label']} ({rec['status']}, recommended)", "bibtex": rec["bibtex"]}]
    out += [{"label": f"{v['label']} ({v['status']})", "bibtex": v["bibtex"]}
            for v in w["versions"] if v["id"] != rec["id"] and not v.get("suspicious")]
    if w["confidence"] != "high":
        for x in works[1:4]:
            v = recommended(x)
            out.append({"label": f"other paper: {x['title'][:60]} ({v['label']})", "bibtex": v["bibtex"]})
    return out


def parse_answer(text: str, n: int, default: str) -> tuple[str, int]:
    """Réponse tapée -> (action, numéro) : pick (1..n), keep, show, all, quit, ou again
    si la réponse n'est pas comprise. Entrée seule = le choix par défaut."""
    t = text.strip().lower()
    if not t:
        return (default, 1) if default == "pick" else (default, 0)
    if t.isdigit() and 1 <= int(t) <= n:
        return "pick", int(t)
    if t in ("k", "keep"):
        return "keep", 0
    if t in ("a", "all"):
        return "all", 0
    if t in ("q", "quit"):
        return "quit", 0
    m = t.removeprefix("show").removeprefix("s").strip()
    if t.startswith("s") and m.isdigit() and 1 <= int(m) <= n:
        return "show", int(m)
    return "again", 0


def _searches_in_background(items: list[str], settings: dict, refresh: bool) -> list[concurrent.futures.Future]:
    """Lance les recherches (deux à la fois) dans un fil à part et rend un résultat à
    attendre par entrée : les recherches avancent pendant que l'utilisateur répond, et
    Ctrl+C ou q n'ont rien à attendre (le fil s'arrête avec le programme)."""
    futures = [concurrent.futures.Future() for _ in items]

    async def go():
        http = Http()
        queue = list(enumerate(items))

        async def worker():
            while queue:
                i, item = queue.pop(0)
                try:
                    works = await _search(http, query_mod.parse_input(item), settings, refresh)
                except Exception as e:  # une recherche en échec ne bloque pas les questions
                    logging.getLogger(__name__).info("Recherche en échec : %s", e)
                    works = []
                futures[i].set_result(works)

        try:
            await asyncio.gather(worker(), worker())
        finally:
            await http.close()

    threading.Thread(target=lambda: asyncio.run(go()), daemon=True).start()
    return futures


def _wait(future: concurrent.futures.Future, out: Out, text: str) -> list[dict]:
    # Attente par petits pas : Ctrl+C passe aussi sous Windows.
    while True:
        try:
            return future.result(timeout=0.2)
        except concurrent.futures.TimeoutError:
            out.progress(text)


def _ask(out: Out, n: int, default: str) -> tuple[str, int]:
    while True:
        out.clear()
        sys.stderr.write(f"Choice [{'1' if default == 'pick' else 'k'}]: ")
        sys.stderr.flush()
        line = sys.stdin.readline()
        if not line:
            return "quit", 0     # fin de l'entrée : plus de questions
        action, num = parse_answer(line, n, default)
        if action != "again":
            return action, num
        print(f"  type a number from 1 to {n}, k, s and a number, a or q", file=sys.stderr)


def cmd_pick(args, out: Out) -> int:
    """Comme batch, mais chaque entrée qui a plusieurs possibilités (version publiée et
    prépublication, autres papiers proches) fait l'objet d'une question : laquelle garder."""
    if args.file == "-" or not sys.stdin.isatty():
        raise SystemExit("pick asks a question for each entry: run it in a terminal on a file (or use batch)")
    text = Path(args.file).read_text(encoding="utf-8", errors="replace")
    items, is_bib = _batch_items(text)
    if not items:
        raise SystemExit(f"{args.file}: no BibTeX entry and no reference to look up")
    settings = _settings(args.setting)
    keep_keys = is_bib and not args.new_keys
    futures = _searches_in_background(items, settings, args.refresh)
    results: list[str | None] = [None] * len(items)
    found = kept = 0
    mode = "ask"          # ask | all (a : la recommandée pour le reste) | stop (q : le reste tel quel)

    def say(text: str):   # les questions s'affichent même avec -q
        out.clear()
        print(text, file=sys.stderr)

    for i, item in enumerate(items):
        head = f"[{i + 1}/{len(items)}]"
        if mode == "stop":   # sans attendre les recherches restantes
            results[i] = item if is_bib else f"% not found: {item}"
            kept += 1
            continue
        works = _wait(futures[i], out, f"{head} searching...")
        opts = choices(works)
        confident = bool(works) and works[0]["confidence"] in ("high", "medium")
        old = bibtex.parse_one(item) if is_bib else None
        action, num = "keep", 0
        if not opts:
            out.info(f"{head} not found, kept as is: {item.splitlines()[0][:60]}")
        elif mode == "all" or (len(opts) == 1 and works[0]["confidence"] == "high"):
            action, num = ("pick", 1) if confident else ("keep", 0)
            out.info(f"{head} {opts[0]['label'] if confident else 'uncertain match, kept as is'}: "
                     f"{works[0]['title'][:60]}")
        else:
            # Par défaut : la version recommandée, sauf si la correspondance est incertaine.
            default = "pick" if confident else "keep"
            say("")
            say(f"{head} {works[0]['title']}")
            say(f"  your entry: {old.key or '(no key)'}" if old else f"  you typed: {item[:70]}")
            if works[0]["confidence"] != "high":
                say(f"  {CONFIDENCE_WORD.get(works[0]['confidence'], 'unrated')}")
            for n, o in enumerate(opts, 1):
                say(f"  {n}. {o['label']}")
            say("  k. keep your entry as it is" if is_bib else "  k. leave it out")
            say("  s 2 shows a BibTeX, a accepts the rest, q stops asking")
            while True:
                action, num = _ask(out, len(opts), default)
                if action != "show":
                    break
                say(opts[num - 1]["bibtex"])
            if action == "all":
                action, num, mode = default, 1, "all"
            elif action == "quit":
                action, mode = "keep", "stop"
        if action == "pick":
            entry = opts[num - 1]["bibtex"]
            results[i] = bibtex.set_key(entry, old.key) if keep_keys and old and old.key else entry
            found += 1
        else:
            results[i] = item if is_bib else f"% not found: {item}"
            kept += 1
    return _write_results(args, out, results, found, kept)


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
               "  therightbibtex pick refs.bib -o refs-checked.bib\n"
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

    sp = sub.add_parser("pick", help="like batch, but asks which one to keep when an entry has several possibilities",
                        description="Checks a .bib file (or one reference per line) like batch. When an entry has "
                                    "several possibilities (published version and preprint, other similar papers), "
                                    "it asks which one to keep: Enter keeps the recommended one, a number picks "
                                    "another, k keeps your entry, s 2 shows a BibTeX, a accepts the recommended "
                                    "one for the rest, q keeps the rest as it is.")
    sp.add_argument("file", help="a .bib file or a text file with one reference per line")
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
        return {"search": cmd_search, "batch": cmd_batch, "pick": cmd_pick, "serve": cmd_serve,
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
