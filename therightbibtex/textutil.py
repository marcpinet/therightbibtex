"""Normalisation des métadonnées : titres, DOI, identifiants arXiv, noms
d'auteurs, et conversions LaTeX <-> Unicode.

Toutes les sources sont ramenées à du texte Unicode « propre » (les segments
mathématiques `$...$` sont conservés tels quels) ; le LaTeX n'est produit
qu'au moment d'écrire le BibTeX.
"""
from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass

from rapidfuzz import fuzz

# --------------------------------------------------------------- identifiants

DOI_RE = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>{}]+)", re.I)
ARXIV_NEW_RE = re.compile(r"(?<![\d.])(\d{4}\.\d{4,5})(v\d+)?(?![\d])")
ARXIV_OLD_RE = re.compile(r"\b([a-z][a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?\b")

# Préfixes DOI de serveurs de prépublications / dépôts (pas des versions publiées).
PREPRINT_DOI_PREFIXES = (
    "10.48550/",          # arXiv
    "10.2139/ssrn",       # SSRN
    "10.21203/rs.",       # Research Square
    "10.20944/preprints", # Preprints.org
    "10.36227/techrxiv",  # TechRxiv
    "10.26434/chemrxiv",  # ChemRxiv
    "10.31219/osf.io",    # OSF Preprints
    "10.31234/osf.io",    # PsyArXiv
    "10.31235/osf.io",    # SocArXiv
    "10.31222/osf.io",    # MetaArXiv
    "10.35542/osf.io",    # EdArXiv
    "10.31224/",          # engrXiv
    "10.31223/",          # EarthArXiv
    "10.22541/au.",       # Authorea
    "10.1002/essoar.",    # ESS Open Archive
    "10.33774/",          # Cambridge Open Engage
    "10.32942/",          # EcoEvoRxiv
    "10.2196/preprints.", # JMIR Preprints
)
REPOSITORY_DOI_PREFIXES = ("10.5281/zenodo.", "10.6084/m9.figshare.")
# bioRxiv / medRxiv : préfixe 10.1101 partagé avec des revues de Cold Spring
# Harbor (Genome Research = 10.1101/gr...) -> on reconnaît le motif daté.
BIORXIV_RE = re.compile(r"^10\.1101/(\d{4}\.\d{2}\.\d{2}\.\d+|\d{6})(v\d+)?$")


def norm_doi(s: str | None) -> str:
    if not s:
        return ""
    s = html.unescape(str(s)).strip()
    s = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", s, flags=re.I)
    s = re.sub(r"^doi:\s*", "", s, flags=re.I)
    m = DOI_RE.search(s)
    if not m:
        return ""
    doi = m.group(1)
    # Ponctuation finale ramassée par le motif (fin de phrase, parenthèse).
    while doi and doi[-1] in ".,;:)]}>'\"":
        if doi[-1] == ")" and doi.count("(") >= doi.count(")"):
            break
        doi = doi[:-1]
    return doi.lower()


def is_preprint_doi(doi: str) -> bool:
    doi = (doi or "").lower()
    return doi.startswith(PREPRINT_DOI_PREFIXES) or bool(BIORXIV_RE.match(doi))


def is_repository_doi(doi: str) -> bool:
    return (doi or "").lower().startswith(REPOSITORY_DOI_PREFIXES)


def arxiv_from_doi(doi: str) -> str:
    m = re.match(r"^10\.48550/arxiv\.(.+)$", (doi or "").lower())
    return norm_arxiv(m.group(1)) if m else ""


def norm_arxiv(s: str | None) -> str:
    """Identifiant arXiv sans version ni préfixe ('arXiv:1706.03762v5' -> '1706.03762')."""
    if not s:
        return ""
    s = str(s).strip()
    s = re.sub(r"^(https?://)?(www\.|export\.)?arxiv\.org/(abs|pdf|html)/", "", s, flags=re.I)
    s = re.sub(r"^arxiv:\s*", "", s, flags=re.I)
    s = re.sub(r"\.pdf$", "", s, flags=re.I)
    m = ARXIV_NEW_RE.fullmatch(s) or ARXIV_NEW_RE.match(s)
    if m:
        return m.group(1)
    m = ARXIV_OLD_RE.match(s)
    if m:
        return m.group(1)
    return ""


def arxiv_version(s: str | None) -> int | None:
    m = re.search(r"v(\d+)$", (s or "").strip())
    return int(m.group(1)) if m else None


