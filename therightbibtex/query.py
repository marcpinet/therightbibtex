"""Analyse de ce que l'utilisateur colle : titre, référence complète, DOI,
identifiant arXiv, URL (arXiv, OpenReview, ACL, DOI, PubMed, HAL, DBLP...),
entrée BibTeX existante."""
from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse

from . import bibtex
from .sources.base import Query
from .sources.pages import is_official
from .textutil import (
    ARXIV_NEW_RE, ARXIV_OLD_RE, DOI_RE, arxiv_from_doi, clean_title, family_key, latex_to_text,
    norm_arxiv, norm_doi, year_of,
)

# Lien avec ou sans « https:// » (« arxiv.org/abs/... » copié d'une barre d'adresse).
URL_RE = re.compile(r"^(https?://|www\.|(?:[a-z0-9-]+\.)+[a-z]{2,}/)\S*$", re.I)
_REF_MARKERS = re.compile(r"\b(et al\.?|in proceedings|proceedings of|pp\.|vol\.|journal of|"
                          r"conference on|transactions on|arxiv preprint|in:|eds?\.)", re.I)
_STOP = {"the", "and", "for", "with", "from", "this", "that", "proceedings", "conference", "journal",
         "international", "advances", "neural", "information", "processing", "systems", "arxiv",
         "preprint", "learning", "vol", "pages", "press", "university", "ieee", "acm", "in", "of"}
# Mots de deux lettres qui ne sont pas des noms (He, Li, Ng, Wu en sont).
_STOP2 = {"in", "on", "an", "at", "to", "is", "we", "it", "as", "by", "do", "go", "my", "no", "or", "so",
          "up", "of", "de", "di", "da", "du", "le", "la", "al", "el", "et", "vs", "pp", "ed", "st", "nd",
          "rd", "th", "us", "jr", "sr"}


def _url_query(url: str) -> Query | None:
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    u = urlparse(url)
    host = u.netloc.lower().removeprefix("www.")
    path = unquote(u.path)
    q = Query(raw=url, kind="id", url=url)
    if host in ("doi.org", "dx.doi.org"):
        q.doi = norm_doi(path.lstrip("/"))
    elif host.endswith("arxiv.org"):
        q.arxiv = norm_arxiv(path.split("/", 2)[-1] if path.count("/") >= 2 else path)
    elif host == "openreview.net":
        oid = (parse_qs(u.query).get("id") or [""])[0]
        if oid:
            q.ids["openreview"] = oid
    elif host == "aclanthology.org":
        m = re.match(r"^/([\w.-]+?)(?:\.pdf|\.bib|/)?$", path)
        if m:
            q.ids["acl"] = m.group(1)
    elif host == "semanticscholar.org" or host == "api.semanticscholar.org":
        m = re.search(r"([0-9a-f]{40})", path) or re.search(r"corpusid:?(\d+)", url, re.I)
        if m:
            q.ids["s2"] = m.group(1) if len(m.group(1)) == 40 else f"CorpusId:{m.group(1)}"
    elif host in ("pubmed.ncbi.nlm.nih.gov",):
        m = re.search(r"/(\d{5,9})", path)
        if m:
            q.ids["pmid"] = m.group(1)
    elif host.endswith("ncbi.nlm.nih.gov") and "/pmc/articles/" in path.lower():
        m = re.search(r"(PMC\d+)", path, re.I)
        if m:
            q.ids["pmcid"] = m.group(1).upper()
    elif host.endswith("hal.science") or host.endswith("archives-ouvertes.fr"):
        m = re.search(r"((?:hal|tel|inria|cea|halshs|hal-\w+)-\d{6,8})", path)
        if m:
            q.ids["hal"] = m.group(1)
    elif host.endswith("dblp.org") or host.endswith("dblp.uni-trier.de"):
        m = re.search(r"/rec/(?:bibtex/)?([\w/.-]+?)(?:\.html|\.bib|\.xml)?$", path)
        if m:
            q.ids["dblp"] = m.group(1)
    elif host == "openalex.org" or host == "api.openalex.org":
        m = re.search(r"(W\d+)", path)
        if m:
            q.ids["openalex"] = m.group(1)
    if not q.has_ids():
        m = DOI_RE.search(path)
        if m:
            q.doi = norm_doi(m.group(1))
            # biorxiv/medrxiv : « ...v2 » et « .full » collés au DOI
            q.doi = re.sub(r"(v\d+)?(\.full(\.pdf)?|\.abstract|\.pdf)?$", "", q.doi)
    if q.doi and arxiv_from_doi(q.doi):
        q.arxiv, q.doi = arxiv_from_doi(q.doi), ""
    if not q.has_ids():
        q.kind = "url"
    elif is_official(url):
        q.kind = "id"
    return q


