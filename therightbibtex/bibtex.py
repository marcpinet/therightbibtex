"""Lecture et écriture de BibTeX.

- `parse()` lit un texte contenant une ou plusieurs entrées (tolérant :
  valeurs entre accolades, guillemets, nombres, macros, concaténations #).
- `render()` écrit l'entrée « recommandée » à partir d'une spec fusionnée
  (valeurs Unicode + provenance) selon les réglages de l'utilisateur.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .textutil import (
    Author, ascii_word, first_title_word, latex_to_text, norm_pages, parse_name,
    protect_title, text_to_latex,
)

MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
_MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6, "july": 7,
    "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "janvier": 1, "fevrier": 2, "février": 2, "mars": 3, "avril": 4, "mai": 5, "juin": 6,
    "juillet": 7, "aout": 8, "août": 8, "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12,
    "décembre": 12,
}


def month_number(v) -> int | None:
    if v is None:
        return None
    s = str(v).strip().strip("{}").lower().rstrip(".")
    if s.isdigit():
        n = int(s)
        return n if 1 <= n <= 12 else None
    if s[:3] in MONTHS:
        return MONTHS.index(s[:3]) + 1
    return _MONTH_NAMES.get(s)


# ----------------------------------------------------------------- parsing

@dataclass
class BibEntry:
    type: str
    key: str
    fields: dict[str, str] = field(default_factory=dict)
    raw: str = ""

    def get(self, name: str, default: str = "") -> str:
        return self.fields.get(name.lower(), default)

    def text(self, name: str) -> str:
        """Valeur décodée en Unicode."""
        return latex_to_text(self.get(name))

    def authors(self, name: str = "author") -> list[Author]:
        raw = self.get(name)
        if not raw:
            return []
        out = []
        for part in split_names(raw):
            stripped = part.strip()
            if stripped.lower() == "others":
                continue
            # {{Nom de collectif}} protégé : on le garde tel quel.
            if stripped.startswith("{") and stripped.endswith("}") and _balanced(stripped[1:-1]):
                out.append(Author("", latex_to_text(stripped[1:-1]), corporate=True))
                continue
            out.append(parse_name(latex_to_text(stripped)))
        return [a for a in out if a.family]


def _balanced(s: str) -> bool:
    depth = 0
    for c in s:
        depth += (c == "{") - (c == "}")
        if depth < 0:
            return False
    return depth == 0


def split_names(raw: str) -> list[str]:
    """Découpe sur ' and ' au niveau 0 des accolades."""
    raw = re.sub(r"\s+", " ", raw)
    parts, depth, cur, i = [], 0, [], 0
    while i < len(raw):
        c = raw[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        if depth == 0 and raw[i:i + 5].lower() == " and ":
            parts.append("".join(cur))
            cur = []
            i += 5
            continue
        cur.append(c)
        i += 1
    parts.append("".join(cur))
    return [p.strip() for p in parts if p.strip()]


class _Parser:
    def __init__(self, text: str):
        self.s = text
        self.i = 0
        self.macros = {m: m for m in MONTHS}

    def ws(self):
        while self.i < len(self.s) and self.s[self.i].isspace():
            self.i += 1

    def ident(self) -> str:
        start = self.i
        while self.i < len(self.s) and (self.s[self.i].isalnum() or self.s[self.i] in "_-:.+/'"):
            self.i += 1
        return self.s[start:self.i]

    def braced(self) -> str:
        """Lit {...} équilibré, renvoie le contenu sans les accolades externes."""
        assert self.s[self.i] == "{"
        depth, start = 0, self.i + 1
        while self.i < len(self.s):
            c = self.s[self.i]
            if c == "\\":
                self.i += 2
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    self.i += 1
                    return self.s[start:self.i - 1]
            self.i += 1
        raise ValueError("accolade non fermée")

    def quoted(self) -> str:
        assert self.s[self.i] == '"'
        self.i += 1
        depth, start = 0, self.i
        while self.i < len(self.s):
            c = self.s[self.i]
            if c == "\\":
                self.i += 2
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
            elif c == '"' and depth == 0:
                self.i += 1
                return self.s[start:self.i - 1]
            self.i += 1
        raise ValueError("guillemet non fermé")

    def value(self) -> str:
        parts = []
        while True:
            self.ws()
            if self.i >= len(self.s):
                break
            c = self.s[self.i]
            if c == "{":
                parts.append(self.braced())
            elif c == '"':
                parts.append(self.quoted())
            else:
                word = self.ident()
                if not word:
                    break
                parts.append(self.macros.get(word.lower(), word))
            self.ws()
            if self.i < len(self.s) and self.s[self.i] == "#":
                self.i += 1
                continue
            break
        return "".join(parts)

    def entries(self) -> list[BibEntry]:
        out = []
        while True:
            at = self.s.find("@", self.i)
            if at < 0:
                break
            self.i = at + 1
            etype = self.ident().lower()
            self.ws()
            if self.i >= len(self.s) or self.s[self.i] not in "{(":
                continue
            close = "}" if self.s[self.i] == "{" else ")"
            if etype in ("comment", "preamble"):
                try:
                    if self.s[self.i] == "{":
                        self.braced()
                except ValueError:
                    break
                continue
            self.i += 1
            try:
                if etype == "string":
                    self.ws()
                    name = self.ident().lower()
                    self.ws()
                    if self.i < len(self.s) and self.s[self.i] == "=":
                        self.i += 1
                        self.macros[name] = self.value()
                    self.ws()
                    if self.i < len(self.s) and self.s[self.i] == close:
                        self.i += 1
                    continue
                self.ws()
                kstart = self.i
                while self.i < len(self.s) and self.s[self.i] not in ",\n" + close:
                    self.i += 1
                key = self.s[kstart:self.i].strip()
                fields: dict[str, str] = {}
                while self.i < len(self.s):
                    self.ws()
                    if self.i < len(self.s) and self.s[self.i] == ",":
                        self.i += 1
                        self.ws()
                    if self.i >= len(self.s):
                        break
                    if self.s[self.i] == close:
                        self.i += 1
                        break
                    name = self.ident().lower()
                    self.ws()
                    if not name or self.i >= len(self.s) or self.s[self.i] != "=":
                        # Champ illisible : on saute jusqu'à la prochaine virgule.
                        nxt = self.s.find(",", self.i)
                        if nxt < 0:
                            self.i = len(self.s)
                            break
                        self.i = nxt
                        continue
                    self.i += 1
                    fields[name] = re.sub(r"\s+", " ", self.value()).strip()
                out.append(BibEntry(etype, key, fields, self.s[at:self.i].strip()))
            except ValueError:
                break
        return out


def parse(text: str) -> list[BibEntry]:
    if not text or "@" not in text:
        return []
    return _Parser(text).entries()


def parse_one(text: str) -> BibEntry | None:
    entries = parse(text)
    return entries[0] if entries else None


# ----------------------------------------------------------------- écriture

def write_entry(etype: str, key: str, fields: list[tuple[str, str]]) -> str:
    fields = [(n, v) for n, v in fields if v not in (None, "")]
    if not fields:
        return f"@{etype}{{{key}\n}}"
    width = max(len(n) for n, _ in fields)
    lines = []
    for name, value in fields:
        if name == "month" and re.fullmatch(r"[a-z]{3}", value):
            lines.append(f"  {name.ljust(width)} = {value}")
        else:
            lines.append(f"  {name.ljust(width)} = {{{value}}}")
    return f"@{etype}{{{key},\n" + ",\n".join(lines) + "\n}"


def reformat(text: str) -> str:
    """Réécrit des entrées brutes avec une mise en forme homogène (valeurs
    inchangées). Sert à présenter les BibTeX des sources de façon lisible."""
    entries = parse(text)
    if not entries:
        return text.strip()
    out = []
    for e in entries:
        out.append(write_entry(e.type, e.key, list(e.fields.items())))
    return "\n\n".join(out)


# ----------------------------------------------------------------- clés

KEY_STYLES = ("scholar", "AuthorYear", "AuthorYearWord", "dblp")


def make_key(style: str, authors: list[Author], year, title: str, dblp: str = "") -> str:
    if style == "dblp" and dblp:
        return "DBLP:" + dblp
    first = authors[0] if authors else None
    family = (first.family.split()[0] if first.corporate else first.family) if first else ""
    surname = ascii_word(family) or "anon"
    y = str(year or "")
    word = first_title_word(title)
    if style == "AuthorYear":
        return f"{surname.capitalize()}{y}"
    if style == "AuthorYearWord":
        return f"{surname.capitalize()}{y}{word.capitalize()}"
    return f"{surname}{y}{word}"


# ----------------------------------------------------------------- rendu

DEFAULT_SETTINGS = {
    "key_style": "scholar",        # scholar | AuthorYear | AuthorYearWord | dblp
    "arxiv_style": "article",      # article (journal = arXiv preprint ...) | misc (eprint)
    "accents": "latex",            # latex | unicode
    "protect_caps": True,
    "url": "auto",                 # auto (sans DOI) | always | never
    "doi": True,
    "hal": True,                   # hal_id / hal_version d'un dépôt HAL (champs de l'export de HAL)
    "month": False,
    "abstract": False,
    "max_authors": 100,
    "venue_style": "full",         # full (nom officiel) | short (ICML) | short_year (ICML 2021)
    # Affichage, pas BibTeX : largeur de la notice étirée sur grand écran (px, 0 = largeur par défaut).
    "card_width": 0,
}
CARD_WIDTH_RANGE = (480, 3000)
VENUE_STYLES = ("full", "short", "short_year")


def merged_settings(s: dict | None) -> dict:
    out = dict(DEFAULT_SETTINGS)
    for k, v in (s or {}).items():
        if k in out and type(v) is type(out[k]):
            out[k] = v
    if out["key_style"] not in KEY_STYLES:
        out["key_style"] = "scholar"
    if out["arxiv_style"] not in ("article", "misc"):
        out["arxiv_style"] = "article"
    if out["accents"] not in ("latex", "unicode"):
        out["accents"] = "latex"
    if out["url"] not in ("auto", "always", "never"):
        out["url"] = "auto"
    if out["venue_style"] not in VENUE_STYLES:
        out["venue_style"] = "full"
    out["max_authors"] = max(1, min(int(out["max_authors"]), 1000))
    lo, hi = CARD_WIDTH_RANGE
    out["card_width"] = 0 if out["card_width"] <= 0 else max(lo, min(int(out["card_width"]), hi))
    return out


def _fmt_names(names: list[dict], accents: str, limit: int) -> str:
    out = []
    for d in names[:limit]:
        a = Author.from_dict(d)
        if a.corporate:
            out.append("{" + text_to_latex(a.family, accents) + "}")
        elif a.given:
            out.append(f"{text_to_latex(a.family, accents)}, {text_to_latex(a.given, accents)}")
        else:
            out.append(text_to_latex(a.family, accents))
    if len(names) > limit:
        out.append("others")
    return " and ".join(out)


def render(spec: dict, settings: dict | None = None) -> dict:
    """spec (valeurs Unicode) -> {"bibtex", "key", "prov": {champ: source}}."""
    st = merged_settings(settings)
    acc = st["accents"]
    prov_in = spec.get("prov", {})
    etype = spec.get("type") or "misc"
    authors = [Author.from_dict(a) for a in spec.get("authors", [])]
    key = make_key(st["key_style"], authors, spec.get("year"), spec.get("title", ""), spec.get("dblp", ""))

    fields: list[tuple[str, str]] = []
    prov: dict[str, str] = {}

    def add(name: str, value, src_field: str | None = None):
        if value in (None, "", []):
            return
        fields.append((name, str(value)))
        src = prov_in.get(src_field or name)
        if src:
            prov[name] = src

    if spec.get("authors"):
        add("author", _fmt_names(spec["authors"], acc, st["max_authors"]), "authors")
    title = spec.get("title", "")
    if title:
        t = protect_title(title) if st["protect_caps"] else title
        # protect_title insère des accolades : on échappe mot à mot pour ne pas les doubler.
        add("title", _latex_title(t, acc, st["protect_caps"]), "title")

    preprint = spec.get("preprint")
    arxiv = spec.get("arxiv", "")
    venue = spec.get("venue", "")
    venue_src = "venue"
    # Nom abrégé de la conférence/revue (« ICML », « ICML 2021 ») à la place du nom
    # officiel. Aucun champ standard n'existe pour cela (biblatex n'a que shortjournal).
    short = not preprint and st["venue_style"] != "full" and bool(spec.get("venue_short")) \
        and etype in ("inproceedings", "incollection", "article")
    if short:
        venue, venue_src = spec["venue_short"], "venue_short"
        year = str(spec.get("year") or "")
        if st["venue_style"] == "short_year" and etype != "article" and year and year not in venue:
            venue = f"{venue} {year}"
    if preprint and arxiv:
        if st["arxiv_style"] == "misc":
            etype = "misc"
        else:
            etype = "article"
            add("journal", f"arXiv preprint arXiv:{arxiv}", "arxiv")
    elif preprint and venue:
        if st["arxiv_style"] == "misc":
            etype = "misc"
            add("howpublished", text_to_latex(venue, acc), "venue")
        else:
            etype = "article"
            add("journal", text_to_latex(venue, acc), "venue")
    elif etype in ("inproceedings", "incollection"):
        add("booktitle", text_to_latex(venue, acc), venue_src)
    elif etype == "article":
        add("journal", text_to_latex(venue, acc), venue_src)
    elif etype in ("phdthesis", "mastersthesis"):
        add("school", text_to_latex(spec.get("school") or venue, acc), "school")
    elif etype == "techreport":
        add("institution", text_to_latex(spec.get("institution") or venue, acc), "institution")
    elif etype == "misc" and venue:
        add("howpublished", text_to_latex(venue, acc), "venue")
    elif etype == "book" and venue and venue != spec.get("publisher"):
        add("series", text_to_latex(venue, acc), "venue")

    # En abrégé, la collection des actes (PMLR, LNCS), son volume et l'éditeur
    # n'ont plus de sens sans le nom complet : on les omet.
    drop_proc = short and etype != "article"
    if spec.get("series") and etype != "book" and not drop_proc:
        add("series", text_to_latex(spec["series"], acc))
    if not (drop_proc and spec.get("series")):
        add("volume", spec.get("volume"))
    add("number", spec.get("number"))
    add("pages", norm_pages(spec.get("pages")))
    add("year", spec.get("year"))
    if st["month"] and spec.get("month"):
        m = month_number(spec["month"])
        if m:
            add("month", MONTHS[m - 1])
    if spec.get("publisher") and not short:
        add("publisher", text_to_latex(spec["publisher"], acc))
    if etype in ("book", "incollection", "inbook") and spec.get("isbn"):
        add("isbn", spec["isbn"])
    doi = spec.get("doi", "")
    if st["doi"] and doi:
        add("doi", doi)
    url = spec.get("url", "")
    if url and (st["url"] == "always" or (st["url"] == "auto" and not (st["doi"] and doi))):
        add("url", url)
    if st["hal"] and spec.get("hal"):
        add("hal_id", spec["hal"], "hal")
        add("hal_version", spec.get("hal_version"), "hal")
    if preprint and arxiv and st["arxiv_style"] == "misc":
        add("eprint", arxiv, "arxiv")
        add("archivePrefix", "arXiv", "arxiv")
        if spec.get("arxiv_class"):
            add("primaryClass", spec["arxiv_class"], "arxiv_class")
    if st["abstract"] and spec.get("abstract"):
        add("abstract", text_to_latex(spec["abstract"], acc))
    return {"bibtex": write_entry(etype, key, fields), "key": key, "type": etype, "prov": prov}


def _latex_title(protected: str, accents: str, protect_math: bool) -> str:
    """Échappe un titre déjà passé par protect_title sans toucher aux
    accolades de protection ajoutées."""
    out, depth, buf = [], 0, []

    def flush():
        if buf:
            out.append(text_to_latex("".join(buf), accents, protect_math))
            buf.clear()

    i = 0
    # Les accolades de protection n'entourent que des mots (jamais de $).
    while i < len(protected):
        c = protected[i]
        if c == "$":
            j = protected.find("$", i + 1)
            if j > i:
                buf.append(protected[i:j + 1])
                i = j + 1
                continue
        if c == "{" and _is_protect_open(protected, i):
            flush()
            out.append("{")
            depth += 1
        elif c == "}" and depth:
            flush()
            out.append("}")
            depth -= 1
        else:
            buf.append(c)
        i += 1
    flush()
    return "".join(out)


def _is_protect_open(s: str, i: int) -> bool:
    j = s.find("}", i)
    return j > i and "{" not in s[i + 1:j] and " " not in s[i + 1:j]


# ----------------------------------------------------------------- spec <- entrée

TYPE_KIND = {
    "article": "journal", "inproceedings": "conference", "conference": "conference",
    "incollection": "chapter", "inbook": "chapter", "book": "book", "phdthesis": "thesis",
    "mastersthesis": "thesis", "techreport": "report", "misc": "other", "unpublished": "other",
    "online": "other", "proceedings": "book",
}


def entry_summary(e: BibEntry) -> dict:
    """Métadonnées lisibles d'une entrée (pour les favoris et l'import)."""
    venue = e.text("journal") or e.text("booktitle") or e.text("howpublished") or e.text("school") \
        or e.text("institution") or e.text("publisher")
    return {
        "key": e.key,
        "type": e.type,
        "title": e.text("title"),
        "authors": "; ".join(a.display() for a in e.authors()),
        "year": e.get("year")[:4] if e.get("year") else "",
        "venue": venue,
        "doi": e.get("doi"),
        "arxiv": e.get("eprint") if e.get("archiveprefix").lower() == "arxiv" else "",
    }


def set_key(text: str, new_key: str) -> str:
    """Remplace la clé de la première entrée."""
    return re.sub(r"^(\s*@\w+\s*[{(]\s*)[^,\s]*", lambda m: m.group(1) + new_key, text, count=1)


# ----------------------------------------------------------------- CSL-JSON

_CSL_TYPE = {
    "article": "article-journal", "inproceedings": "paper-conference", "conference": "paper-conference",
    "incollection": "chapter", "inbook": "chapter", "book": "book", "proceedings": "book",
    "phdthesis": "thesis", "mastersthesis": "thesis", "techreport": "report", "unpublished": "manuscript",
    "online": "webpage", "misc": "document",
}


def to_csl(e: BibEntry) -> dict:
    """Entrée BibTeX -> élément CSL-JSON (Zotero, Pandoc, Mendeley...)."""
    etype = e.type.lower()
    container = e.text("journal") or e.text("booktitle")
    eprint = e.get("eprint") if e.get("archiveprefix").lower() == "arxiv" or "arxiv" in e.get("eprinttype").lower() else ""
    m = re.search(r"arxiv[:\s]*(\d{4}\.\d{4,5}|[a-z][a-z-]+(?:\.[A-Z]{2})?/\d{7})", container, re.I) \
        if not eprint else None
    if m:
        eprint = m.group(1)
    ctype = _CSL_TYPE.get(etype, "document")
    if eprint and etype in ("article", "misc", "unpublished"):
        ctype = "article"                      # CSL : « article » = prépublication
    item: dict = {"id": e.key, "type": ctype, "title": e.text("title")}
    def names(field):
        return [{"literal": a.family} if a.corporate else {"family": a.family, "given": a.given} if a.given
                else {"family": a.family} for a in e.authors(field)]
    if e.get("author"):
        item["author"] = names("author")
    if e.get("editor"):
        item["editor"] = names("editor")
    year = re.match(r"\d{4}", e.get("year") or "")
    if year:
        parts = [int(year.group())]
        month = month_number(e.get("month"))
        if month:
            parts.append(month)
        item["issued"] = {"date-parts": [parts]}
    if eprint:
        item.update({"publisher": "arXiv", "number": f"arXiv:{eprint}", "URL": f"https://arxiv.org/abs/{eprint}"})
    elif container:
        item["container-title"] = container
    elif e.get("howpublished"):
        item["container-title"] = e.text("howpublished")
    for csl, bib in (("volume", "volume"), ("issue", "number"), ("collection-title", "series"), ("ISBN", "isbn"),
                     ("ISSN", "issn"), ("abstract", "abstract"), ("note", "note"), ("edition", "edition")):
        if e.get(bib):
            item[csl] = e.text(bib)
    if etype == "techreport" and e.get("number"):
        item["number"] = item.pop("issue")
    if e.get("pages"):
        item["page"] = norm_pages(e.get("pages")).replace("--", "-")
    publisher = e.text("publisher") or e.text("school") or e.text("institution")
    if publisher and "publisher" not in item:
        item["publisher"] = publisher
    if e.get("address"):
        item["publisher-place"] = e.text("address")
    if etype in ("phdthesis", "mastersthesis"):
        item["genre"] = "PhD thesis" if etype == "phdthesis" else "Master's thesis"
    if e.get("doi"):
        item["DOI"] = re.sub(r"^https?://(dx\.)?doi\.org/", "", e.get("doi"))
    if e.get("url") and "URL" not in item:
        item["URL"] = e.get("url")
    return {k: v for k, v in item.items() if v not in (None, "", [])}