def arxiv_doi(arxiv_id: str) -> str:
    return f"10.48550/arxiv.{arxiv_id}".lower() if arxiv_id else ""


# --------------------------------------------------------------- texte

_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")


def clean_markup(s: str | None) -> str:
    """Retire le balisage HTML/JATS/MathML des titres Crossref & co.
    <sub>/<sup> deviennent des indices/exposants LaTeX, le reste est aplati."""
    if not s:
        return ""
    s = str(s)
    if "<" in s:
        s = re.sub(r"<mml:math.*?</mml:math>", lambda m: _TAG_RE.sub("", m.group(0)), s, flags=re.S)
        s = re.sub(r"<sub>(.*?)</sub>", r"$_{\1}$", s, flags=re.S | re.I)
        s = re.sub(r"<sup>(.*?)</sup>", r"$^{\1}$", s, flags=re.S | re.I)
        s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    return _SPACE_RE.sub(" ", s).strip()


def strip_accents(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c))


_LATEX_CMD_RE = re.compile(r"\\[a-zA-Z]+\*?")


def norm_title(s: str | None) -> str:
    """Clé de comparaison : minuscules, sans accents, sans LaTeX ni ponctuation."""
    if not s:
        return ""
    s = clean_markup(s)
    s = _LATEX_CMD_RE.sub(" ", s)
    s = strip_accents(s).lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return _SPACE_RE.sub(" ", s).strip()


def _token_sim(ta: list[str], tb: list[str]) -> float:
    """Jaccard sur les mots, deux mots longs presque identiques comptant pour
    égaux (optimisation/optimization). Sensible au mot qui change tout
    (« INet » vs « U-Net », « classification » vs « recognition »)."""
    if not ta or not tb:
        return 0.0
    sa, sb = set(ta), set(tb)
    common = sa & sb
    rest_a, rest_b = sa - common, sb - common
    fuzzy = 0
    for w in list(rest_a):
        if len(w) >= 5:
            for v in rest_b:
                if len(v) >= 5 and fuzz.ratio(w, v) >= 88:
                    fuzzy += 1
                    rest_b.discard(v)
                    break
    inter = len(common) + fuzzy
    return inter / (len(sa) + len(sb) - inter)


def title_similarity(a: str, b: str) -> float:
    """0..1. Ratio caractère (sensible à l'ordre), borné par token_set
    (sous-titre manquant) et par la similarité au niveau des mots."""
    na, nb = norm_title(a), norm_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    char = min(fuzz.ratio(na, nb), fuzz.token_set_ratio(na, nb)) / 100.0
    return min(char, _token_sim(na.split(), nb.split()) + 0.15)


_SUBTITLE_SEP = re.compile(r"\s*[:?!]\s+|\s+[-\N{EN DASH}\N{EM DASH}]\s+")


def main_title_match(query_title: str, title: str) -> float:
    """Ce qui est tapé est le titre principal, sans le sous-titre : « When Foundation
    Models are One-Liners », ou le nom court du papier (« mTSBench », « LoRA »,
    « DINOv2 ») pour « mTSBench: Benchmarking Multivariate Time Series... ». Presque
    aussi sûr qu'un titre exact ; un nom court l'est un peu moins (homonymes)."""
    nq = norm_title(query_title)
    parts = _SUBTITLE_SEP.split(title, maxsplit=1)
    if not nq or len(parts) < 2 or norm_title(parts[0]) != nq:
        return 0.0
    words = nq.split()
    if len(words) >= 3:
        return 0.96
    if len(nq.replace(" ", "")) >= 4 and not all(w in TITLE_STOPWORDS for w in words):
        return 0.93
    return 0.0


def is_short_name(text: str) -> bool:
    """Un seul « mot » tapé, le nom court d'un papier : « BERT », « U-Net », « GPT-4 »,
    « DINOv2 », « mTSBench ». Les sources le cherchent alors dans les titres
    (« BERT: Pre-training of... »), pas comme un titre entier."""
    t = (text or "").strip()
    return 3 <= len(t) <= 24 and not re.search(r"\s", t) and norm_title(t) not in TITLE_STOPWORDS


def title_match(found: str, wanted: str) -> float:
    """Comme title_similarity, mais un titre tapé sans son sous-titre (« When
    Foundation Models are One-Liners » pour « ... One-Liners: Limitations and
    Future Directions ») compte comme trouvé. Sert aux sources qui trient
    elles-mêmes leurs résultats avant de les rendre au moteur."""
    s = max(title_similarity(found, wanted), main_title_match(wanted, found))
    nf, nw = norm_title(found), norm_title(wanted)
    if s < 0.97 and len(nw.split()) >= 4 and len(nw) < len(nf):
        s = max(s, fuzz.partial_ratio(nw, nf) / 100.0 * 0.97)
    return s