def _surnames(segment: str) -> list[str]:
    out = []
    for w in re.findall(r"[A-ZÀ-Þ][\w'’-]+", segment):
        k = family_key(w)
        if len(k) == 2 and (not w[1:2].islower() or k in _STOP2):
            continue   # « In », « AI »...
        if len(k) >= 2 and k not in _STOP and w.lower() not in _STOP:
            out.append(w)
    return out[:6]


# Coupures de fin de ligne (texte copié d'un PDF) : « learn-\ning » est recollé,
# mais un mot composé coupé sur son trait d'union (« self-\nsupervised ») le garde.
_COMPOUND_HEADS = {
    "self", "multi", "non", "semi", "cross", "meta", "end", "state", "large", "small", "low", "high", "fine",
    "coarse", "zero", "few", "one", "two", "three", "long", "short", "real", "open", "closed", "deep", "graph",
    "time", "domain", "task", "model", "data", "text", "image", "video", "vision", "language", "object",
    "point", "knowledge", "context", "weakly", "fully", "well", "world", "human", "machine", "quasi",
    "pseudo", "mixed", "single", "black", "white", "sequence",
}
_COMPOUND_TAILS = {
    "based", "aware", "free", "level", "scale", "shot", "wise", "driven", "guided", "specific", "agnostic",
    "invariant", "efficient", "supervised", "unsupervised", "trained", "training", "oriented", "dependent",
    "independent", "consistent", "conditioned", "conditional", "augmented", "resolution", "dimensional",
    "grained", "tuning", "tuned", "modal", "art", "the", "of", "to", "end", "like", "preserving",
    "constrained", "regularized", "box", "centric", "friendly", "sensitive", "robust", "only", "wide", "rank",
}


def _dehyphenate(s: str) -> str:
    def join(m: re.Match) -> str:
        head, tail = m.group(1), m.group(2)
        keep = head.lower() in _COMPOUND_HEADS or tail.lower() in _COMPOUND_TAILS
        return f"{head}-{tail}" if keep else head + tail
    return re.sub(r"(\w+)-[ \t]*\r?\n\s*([a-z]\w*)", join, s)


# Points qui ne terminent pas une phrase : initiales (« K. »), « et al. », « vs. »...
_NO_BREAK = re.compile(r"(?:(?:^|[\s.(\-])[A-ZÀ-Þ]|\bet al|\bvs|\be\.g|\bi\.e|\bcf|\bJr|\bSr|\bSt|\bDr|\bProf|"
                       r"\b[Vv]ol|\b[Nn]o|\bpp|\bEq|\bFig|\beds?)$")
_YEAR_SENT = re.compile(r"^\(?(?:19|20)\d\d[a-z]?\)?[.,]?$")
_VENUE_START = re.compile(r"^(in\b|in:|proceedings\b|advances in\b|arxiv\b|corr\b|journal\b|transactions\b|"
                          r"retrieved\b|available\b|url\b|doi\b|https?:)", re.I)


