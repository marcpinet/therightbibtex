"""Modèle commun : la requête analysée et la notice normalisée qu'une source
renvoie. Chaque notice décrit UNE version d'un papier (prépublication arXiv,
version publiée, page de proceedings...) vue par UNE source."""
from __future__ import annotations

from dataclasses import dataclass, field

from ..textutil import Author


@dataclass
class Query:
    raw: str = ""
    kind: str = "title"          # title | reference | id | bibtex | pdf | url
    title: str = ""              # meilleure hypothèse de titre
    text: str = ""               # texte libre (référence complète)
    authors: list[str] = field(default_factory=list)   # noms de famille indicatifs
    year: int | None = None
    doi: str = ""
    arxiv: str = ""
    ids: dict = field(default_factory=dict)            # openreview, acl, s2, pmid, pmcid, hal, dblp, openalex
    url: str = ""
    context: str = ""            # ex. texte de la 1re page du PDF (vérification des auteurs)
    venue_hint: str = ""         # ex. « ICLR 2024 » lu dans l'en-tête du PDF
    original: dict | None = None # entrée BibTeX collée / métadonnées du PDF

    def has_ids(self) -> bool:
        return bool(self.doi or self.arxiv or self.ids)

    def public(self) -> dict:
        return {
            "kind": self.kind, "title": self.title, "text": self.text if self.kind == "reference" else "",
            "authors": self.authors, "year": self.year, "doi": self.doi, "arxiv": self.arxiv,
            "ids": self.ids, "url": self.url, "venue_hint": self.venue_hint,
        }


@dataclass
class Record:
    source: str
    title: str = ""
    authors: list[Author] = field(default_factory=list)
    year: int | None = None
    month: int | None = None
    year_weak: bool = False      # année devinée (date de dépôt du DOI), pas de publication
    date: str = ""               # date de CETTE version (ISO)
    kind: str = "other"          # journal | conference | preprint | chapter | book | thesis | report | dataset | software | reference | other
    venue: str = ""
    venue_short: str = ""
    volume: str = ""
    number: str = ""
    pages: str = ""
    publisher: str = ""
    series: str = ""
    isbn: str = ""
    issn: str = ""
    school: str = ""
    institution: str = ""
    doi: str = ""
    arxiv: str = ""
    arxiv_version: int | None = None
    arxiv_class: str = ""
    ids: dict = field(default_factory=dict)
    url: str = ""
    pdf_url: str = ""
    bibtex: str = ""             # BibTeX brut fourni par la source
    bibtex_label: str = ""
    abstract: str = ""
    citations: int | None = None
    status: str = ""             # published | preprint | submitted
    status_note: str = ""        # « ICLR 2024 poster », « Accepted at ICML (commentaire arXiv) »
    retracted: bool = False
    notices: list = field(default_factory=list)       # avertissements (rétractation, correction)
    related: list = field(default_factory=list)       # [('doi', x), ('arxiv', y)] autres versions déclarées
    link: str = ""               # page de la notice chez la source
    weight: float = 50.0         # autorité de la source pour cette notice
    aggregate: bool = False      # décrit l'œuvre en mélangeant les versions (S2, Scholar)
    incomplete_authors: bool = False
    score: float = 0.0           # adéquation à la requête (calculée par le moteur)
    rank: int = 0                # rang dans la réponse de la source


class Ctx:
    """Contexte d'une recherche : client HTTP, options, journal des sources, et clés
    d'API (config.json). scholar_direct : lire Google Scholar sans SerpAPI, désactivé
    par défaut (souvent bloqué, et contraire aux conditions d'utilisation de Google)."""

    def __init__(self, http, refresh: bool = False, emit=None, keys: dict | None = None,
                 scholar_direct: bool = False):
        self.http = http
        self.refresh = refresh
        self.emit = emit or (lambda ev: None)
        self.scholar_direct = scholar_direct
        keys = keys or {}
        # Plusieurs clés par service, essayées dans l'ordre : si l'une est refusée ou
        # épuisée, la suivante prend le relais.
        as_list = lambda v: [k for k in (v if isinstance(v, (list, tuple)) else [v]) if k]
        self.s2_keys = as_list(keys.get("s2"))
        self.serpapi_keys = as_list(keys.get("serpapi"))
        self.openalex_keys = as_list(keys.get("openalex"))


LABELS = {
    "crossref": "Crossref", "doi": "doi.org", "datacite": "DataCite", "arxiv": "arXiv",
    "s2": "Semantic Scholar", "openalex": "OpenAlex", "dblp": "DBLP", "openreview": "OpenReview",
    "europepmc": "Europe PMC", "hal": "HAL", "inspire": "INSPIRE-HEP", "zbmath": "zbMATH",
    "scholar": "Google Scholar", "pages": "Official sites", "proceedings": "Official proceedings",
    "acl": "ACL Anthology", "pdf": "PDF", "web": "Web page", "cite": "Abbreviation (built-in)",
}

# Autorité de base de chaque source (départage les champs contradictoires).
WEIGHTS = {
    "proceedings": 100, "acl": 100, "openreview": 95, "crossref": 90, "dblp": 88, "inspire": 86,
    "web": 84, "doi": 82, "europepmc": 80, "arxiv": 76, "datacite": 72, "hal": 70, "zbmath": 70,
    "s2": 60, "openalex": 55, "scholar": 40, "pdf": 30,
}