def title_in_text(title: str, text: str) -> float:
    """Le titre apparaît-il dans un texte plus long (référence libre) ? 0..1."""
    nt, nx = norm_title(title), norm_title(text)
    if not nt or not nx:
        return 0.0
    if len(nt) > len(nx):
        return title_similarity(title, text)
    return fuzz.partial_ratio(nt, nx) / 100.0


def is_all_caps(s: str) -> bool:
    letters = [c for c in s if c.isalpha()]
    return len(letters) > 8 and sum(c.isupper() for c in letters) / len(letters) > 0.9


def smart_title_case_fix(s: str) -> str:
    """Titres en MAJUSCULES (certains éditeurs) -> casse de titre lisible."""
    if not is_all_caps(s):
        return s
    small = {"a", "an", "the", "and", "or", "of", "for", "in", "on", "to", "with", "by", "at", "via", "from"}
    out = []
    for i, w in enumerate(s.split(" ")):
        lw = w.lower()
        out.append(lw if i and lw in small else lw[:1].upper() + lw[1:])
    return " ".join(out)


def clean_title(s: str | None) -> str:
    s = clean_markup(s)
    s = s.strip().rstrip(".").strip() if not s.endswith("...") else s.strip()
    return smart_title_case_fix(s)


def year_of(s) -> int | None:
    if s is None:
        return None
    if isinstance(s, int):
        return s if 1000 < s < 3000 else None
    m = re.search(r"\b(1[5-9]\d\d|2[01]\d\d)\b", str(s))
    return int(m.group(1)) if m else None


def norm_pages(s: str | None) -> str:
    if not s:
        return ""
    s = str(s).strip()
    s = re.sub(r"\s*(--|\u2013|\u2014|\u2010|\u2011|-)\s*", "--", s)
    if re.fullmatch(r"[A-Za-z]?\d+--[A-Za-z]?\d+", s):
        a, b = s.split("--")
        if a == b:
            return a
    return s


# --------------------------------------------------------------- auteurs

_PARTICLES = {"van", "von", "der", "den", "de", "del", "della", "di", "da", "du",
              "le", "la", "dos", "das", "ter", "ten", "zu", "af", "y", "bin", "al"}
_SUFFIXES = {"jr", "jr.", "sr", "sr.", "ii", "iii", "iv"}
_CORPORATE_RE = re.compile(
    r"\b(collaboration|consortium|team|group|project|committee|council|association|"
    r"institute|university|inc\.?|ltd\.?|corporation|foundation|organization|society|"
    r"network|initiative|openai|deepmind|google|meta|microsoft|anthropic|working)\b", re.I)


@dataclass
class Author:
    given: str = ""
    family: str = ""
    corporate: bool = False

    def display(self) -> str:
        return f"{self.given} {self.family}".strip() if not self.corporate else self.family

    def key(self) -> str:
        return family_key(self.family)

    def to_dict(self) -> dict:
        d = {"given": self.given, "family": self.family}
        if self.corporate:
            d["corporate"] = True
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Author":
        return cls(d.get("given", ""), d.get("family", ""), bool(d.get("corporate")))

    def initials_only(self) -> bool:
        g = self.given.replace(".", " ").replace("-", " ").split()
        return bool(g) and all(len(x) == 1 for x in g)


def family_key(name: str) -> str:
    return re.sub(r"[^a-z]", "", strip_accents(name or "").lower())


def parse_name(name: str) -> Author:
    """'Laurens van der Maaten' / 'van der Maaten, Laurens' / 'Wei Wang 0001'."""
    name = clean_markup(name)
    name = re.sub(r"\s+\d{4}$", "", name)          # désambiguïsation DBLP
    name = _SPACE_RE.sub(" ", name).strip().strip(",")
    if not name:
        return Author()
    if _CORPORATE_RE.search(name) and len(name.split()) >= 2 and "," not in name:
        return Author("", name, corporate=True)
    if "," in name:
        parts = [p.strip() for p in name.split(",")]
        family, given = parts[0], parts[1] if len(parts) > 1 else ""
        if len(parts) > 2 and parts[1].lower() in _SUFFIXES:   # "King, Jr., Martin"
            family, given = f"{parts[0]} {parts[1]}", parts[2]
        return Author(given, family)
    tokens = name.split(" ")
    suffix = ""
    if len(tokens) > 2 and tokens[-1].lower() in _SUFFIXES:
        suffix = " " + tokens.pop()
    if len(tokens) == 1:
        return Author("", tokens[0] + suffix)
    i = len(tokens) - 1
    while i > 1 and tokens[i - 1].lower() in _PARTICLES:
        i -= 1
    return Author(" ".join(tokens[:i]), " ".join(tokens[i:]) + suffix)