def _sentences(s: str) -> list[str]:
    out, start = [], 0
    for m in re.finditer(r"[.?!](?=\s|$)", s):
        if m.group() == "." and _NO_BREAK.search(s[start:m.start()]):
            continue
        piece = s[start:m.end()].strip()
        if piece:
            out.append(piece)
        start = m.end()
    rest = s[start:].strip()
    if rest:
        out.append(rest)
    return out


def _authors_like(seg: str) -> bool:
    """« K. He, X. Zhang, and J. Sun » / « He, K., & Sun, J. (2016) »."""
    seg = re.sub(r"\(?(?:19|20)\d\d[a-z]?\)?", "", seg).strip(" .,")
    if not seg or re.search(r"\d", seg) or len(seg) > 800:
        return False
    if not re.search(r"[A-ZÀ-Þ][\w'’-]+", seg):
        return False
    return "," in seg or " and " in seg or "&" in seg or len(seg.split()) <= 4


def _reference_title(s: str) -> str:
    """Titre dans une référence formatée : guillemets (IEEE, MLA), sinon la phrase
    qui suit la liste d'auteurs (APA, NeurIPS/natbib, ACL, Vancouver)."""
    m = re.search(r"[\"“«]\s*(.{12,300}?)\s*[,.]?\s*[\"”»]", s)
    if m:
        return m.group(1).strip(" ,.")
    sents = _sentences(s)
    if len(sents) >= 2 and _authors_like(sents[0]):
        i = 2 if _YEAR_SENT.match(sents[1]) and len(sents) >= 3 else 1   # « Auteurs. 2019. Titre. »
        cand = sents[i].rstrip(".").strip()
        if 12 <= len(cand) <= 300 and not _VENUE_START.match(cand) and not _YEAR_SENT.match(cand):
            return cand
    # Vancouver : « He K, Zhang X, Sun J. Titre. In: ... »
    m = re.match(r"^(?:[A-ZÀ-Þ][\w'’-]+(?: [A-ZÀ-Þ][\w'’-]+)* [A-Z]{1,3}, )*[A-ZÀ-Þ][\w'’-]+"
                 r"(?: [A-ZÀ-Þ][\w'’-]+)* [A-Z]{1,3}(?:, et al)?\.\s+(.{12,300}?[^A-Z])[.?]\s", s + " ")
    if m:
        return m.group(1).strip()
    m = re.search(r"\b(?:19|20)\d\d[a-z]?\.\s+(.{12,300}?)\.\s", s + " ")      # « Auteurs. 2019. Titre. »
    if m and not _REF_MARKERS.match(m.group(1)):
        return m.group(1).strip()
    return ""


def parse_input(raw: str) -> Query:
    s = (raw or "").strip()
    s = re.sub(r"[\N{ZERO WIDTH SPACE}\N{SOFT HYPHEN}]", "", s)
    if not s:
        return Query(raw="", kind="title")
    # Entrée BibTeX collée : on cherche la bonne version de CE papier.
    if s.startswith("@"):
        e = bibtex.parse_one(s)
        if e:
            q = Query(raw=s, kind="bibtex", title=clean_title(e.text("title")),
                      authors=[a.family for a in e.authors()][:6], year=year_of(e.get("year")),
                      doi=norm_doi(e.get("doi")))
            ep = e.get("eprint")
            if ep and (e.get("archiveprefix").lower() == "arxiv" or ARXIV_NEW_RE.fullmatch(ep)):
                q.arxiv = norm_arxiv(ep)
            jr = e.text("journal")
            m = re.search(r"arxiv[:\s]*(\d{4}\.\d{4,5})", jr + " " + e.get("volume"), re.I)
            if m and not q.arxiv:
                q.arxiv = m.group(1)
            if q.doi and arxiv_from_doi(q.doi):
                q.arxiv, q.doi = arxiv_from_doi(q.doi), ""
            url = e.get("url")
            if url and not q.has_ids():
                uq = _url_query(url)
                if uq and uq.has_ids():
                    q.doi, q.arxiv, q.ids = uq.doi, uq.arxiv, uq.ids
            q.original = {"type": e.type, "key": e.key, "fields": e.fields}
            return q
    if URL_RE.match(s) and not ARXIV_OLD_RE.fullmatch(s):   # « math.GT/0309136 » n'est pas un lien
        q = _url_query(s)
        if q:
            return q
    one_line = " ".join(_dehyphenate(s).split())
    # Identifiants seuls (éventuellement préfixés).
    bare = re.sub(r"^(doi|arxiv|pmid|pmcid)\s*:?\s*", "", one_line, flags=re.I).strip()
    m = DOI_RE.fullmatch(bare.rstrip(".")) if bare.startswith("10.") else None
    if m:
        doi = norm_doi(bare)
        if arxiv_from_doi(doi):
            return Query(raw=s, kind="id", arxiv=arxiv_from_doi(doi))
        return Query(raw=s, kind="id", doi=doi)
    if ARXIV_NEW_RE.fullmatch(bare) or ARXIV_OLD_RE.fullmatch(bare):
        return Query(raw=s, kind="id", arxiv=norm_arxiv(bare))
    if re.fullmatch(r"(?i)pmid\s*:?\s*\d{5,9}", one_line):
        return Query(raw=s, kind="id", ids={"pmid": re.sub(r"\D", "", one_line)})
    if re.fullmatch(r"(?i)pmc\d{5,9}", bare):
        return Query(raw=s, kind="id", ids={"pmcid": bare.upper()})
    # DOI au milieu d'une référence : on le garde comme indice fort.
    doi_in = norm_doi(DOI_RE.search(one_line).group(1)) if DOI_RE.search(one_line) else ""
    arxiv_in = ""
    m = re.search(r"arxiv[:\s/]*(?:abs/)?(\d{4}\.\d{4,5})", one_line, re.I)
    if m:
        arxiv_in = m.group(1)
    text = latex_to_text(one_line) if "\\" in one_line or "{" in one_line else one_line
    words = text.split()
    # Les identifiants (arXiv 2002.05709, DOI) ne donnent pas l'année.
    dated = ARXIV_NEW_RE.sub(" ", DOI_RE.sub(" ", text))
    has_year = bool(re.search(r"\b(19|20)\d\d\b", dated))
    is_ref = (len(words) > 12 and has_year and (text.count(",") >= 2 or _REF_MARKERS.search(text))) \
        or bool(re.search(r"[\"“«].{12,}[\"”»]", text) and has_year) or (doi_in and len(words) > 4)
    if is_ref:
        title = _reference_title(text)
        lead = text.split(title)[0] if title else re.split(r"\b(19|20)\d\d\b", text)[0]
        q = Query(raw=s, kind="reference", text=text, title=clean_title(title) if title else "",
                  authors=_surnames(lead), year=year_of(dated), doi=doi_in if not arxiv_from_doi(doi_in) else "",
                  arxiv=arxiv_in or arxiv_from_doi(doi_in), context=text)
        return q
    if has_year and len(words) <= 15:
        # « Auteurs titre revue 2015 » ou « Titre 2017 » : référence courte. Le titre
        # (sans l'année) sert aux sources qui cherchent par titre ; le texte complet
        # sert à vérifier les auteurs.
        title = re.sub(r"[(\[]?\b(?:19|20)\d\d\b[)\]]?", " ", text)
        title = re.sub(r"\s+", " ", title).strip(" ,.;:")
        return Query(raw=s, kind="reference", text=text, title=clean_title(title), year=year_of(dated), context=text)
    title = text.strip().strip("\"“”«»'").strip()
    title = re.sub(r"\s*\.$", "", title)
    return Query(raw=s, kind="title", title=title, year=None)