def authors_overlap(a: list[Author], b: list[Author]) -> float:
    """Part des noms de famille de la liste la plus courte présents dans l'autre."""
    ka = {x.key() for x in a if x.key()}
    kb = {x.key() for x in b if x.key()}
    if not ka or not kb:
        return -1.0  # inconnu
    small, big = (ka, kb) if len(ka) <= len(kb) else (kb, ka)
    hits = 0
    for k in small:
        if k in big or any(len(k) > 3 and (k in o or o in k) for o in big):
            hits += 1
    return hits / len(small)


# --------------------------------------------------------------- LaTeX <-> Unicode

_MATH_RE = re.compile(r"(\$\$.+?\$\$|\$[^$]+\$|\\\(.+?\\\))", re.S)

try:
    from pylatexenc.latex2text import LatexNodes2Text
    from pylatexenc.latexencode import UnicodeToLatexEncoder

    _L2T = LatexNodes2Text(math_mode="verbatim", strict_latex_spaces=False)
    # Caractères sans équivalent LaTeX (chinois, japonais...) gardés tels quels, sans
    # un avertissement par caractère dans le journal.
    _U2L = UnicodeToLatexEncoder(non_ascii_only=True, replacement_latex_protection="braces-all",
                                 unknown_char_policy="keep", unknown_char_warning=False)
except Exception:  # pragma: no cover - dépendance optionnelle
    _L2T = None
    _U2L = None


def latex_to_text(s: str | None) -> str:
    """Valeur BibTeX -> texte Unicode (accents décodés, accolades retirées,
    maths conservées verbatim)."""
    if not s:
        return ""
    s = str(s)
    if "\\" not in s and "{" not in s and "~" not in s and "--" not in s:
        return _SPACE_RE.sub(" ", s).strip()
    out = []
    for i, seg in enumerate(_MATH_RE.split(s)):
        if i % 2:
            out.append(seg)
            continue
        if not seg:
            continue
        seg = seg.replace("~", " ")
        txt = None
        if _L2T is not None:
            try:
                txt = _L2T.latex_to_text(seg)
            except Exception:
                txt = None
        if txt is None:
            txt = re.sub(r"\\[a-zA-Z]+\s*", "", seg).replace("{", "").replace("}", "")
        out.append(txt)
    return _SPACE_RE.sub(" ", "".join(out)).strip()


_SPECIALS = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "#": r"\#", "_": r"\_",
             "{": r"\{", "}": r"\}", "^": r"\^{}", "$": r"\$"}
_SPECIAL_RE = re.compile(r"[\\&%#_{}^$]")


_TYPO = {"\u2013": "--", "\u2014": "---", "’": "'", "‘": "`", "“": "``", "”": "''", "\N{NO-BREAK SPACE}": "~", "…": "\\ldots{}"}
_TYPO_RE = re.compile("[" + "".join(_TYPO) + "]")


def _escape_plain(seg: str, accents: str) -> str:
    seg = _SPECIAL_RE.sub(lambda m: _SPECIALS[m.group(0)], seg)
    if accents == "latex":
        seg = _TYPO_RE.sub(lambda m: _TYPO[m.group(0)], seg)
    if accents == "latex" and _U2L is not None and any(ord(c) > 127 for c in seg):
        try:
            seg = _U2L.unicode_to_latex(seg)
        except Exception:
            pass
    return seg


def text_to_latex(s: str | None, accents: str = "latex", protect_math: bool = False) -> str:
    """Texte Unicode -> valeur BibTeX sûre. Les segments $...$ sont gardés
    (entre accolades si protect_math, pour qu'un style ne change pas leur casse)."""
    if not s:
        return ""
    out = []
    for i, seg in enumerate(_MATH_RE.split(s)):
        if i % 2:
            out.append("{" + seg + "}" if protect_math else seg)
        elif seg:
            out.append(_escape_plain(seg, accents))
    return "".join(out)


# Mots à garder tels quels même dans les styles qui passent le titre en minuscules.
PROPER_WORDS = {
    "bayesian", "gaussian", "markov", "markovian", "euler", "eulerian", "newton", "newtonian",
    "hilbert", "lipschitz", "wasserstein", "langevin", "monte", "carlo", "fourier", "laplace",
    "laplacian", "poisson", "bernoulli", "dirichlet", "riemannian", "riemann", "hamiltonian",
    "lagrangian", "boltzmann", "kalman", "hessian", "jacobian", "krylov", "shapley", "nash",
    "turing", "atari", "python", "english", "french", "chinese", "german", "spanish", "japanese",
    "arabic", "european", "african", "american", "asian", "indian", "latin", "greek", "russian",
    "korean", "italian", "portuguese", "dutch", "hindi", "wikipedia", "twitter", "reddit",
    "youtube", "github", "minecraft", "go", "transformer", "transformers", "stein", "kullback",
    "leibler", "jensen", "shannon", "gibbs", "metropolis", "hastings", "brownian", "levy",
    "cauchy", "gauss", "galerkin", "hopfield", "boltzmann", "hebbian", "pareto", "wiener",
    "volterra", "stokes", "navier", "schrodinger", "schrödinger", "einstein", "maxwell",
    "bellman", "kolmogorov", "chebyshev", "legendre", "hermite", "jacobi", "lanczos", "arnoldi",
    "householder", "cholesky", "tikhonov", "lyapunov", "bregman", "mahalanobis", "procrustes",
    "voronoi", "delaunay", "dijkstra", "kruskal", "prim", "huffman", "lempel", "ziv",
    "covid", "sars", "alzheimer", "parkinson", "huntington", "crohn", "europe", "africa",
    "america", "asia", "france", "china", "india", "japan", "germany", "paris", "london",
}
# "transformer(s)" et "go" retirés : trop souvent des noms communs.
PROPER_WORDS -= {"transformer", "transformers", "go", "prim"}

_WORD_SPLIT_RE = re.compile(r"(\s+)")


def _needs_protection(core: str) -> bool:
    letters = [c for c in core if c.isalpha()]
    if not letters:
        return False
    if any(c.isupper() for c in core[1:]):                 # BERT, ImageNet, eBay, GPT-4
        return True
    if re.match(r"^[A-Z]\d", core) or re.match(r"^\d+[A-Z]", core):   # R2, 3D
        return True
    if re.match(r"^[A-Z]-", core):                           # Q-learning, K-means
        return True
    if core[0].isupper() and strip_accents(core).lower() in PROPER_WORDS:
        return True
    return False


def protect_title(s: str) -> str:
    """Entoure d'accolades les mots dont la casse doit survivre aux styles
    BibTeX qui mettent le titre en minuscules (sigles, noms propres)."""
    out = []
    for i, seg in enumerate(_MATH_RE.split(s)):
        if i % 2:
            out.append(seg)
            continue
        parts = _WORD_SPLIT_RE.split(seg)
        for j, w in enumerate(parts):
            if j % 2 or not w:
                out.append(w)
                continue
            m = re.match(r"^([\"'(\[“‘]*)(.*?)([\"'),.:;!?\]”’]*)$", w)
            lead, core, trail = m.groups() if m else ("", w, "")
            pieces = core.split("-")
            if core and (_needs_protection(core) or any(_needs_protection(p) for p in pieces if p)):
                out.append(f"{lead}{{{core}}}{trail}")
            else:
                out.append(w)
    return "".join(out)


def ascii_word(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", strip_accents(s or "").lower())


TITLE_STOPWORDS = {"a", "an", "the", "on", "of", "for", "and", "in", "to", "with", "towards",
                   "toward", "via", "is", "are", "at", "by", "from", "into", "do", "does", "how",
                   "what", "why", "when", "can", "we", "it", "its", "as", "or", "be", "le",
                   "la", "les", "un", "une", "des", "du", "de", "et", "l", "d"}


def first_title_word(title: str) -> str:
    """Premier mot significatif, mots composés recollés (« U-Net » -> « unet »)."""
    plain = clean_markup(_MATH_RE.sub(" ", title or ""))
    plain = _LATEX_CMD_RE.sub(" ", plain)
    words = [ascii_word(w) for w in re.split(r"[\s/]+", plain)]
    words = [w for w in words if w]
    for w in words:
        if w not in TITLE_STOPWORDS and len(w) > 1:
            return w
    return words[0] if words else ""
