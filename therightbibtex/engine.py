"""Moteur de recherche : interroge toutes les sources en parallèle, regroupe
les notices par œuvre puis par version (prépublication / publiée), complète
les meilleures pistes par identifiant (DOI, arXiv, DBLP, pages officielles),
puis fusionne une notice BibTeX en gardant la provenance de chaque champ.

Les événements (état des sources, résultats provisoires puis définitifs)
sont émis au fil de l'eau pour que l'interface se remplisse progressivement.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
import re
import time
from collections import Counter, defaultdict

from rapidfuzz import fuzz

from . import bibtex
from . import venues
from .sources import (arxiv, crossref, datacite, dblp, doi as doi_src, europepmc, hal, inspire,
                     openalex, openreview, pages, s2, scholar, zbmath)
from .sources.base import LABELS, Ctx, Query, Record
from .sources.http import SourceError
from . import pdfinfo
from .textutil import (
    TITLE_STOPWORDS, Author, arxiv_doi, authors_overlap, family_key, is_preprint_doi, is_repository_doi,
    is_short_name, main_title_match, norm_pages, norm_title, title_in_text, title_match, title_similarity,
)

logger = logging.getLogger(__name__)

SEARCHERS = {
    "crossref": crossref.search, "openalex": openalex.search, "s2": s2.search, "dblp": dblp.search,
    "arxiv": arxiv.search, "openreview": openreview.search, "europepmc": europepmc.search,
    "hal": hal.search, "inspire": inspire.search, "datacite": datacite.search, "zbmath": zbmath.search,
    "scholar": scholar.search,
}
TIMEOUTS = {"dblp": 25, "scholar": 22, "s2": 16, "arxiv": 20, "openreview": 16}   # scholar : SerpAPI, 20 s max
DEFAULT_TIMEOUT = 14
TOP_CLUSTERS = 3
EXPAND_BUDGET = 9.0
FULL_TITLE_BUDGET = 10.0
# Recherche par identifiant : une fois une notice fiable reçue, délai laissé aux
# autres sources avant de lancer la recherche par titre (elles continuent en fond).
IDS_GRACE = 1.5
PUBLISHED_KINDS = {"journal", "conference", "chapter", "book", "thesis", "report"}


# =================================================================== regroupement

class _UF:
    def __init__(self, n):
        self.p = list(range(n))

    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[max(ra, rb)] = min(ra, rb)


def _id_keys(r: Record) -> set[str]:
    keys = set()
    if r.doi:
        keys.add("doi:" + r.doi)
    if r.arxiv:
        keys.add("arxiv:" + r.arxiv)
        keys.add("doi:" + arxiv_doi(r.arxiv))
    for k in ("s2", "dblp", "openalex", "openreview", "pmid", "pmcid", "hal", "acl", "inspire", "corpus"):
        v = r.ids.get(k)
        if v:
            keys.add(f"{k}:{str(v).lower()}")
    for rel in r.related:
        kind, val = rel[0], rel[1]
        if val:
            keys.add(f"{kind}:{val.lower()}")
    return keys


# Mots qui font d'un titre une autre œuvre que l'article (« Invited talk: X »,
# « Erratum to: X », « Reply to the comment on X »...).
_DISTINCT_MARKERS = re.compile(
    r"\b(invited|talk|keynote|erratum|errata|correction|corrigendum|retraction|retracted|reply|response|"
    r"comment|comments|commentary|supplementary|supplement|supplemental|poster|demo|demonstration|tutorial|"
    r"review|preface|abstracts|summary|withdrawn|addendum|discussion|rejoinder|retrospective|reproducibility|"
    r"replication)\b")

# Données ou code déposés à côté d'un article (« Replication Data for: "X" » sur un
# entrepôt de données), entrées de dictionnaire (« Bert, J. ») : jamais l'article
# lui-même, même si leur titre le contient ou lui ressemble.
_DATA_KINDS = ("dataset", "software", "reference")
_SUPPLEMENT_TITLE = re.compile(
    r"^(replication|supplementary|supplemental|supporting|reproducibility|reproduction)\s+"
    r"(data|dataset|datasets|code|software|materials?|information|files?|package)\b")


def is_supplement(r: Record) -> bool:
    return r.kind in _DATA_KINDS or bool(_SUPPLEMENT_TITLE.match(norm_title(r.title)))


def _same_work(a: Record, b: Record) -> bool:
    ta, tb = norm_title(a.title), norm_title(b.title)
    if not ta or not tb:
        return False
    if set(_DISTINCT_MARKERS.findall(ta)) != set(_DISTINCT_MARKERS.findall(tb)):
        return False
    if is_supplement(a) != is_supplement(b):
        return False
    sim = 1.0 if ta == tb else title_similarity(a.title, b.title)
    if sim < 0.9:
        return False
    ov = authors_overlap(a.authors, b.authors)
    if ov != -1.0 and ov < 0.5:
        return False
    if a.year and b.year and abs(a.year - b.year) > 3:
        return False
    if len(ta.split()) <= 3:  # titres très courts : on exige auteurs ET année
        if ov == -1.0 or not (a.year and b.year and abs(a.year - b.year) <= 1):
            return False
    # Titre presque identique, ou titre proche avec des auteurs largement communs.
    return sim >= 0.94 or ov >= 0.75


def _titles_agree(a: Record, b: Record) -> bool:
    """Deux notices qui partagent un identifiant décrivent-elles bien le même texte ?
    Les auteurs communs suffisent (titre traduit) ; sinon les titres doivent concorder."""
    # 0,6 : « Generative adversarial nets » / « ... Networks » (0,65) ou un sous-titre absent
    # passent ; deux papiers qui partagent « time series anomaly detection » (0,51), non.
    if max(title_match(a.title, b.title), title_match(b.title, a.title)) >= 0.6:
        return True
    ov = authors_overlap(a.authors, b.authors)
    return ov >= 0.5 and not (a.year and b.year and abs(a.year - b.year) > 2)


# Qui fait foi pour un identifiant : l'agence d'enregistrement du DOI, arXiv pour son numéro.
_DOI_AUTHORITIES = {"doi", "crossref", "datacite"}


def drop_foreign_ids(records: list[Record]):
    """OpenAlex colle parfois à une prépublication arXiv le DOI d'un tout autre papier
    (« GPT-4 Technical Report » avec le DOI d'un article de COSIT 2024). Quand la
    notice qui fait foi pour cet identifiant décrit un autre texte, la notice qui le
    porte à tort le perd : ni regroupement, ni DOI étranger dans le BibTeX."""
    by_doi: dict[str, list[Record]] = defaultdict(list)
    by_arxiv: dict[str, list[Record]] = defaultdict(list)
    for r in records:
        if r.doi and r.source in _DOI_AUTHORITIES:
            by_doi[r.doi].append(r)
        if r.arxiv and r.source == "arxiv":
            by_arxiv[r.arxiv].append(r)
    for r in records:
        if r.doi and r.source not in _DOI_AUTHORITIES and by_doi.get(r.doi) \
                and not any(_titles_agree(a, r) for a in by_doi[r.doi]):
            r.doi = ""
            r.ids.pop("doi_raw", None)
        if r.arxiv and r.source != "arxiv" and by_arxiv.get(r.arxiv) \
                and not any(_titles_agree(a, r) for a in by_arxiv[r.arxiv]):
            r.arxiv = ""


def cluster(records: list[Record]) -> list[list[Record]]:
    drop_foreign_ids(records)
    n = len(records)
    uf = _UF(n)
    # Chaque identifiant peut avoir plusieurs « propriétaires » quand des notices le
    # partagent sans décrire le même texte : on ne relie que celles qui concordent.
    owners: dict[str, list[int]] = defaultdict(list)
    for i, r in enumerate(records):
        for k in _id_keys(r):
            match = next((o for o in owners[k] if _titles_agree(records[o], r)), None)
            if match is None:
                owners[k].append(i)
            else:
                uf.union(match, i)
    norm = [norm_title(r.title) for r in records]
    for i in range(n):
        for j in range(i + 1, n):
            if uf.find(i) == uf.find(j):
                continue
            # préfiltre bon marché avant la comparaison complète
            if abs(len(norm[i]) - len(norm[j])) > max(12, 0.25 * max(len(norm[i]), len(norm[j]))):
                continue
            if _same_work(records[i], records[j]):
                uf.union(i, j)
    groups: dict[int, list[Record]] = defaultdict(list)
    for i, r in enumerate(records):
        groups[uf.find(i)].append(r)
    return list(groups.values())


# =================================================================== versions

def _status(r: Record) -> str:
    if r.status == "submitted":
        return "submitted"
    if r.kind == "preprint" or r.status == "preprint" or is_preprint_doi(r.doi):
        return "preprint"
    if r.kind in _DATA_KINDS or is_repository_doi(r.doi):
        return "other"
    if r.status == "published" and r.kind in PUBLISHED_KINDS | {"other"}:
        return "published" if r.kind != "other" or r.venue else "other"
    return "other"


def _version_key(r: Record, st: str):
    if st == "preprint":
        if r.arxiv:
            return ("pre", "arxiv:" + r.arxiv)
        if r.doi:
            return ("pre", "doi:" + r.doi)
        return ("pre", r.source + ":" + (r.ids.get("hal") or r.ids.get("openreview") or r.title[:40]))
    if st == "submitted":
        return ("sub", r.ids.get("openreview", r.source))
    if st == "published":
        if r.doi:
            return ("pub", "doi:" + r.doi)
        return ("pubnd", venues.family(r.venue or r.venue_short) or r.source)
    return ("other", r.doi or r.source + str(id(r)))


def build_versions(records: list[Record]) -> list[dict]:
    groups: dict[tuple, list[Record]] = defaultdict(list)
    aggregates: list[Record] = []
    for r in records:
        st = _status(r)
        if r.aggregate:
            aggregates.append(r)
            continue
        groups[_version_key(r, st)].append(r)

    # Versions publiées sans DOI (NeurIPS, ICLR, PMLR, JMLR...) : fusion entre
    # elles si même revue/conférence (abréviations comprises), puis rattachement
    # au DOI de la même publication s'il existe.
    def venues_of(recs):
        return [x for r in recs for x in (r.venue, r.venue_short) if x]

    def years_close(r1, r2):
        y1 = {r.year for r in r1 if r.year}
        y2 = {r.year for r in r2 if r.year}
        return not y1 or not y2 or min(abs(a - b) for a in y1 for b in y2) <= 1

    def compatible(r1, r2):
        return years_close(r1, r2) and any(venues.compatible(a, b) for a in venues_of(r1) for b in venues_of(r2))

    nd = [k for k in groups if k[0] == "pubnd"]
    for i, k in enumerate(nd):
        if k not in groups:
            continue
        for k2 in nd[i + 1:]:
            if k2 in groups and compatible(groups[k], groups[k2]):
                groups[k].extend(groups.pop(k2))
    # Notice publiée sans revue ni DOI (OpenAlex en a) : on la range avec la seule
    # autre version publiée de la même époque.
    for k in [k for k in groups if k[0] == "pubnd" and not venues_of(groups[k])]:
        others = [o for o in groups if o != k and o[0] in ("pub", "pubnd") and venues_of(groups[o])]
        if len(others) == 1 and years_close(groups[k], groups[others[0]]):
            groups[others[0]].extend(groups.pop(k))
    doi_keys = [k for k in groups if k[0] == "pub"]
    nd = [k for k in groups if k[0] == "pubnd"]
    for k in nd:
        target = next((dk for dk in doi_keys if compatible(groups[k], groups[dk])), None)
        if target is None and len(doi_keys) == 1 and len(nd) == 1 and years_close(groups[k], groups[doi_keys[0]]):
            target = doi_keys[0]
        if target:
            groups[target].extend(groups.pop(k))
    # Les notices « agrégées » (S2, Scholar) vont sur la version qu'elles décrivent.
    for r in aggregates:
        st = _status(r)
        key = None
        if st == "published":
            if r.doi and ("pub", "doi:" + r.doi) in groups:
                key = ("pub", "doi:" + r.doi)
            else:
                fam = venues.family(r.venue or r.venue_short)
                for k in groups:
                    if k[0] in ("pub", "pubnd") and (k[1] == fam or compatible([r], groups[k])):
                        key = k
                        break
                if key is None:
                    pubs = [k for k in groups if k[0] in ("pub", "pubnd")]
                    key = pubs[0] if len(pubs) == 1 else ("pubnd", fam or r.source)
        elif r.arxiv and ("pre", "arxiv:" + r.arxiv) in groups:
            key = ("pre", "arxiv:" + r.arxiv)
        else:
            pres = [k for k in groups if k[0] == "pre"]
            key = pres[0] if pres else ("pre", "agg:" + r.source) if st == "preprint" else None
            if key is None:
                pubs = [k for k in groups if k[0] in ("pub", "pubnd")]
                key = pubs[0] if pubs else ("other", r.source)
        groups[key].append(r)
    out = []
    for key, recs in groups.items():
        st = {"pre": "preprint", "sub": "submitted", "pub": "published", "pubnd": "published"}.get(key[0], "other")
        # Un groupe qui ne contient que des notices agrégées « publiées » sans source primaire
        # reste une version publiée (S2 sait parfois ce qu'on ne trouve pas ailleurs).
        out.append({"key": key, "status": st, "records": recs})
    return out


# =================================================================== fusion des champs

FIELD_BONUS = {
    "title": {"dblp": 10, "proceedings": 8, "acl": 8, "arxiv": 8, "openreview": 6, "crossref": -2,
              "scholar": -30, "s2": -5, "openalex": -8, "pdf": -40},
    "authors": {"proceedings": 6, "acl": 6, "crossref": 4, "openreview": 2, "arxiv": 2, "dblp": -2,
                "scholar": -60, "openalex": -10, "pdf": -60},
    "venue_conference": {"proceedings": 15, "acl": 15, "openreview": 8, "crossref": 6, "dblp": 0, "doi": 4,
                         "s2": -25, "openalex": -30, "scholar": -60, "web": 4},
    "venue_journal": {"crossref": 12, "europepmc": 8, "doi": 8, "proceedings": 10, "inspire": 0, "dblp": -15,
                      "s2": -20, "openalex": -20, "scholar": -60, "web": 4},
    "numbers": {"crossref": 8, "proceedings": 8, "acl": 8, "europepmc": 5, "dblp": 2, "inspire": 4,
                "s2": -20, "openalex": -15, "scholar": -60},
    "publisher": {"crossref": 10, "proceedings": 10, "acl": 10, "doi": 5, "dblp": 0, "openalex": -30, "s2": -40},
    "year": {"proceedings": 10, "acl": 10, "dblp": 8, "openreview": 8, "crossref": 4, "arxiv": 6,
             "scholar": -20, "openalex": -10, "s2": -5},
}


def _weight(r: Record, field: str) -> float:
    w = r.weight + FIELD_BONUS.get(field, {}).get(r.source, 0)
    if field == "authors":
        if not r.authors:
            return -1e9
        if r.incomplete_authors or all(a.initials_only() for a in r.authors if not a.corporate):
            w -= 40
    return w


def _pick(records: list[Record], field: str, getter, bonus_field: str | None = None):
    best, best_w, src = None, -1e18, ""
    for r in records:
        v = getter(r)
        if v in (None, "", []):
            continue
        w = _weight(r, bonus_field or field)
        if w > best_w:
            best, best_w, src = v, w, r.source
    return best, src


def _author_consensus(records: list[Record]):
    """Liste d'auteurs de la meilleure source, mais seulement parmi celles dont
    le nombre d'auteurs est cohérent avec la majorité (certaines notices sont
    tronquées ou listent un consortium)."""
    cands = [r for r in records if r.authors and not r.incomplete_authors]
    if not cands:
        cands = [r for r in records if r.authors]
    if not cands:
        return [], ""
    counts = Counter(len(r.authors) for r in cands)
    top_count, top_n = counts.most_common(1)[0]
    if top_n == 1:
        top_count = max(counts)  # pas de majorité : la liste la plus longue
    pool = [r for r in cands if len(r.authors) == top_count] or cands
    best = max(pool, key=lambda r: _weight(r, "authors"))
    return best.authors, best.source


def _year_vote(records: list[Record]):
    votes: dict[int, float] = defaultdict(float)
    best: dict[int, tuple[float, str]] = {}
    for r in records:
        if r.year:
            w = 1.0 if r.year_weak else max(1.0, _weight(r, "year"))
            votes[r.year] += w
            if r.year not in best or w > best[r.year][0]:
                best[r.year] = (w, r.source)
    if not votes:
        return None, ""
    y = max(votes, key=votes.get)
    return y, best[y][1]


_ETYPE = {"conference": "inproceedings", "journal": "article", "chapter": "incollection", "book": "book",
          "thesis": "phdthesis", "report": "techreport", "preprint": "misc", "dataset": "misc",
          "software": "misc", "other": "misc"}


def _enrich_authors(authors: list[Author], pool: list[Record]) -> list[Author]:
    """Initiales -> prénoms complets trouvés dans une autre notice de la même
    œuvre (même nom de famille, même initiale)."""
    if len(authors) > 300:
        return authors
    index: dict[str, list[Author]] = defaultdict(list)
    wanted = {a.key() for a in authors}
    for r in pool:
        if r.incomplete_authors or len(r.authors) > 3000:
            continue
        for b in r.authors:
            k = b.key()
            if k in wanted and b.given and not b.initials_only():
                index[k].append(b)
    out = []
    for a in authors:
        if not a.corporate and (not a.given or a.initials_only()):
            ini = a.given[:1].lower()
            cands = [b for b in index.get(a.key(), []) if not ini or b.given[:1].lower() == ini]
            if cands:
                best = max(cands, key=lambda b: len(b.given))
                a = Author(best.given, best.family if len(best.family) >= len(a.family) else a.family)
        out.append(a)
    return out


def merge_version(v: dict, pool: list[Record] | None = None) -> dict:
    recs: list[Record] = v["records"]
    primary = [r for r in recs if not r.aggregate] or recs
    status = v["status"]
    kinds = Counter(r.kind for r in primary if r.kind not in ("other",))
    if status == "preprint":
        kind = "preprint"
    else:
        # la page officielle / Crossref décident du type ; sinon la majorité
        authority = sorted(primary, key=lambda r: -r.weight)
        kind = next((r.kind for r in authority if r.kind in PUBLISHED_KINDS), None) \
            or (kinds.most_common(1)[0][0] if kinds else "other")
        # Actes publiés comme chapitres (LNCS...) : c'est un article de conférence.
        if kind == "chapter" and any(r.kind == "conference" for r in primary):
            kind = "conference"
    prov = {}
    spec: dict = {"kind": kind, "status": status, "preprint": status in ("preprint", "submitted")}
    title, prov["title"] = _pick(recs, "title", lambda r: r.title)
    spec["title"] = title or ""
    authors, prov["authors"] = _author_consensus(recs)
    if pool and any(a.initials_only() or not a.given for a in authors if not a.corporate):
        authors = _enrich_authors(authors, pool)
    spec["authors"] = [a.to_dict() for a in authors]
    year, prov["year"] = _year_vote(primary if any(r.year for r in primary) else recs)
    spec["year"] = year
    vf = "venue_journal" if kind == "journal" else "venue_conference"
    if status == "preprint":
        arx = next((r for r in recs if r.arxiv), None)
        spec["arxiv"] = arx.arxiv if arx else ""
        cls_rec = next((r for r in sorted(recs, key=lambda r: r.source != "arxiv") if r.arxiv_class), None)
        spec["arxiv_class"] = cls_rec.arxiv_class if cls_rec else ""
        if cls_rec:
            prov["arxiv_class"] = cls_rec.source
        if spec["arxiv"]:
            prov["arxiv"] = next((r.source for r in recs if r.source == "arxiv"), arx.source)
        server = next((r.venue for r in sorted(primary, key=lambda r: -r.weight)
                       if r.venue and r.venue.lower() not in ("arxiv", "openreview", "preprint")), "")
        spec["venue"] = "" if spec["arxiv"] else server
        if spec["venue"]:
            prov["venue"] = next(r.source for r in primary if r.venue == server)
        doi = next((r.doi for r in primary if r.doi and not r.doi.startswith("10.48550/")), "")
        spec["doi"] = "" if spec["arxiv"] else doi
        spec["url"] = f"https://arxiv.org/abs/{spec['arxiv']}" if spec["arxiv"] else \
            next((r.url for r in primary if r.url), "")
    else:
        venue, prov["venue"] = _pick([r for r in recs if not venues.is_preprint_venue(r.venue)], vf,
                                     lambda r: r.venue, vf)
        # Nom abrégé (« J. Mach. Learn. Res. », DBLP/INSPIRE) : on prend la forme
        # complète donnée par une autre source si elle désigne la même revue.
        if venue and re.search(r"[A-Za-z]\.", venue):
            full = [r for r in recs if r.venue and not re.search(r"[A-Za-z]\.", r.venue)
                    and venues.compatible(r.venue, venue)]
            if full:
                best = max(full, key=lambda r: (_weight(r, vf), len(r.venue)))
                venue, prov["venue"] = best.venue, best.source
        spec["venue"] = venue or ""
        short, short_src = _short_venue(spec["venue"], recs, kind)
        if short:
            spec["venue_short"], prov["venue_short"] = short, short_src
        for f in ("volume", "number", "pages"):
            val, s = _pick(recs, f, lambda r, f=f: getattr(r, f), "numbers")
            spec[f] = val or ""
            if val:
                prov[f] = s
        pub, s = _pick(primary, "publisher", lambda r: r.publisher if r.source not in ("zbmath", "inspire", "hal", "openalex")
                       and (r.source != "dblp" or kind != "journal") else "")
        if pub and kind in ("conference", "journal", "chapter", "book", "report", "thesis"):
            spec["publisher"], prov["publisher"] = pub, s
        doi = v["key"][1][4:] if v["key"][0] == "pub" and v["key"][1].startswith("doi:") else \
            next((r.doi for r in primary if r.doi), "")
        spec["doi"] = doi
        if doi:
            prov["doi"] = next((r.source for r in primary if r.doi == doi), "")
        spec["url"] = next((r.url for r in sorted(primary, key=lambda r: -_weight(r, "venue_conference"))
                            if r.url and "doi.org" not in r.url), "") or (f"https://doi.org/{doi}" if doi else "")
        series, s = _pick(primary, "series", lambda r: r.series if kind in ("conference", "chapter", "book") else "")
        if series:
            spec["series"], prov["series"] = series, s
            if not spec.get("volume"):
                svol = next((r.ids["series_volume"] for r in primary if r.ids.get("series_volume")), "")
                if svol:
                    spec["volume"], prov["volume"] = svol, "dblp"
        isbn = next((r.isbn for r in primary if r.isbn), "")
        if isbn:
            spec["isbn"] = isbn
        if kind == "thesis":
            spec["school"] = next((r.school for r in primary if r.school), spec["venue"])
        if kind == "report":
            spec["institution"] = next((r.institution for r in primary if r.institution), spec["venue"])
    # Dépôt HAL de cette version : hal_id / hal_version, comme dans l'export BibTeX de HAL.
    hal_rec = next((r for r in sorted(primary, key=lambda r: r.source != "hal") if r.ids.get("hal")), None)
    if hal_rec:
        spec["hal"], prov["hal"] = hal_rec.ids["hal"], hal_rec.source
        spec["hal_version"] = hal_rec.ids.get("hal_version", "")
    month = next((r.month for r in sorted(primary, key=lambda r: -r.weight) if r.month and r.year == year), None)
    if month:
        spec["month"] = month
    spec["abstract"] = next((r.abstract for r in sorted(recs, key=lambda r: -len(r.abstract or "")) if r.abstract), "")
    spec["dblp"] = next((r.ids["dblp"] for r in recs if r.ids.get("dblp")), "")
    spec["type"] = _ETYPE.get(kind, "misc")
    if kind == "thesis" and any("master" in (r.bibtex or "").lower()[:40] for r in recs):
        spec["type"] = "mastersthesis"
    spec["prov"] = {k: v for k, v in prov.items() if v}
    return spec


def _conflicts(recs: list[Record], spec: dict) -> list[dict]:
    """Désaccords entre sources fiables sur la version retenue."""
    out = []
    strong = [r for r in recs if r.weight >= 70 and not r.aggregate]
    years = defaultdict(set)
    for r in strong:
        if r.year and not r.year_weak:
            years[r.year].add(LABELS.get(r.source, r.source))
    if len(years) > 1:
        out.append({"field": "year", "values": [{"value": str(y), "sources": sorted(s)} for y, s in sorted(years.items())]})
    pgs = defaultdict(set)
    for r in strong:
        if r.pages:
            pgs[norm_pages(r.pages).replace("--", "-")].add(LABELS.get(r.source, r.source))
    if len(pgs) > 1:
        out.append({"field": "pages", "values": [{"value": p, "sources": sorted(s)} for p, s in pgs.items()]})
    counts = defaultdict(set)
    for r in strong:
        if r.authors and not r.incomplete_authors:
            counts[len(r.authors)].add(LABELS.get(r.source, r.source))
    if len(counts) > 1:
        out.append({"field": "number of authors",
                    "values": [{"value": str(c), "sources": sorted(s)} for c, s in sorted(counts.items())]})
    return out


def _short_venue(full: str, recs: list[Record], kind: str) -> tuple[str, str]:
    """Abréviation de la revue/conférence : table maison (qualificatifs d'atelier,
    de Findings... lus sur le nom complet), puis nom court donné par Crossref (revues),
    DBLP, OpenReview ou Semantic Scholar, puis sigle écrit dans le nom des actes."""
    names = [full] + [r.venue for r in recs if r.venue] + [r.venue_short for r in recs if r.venue_short]
    for name in names:
        sf = venues.short_form(name)
        if sf:
            return sf, "cite"
    if kind == "journal":
        cr = next((r.venue_short for r in recs if r.source == "crossref" and r.venue_short), "")
        if cr:
            return cr, "crossref"
    # « ACL (1) », « ICLR (Poster) » chez DBLP ; « ICLR 2024 » chez OpenReview.
    for src, strip in (("dblp", r"\s*\([^)]*\)"), ("openreview", r"\s+(19|20)\d\d\b.*$"), ("s2", None)):
        for r in recs:
            v = r.venue_short if r.source == src else ""
            if v and not venues.is_preprint_venue(v):
                v = (re.sub(strip, "", v) if strip else v).strip()
                if v and len(v) <= 40:
                    return v, src
    for name in names:
        acr = venues.acronym_in(name)
        if acr:
            return acr, "cite"
    return "", ""


def _version_label(spec: dict, recs: list[Record]) -> str:
    st = spec["status"]
    if st == "preprint":
        if spec.get("arxiv"):
            vmax = max((r.arxiv_version or 0 for r in recs), default=0)
            return f"arXiv {spec['arxiv']}" + (f"v{vmax}" if vmax else "")
        return spec.get("venue") or "Preprint"
    if st == "submitted":
        note = next((r.status_note for r in recs if r.status_note), "")
        return note or "OpenReview submission"
    short = spec.get("venue_short") or next(
        (r.venue_short for r in recs if r.venue_short and len(r.venue_short) < 30), "")
    venue = short or (spec.get("venue") or "")[:60]
    return f"{venue} {spec.get('year') or ''}".strip() or "Published version"


def _source_entries(recs: list[Record]) -> list[dict]:
    seen, out = set(), []
    for r in sorted(recs, key=lambda r: -r.weight):
        ident = (r.source, r.link or r.url or r.title)
        if ident in seen:
            continue
        seen.add(ident)
        out.append({
            "source": r.source, "label": LABELS.get(r.source, r.source), "link": r.link or r.url,
            "bibtex": r.bibtex, "bibtex_label": r.bibtex_label, "status_note": r.status_note,
            "title": r.title, "year": r.year, "venue": r.venue_short or r.venue,
        })
    return out


# =================================================================== score

def _same_word(a: str, b: str) -> bool:
    return a == b or (len(a) >= 5 and len(b) >= 5 and fuzz.ratio(a, b) >= 88)   # pluriel, faute de frappe


def keyword_score(text: str, r: Record) -> float:
    """Saisie en mots-clés (« dosovitskiy image worth words ») : quelques mots
    du titre, dans le désordre, mêlés à un nom d'auteur. Chaque mot doit être dans
    le titre de la notice ou être le nom d'un de ses auteurs ; plus les mots
    couvrent le titre, plus le score est haut (un titre exact garde 1, via
    title_similarity)."""
    qwords = [w for w in norm_title(text).split() if w not in TITLE_STOPWORDS]
    tw = [w for w in norm_title(r.title).split() if w not in TITLE_STOPWORDS]
    if not 2 <= len(qwords) <= 10 or not tw:
        return 0.0
    authors = {a.key() for a in r.authors if a.key()}
    used: list[int] = []
    slot: list[int | None] = []      # position dans le titre de chaque mot cherché
    named = 0
    for w in qwords:
        j = next((i for i, t in enumerate(tw) if i not in used and t == w), None)
        if j is None:
            j = next((i for i, t in enumerate(tw) if i not in used and _same_word(w, t)), None)
        if j is not None:
            used.append(j)
        elif family_key(w) in authors:
            named += 1
            j = -1
        slot.append(j)
    # Un mot composé (« One-Liners ») compte en entier : le « one » de « Rank-One »
    # ne fait pas trouver un titre où « liners » manque.
    for comp in re.findall(r"\w+(?:-\w+)+", text.lower()):
        parts = set(norm_title(comp).split()) - TITLE_STOPWORDS
        idx = [i for i, w in enumerate(qwords) if w in parts]
        if any(slot[i] is None for i in idx):
            for i in idx:
                if slot[i] is not None and slot[i] >= 0:
                    used.remove(slot[i])
                slot[i] = None
    missing = sum(1 for j in slot if j is None)
    allowed = 1 if len(used) + missing > 3 else 0   # au-delà de 3 mots, un mot étranger (sigle, revue) toléré
    if len(used) < 2 or missing > allowed:
        return 0.0
    s = 0.78 + 0.14 * len(used) / len(tw) + (0.03 if used == sorted(used) else 0) + (0.04 if named else 0)
    return min(0.97, s - 0.06 * missing)


def record_match(r: Record, q: Query) -> float:
    if not r.title:
        return 0.0
    if q.kind == "reference":
        nt = norm_title(r.title)
        s = title_in_text(r.title, q.text)
        if len(nt.split()) < 4:
            s *= 0.75
        if q.title:
            s = max(s, title_similarity(q.title, r.title))
    else:
        qt = q.title or q.raw
        s = title_similarity(qt, r.title)
        if q.kind == "title" and is_short_name(qt):
            # « Chronos » tout court est un livre ou une entrée de dictionnaire bien plus
            # souvent qu'un papier : il passe derrière les « Chronos: ... ».
            s = min(s, 0.9)
        nq, nr = norm_title(qt), norm_title(r.title)
        if nq and nr and len(nq) < len(nr) * 0.8 and len(nq.split()) >= 2:
            # titre partiel tapé par l'utilisateur
            s = max(s, fuzz.partial_ratio(nq, nr) / 100 * 0.92 * min(1.0, 0.55 + len(nq) / max(len(nr), 1)))
        s = max(s, main_title_match(qt, r.title))
    if q.kind in ("title", "reference") and (q.title or q.raw):
        s = max(s, keyword_score(q.title or q.raw, r))
    if is_supplement(r) and not _SUPPLEMENT_TITLE.match(norm_title(q.title or q.raw or "")):
        s -= 0.2   # l'article passe devant ses données ; un DOI cherché tel quel les retrouve quand même
    names = {family_key(a) for a in q.authors if family_key(a)}
    if names and r.authors:
        rk = {a.key() for a in r.authors}
        hits = len(names & rk)
        s += 0.04 * min(2, hits)
        if not hits and len(names) >= 2:
            s -= 0.06
    if q.context and r.authors:
        ctx = family_key(q.context[:4000])
        keys = [a.key() for a in r.authors[:8] if len(a.key()) > 2]
        found = sum(1 for k in keys if k in ctx)
        if keys:
            if keys[0] in ctx:
                s += 0.06
            if found == 0 and len(keys) >= 2:
                s -= 0.12
    if q.year and r.year:
        dy = abs(q.year - r.year)
        s += 0.03 if dy <= 1 else (-0.03 if dy <= 3 else -0.08)
    return max(0.0, min(1.0, s))


def cluster_score(recs: list[Record], q: Query) -> float:
    ids = set()
    for r in recs:
        ids |= _id_keys(r)
    qkeys = set()
    if q.doi:
        qkeys.add("doi:" + q.doi)
    if q.arxiv:
        qkeys.add("arxiv:" + q.arxiv)
    for k, v in q.ids.items():
        qkeys.add(f"{k}:{str(v).lower()}")
    base = max((r.score for r in recs), default=0.0)
    if qkeys & ids:
        # Un DOI lu dans un PDF peut être celui d'une référence citée : on ne
        # le croit que si le titre concorde un minimum.
        if q.kind != "pdf" or not q.title or base >= 0.5:
            base = max(base, 0.99)
    # Départage des homonymes par la qualité des preuves, pas par les citations
    # des agrégateurs (OpenAlex/S2 en attribuent parfois des milliers à une
    # mauvaise notice).
    groups = evidence_groups(recs)
    curated = groups & {"official", "dblp", "arxiv", "europepmc", "inspire", "hal"}
    # Échelle volontairement petite : un titre exact doit toujours battre un
    # titre partiel, même très attesté.
    bonus = min(0.045, 0.015 * max(0, len(groups) - 1)) + (0.015 if curated else 0.0)
    if all(r.aggregate for r in recs):
        bonus -= 0.05
    cites = max((r.citations or 0 for r in recs), default=0)
    bonus += min(0.005, math.log10(1 + cites) * 0.001)
    if q.kind == "title" and base >= 0.9 and is_short_name(q.title or q.raw):
        # Nom court porté par plusieurs papiers (« BERT: ... », « Chronos: ... ») : on
        # parle le plus souvent du plus cité. Les homonymes restent proposés à côté.
        bonus += min(0.06, math.log10(1 + cites) * 0.012)
    return base + bonus   # non plafonné pour trier ; l'affichage plafonne à 1


def evidence_groups(recs: list[Record]) -> set[str]:
    """Sources indépendantes : Crossref, doi.org, OpenAlex et DataCite décrivent
    le même dépôt de DOI et ne comptent que pour une."""
    out = set()
    for r in recs:
        if r.source in ("crossref", "doi", "openalex", "datacite"):
            out.add("registry")
        elif r.source in ("proceedings", "acl", "web") or (r.source == "openreview" and r.status == "published"):
            out.add("official")
        elif r.source == "openreview":
            out.add("openreview-sub")
        else:
            out.add(r.source)
    return out



CS_FAMILIES = {f for f, _s, _p in venues.VENUES} - {"nature", "science", "pnas"}


def _looks_cs(recs: list[Record]) -> bool:
    for r in recs:
        if r.arxiv_class.lower().startswith(("cs.", "stat.ml", "eess.", "math.oc")):
            return True
        if r.source in ("openreview", "acl", "proceedings", "dblp"):
            return True
        if venues.family(r.venue or r.venue_short) in CS_FAMILIES:
            return True
    return False


def _doi_prefix(doi: str) -> str:
    return doi.split("/", 1)[0] if doi else ""


def _priority_dois(dois: set[str], recs: list[Record], limit: int = 4) -> list[str]:
    """DOI à compléter en priorité : les plus attestés, hors préfixes répétés
    (un même dépôt qui enregistre des copies du papier sous des dizaines de DOI)."""
    prefix_count = Counter(_doi_prefix(d) for d in dois)
    support = Counter()
    for r in recs:
        if r.doi:
            support[r.doi] += 1 + r.weight / 100
        for rel in r.related:
            if rel[0] == "doi":
                support[rel[1]] += 0.8
    out = [d for d in dois if not d.startswith("10.48550/") and not is_repository_doi(d)
           and prefix_count[_doi_prefix(d)] < 3]
    return sorted(out, key=lambda d: (-support[d], d))[:limit]


def _flag_suspicious(versions: list[dict], recs: list[Record]):
    """Marque les versions qui ressemblent à des copies (préfixe DOI répété
    dans l'œuvre, publication isolée très postérieure à la première version)."""
    years = [r.year for r in recs if r.year and not r.aggregate]
    first_year = min(years) if years else None
    prefix_count = Counter(_doi_prefix(d) for d in {r.doi for r in recs if r.doi})
    strong_sources = {"dblp", "proceedings", "acl", "openreview", "s2", "arxiv", "europepmc", "inspire", "hal"}
    best_support = max((v["support"] for v in versions), default=0)
    for v in versions:
        srcs = {x["source"] for x in v["sources"]}
        reasons = []
        if v["doi"] and prefix_count[_doi_prefix(v["doi"])] >= 3 and not srcs & strong_sources:
            reasons.append("same repository as many other records with this title")
        if (first_year and v["year"] and v["year"] - first_year >= 5 and not srcs & strong_sources
                and v["support"] < best_support and len(versions) > 1):
            reasons.append(f"published {v['year'] - first_year} years after the first version, with no other source")
        v["suspicious"] = bool(reasons)
        v["suspicious_reason"] = "; ".join(reasons)


# =================================================================== recherche

def _dup_key(r: Record) -> tuple:
    return (r.source, r.title, r.year, r.doi, r.arxiv, r.venue, r.venue_short, r.status, r.kind, r.pages,
            r.volume, r.number, len(r.authors), bool(r.bibtex), tuple(sorted(r.ids.items())))


class Search:
    def __init__(self, http, q: Query, refresh: bool = False, settings: dict | None = None,
                 keys: dict | None = None, scholar_direct: bool = False):
        self.q = q
        self.settings = bibtex.merged_settings(settings)
        self.events: asyncio.Queue = asyncio.Queue()
        self.ctx = Ctx(http, refresh=refresh, emit=self.events.put_nowait, keys=keys, scholar_direct=scholar_direct)
        self.records: list[Record] = []
        self.status: dict[str, dict] = {}
        self.done: set[str] = set()
        self.t0 = time.monotonic()
        self._last_partial = 0.0
        self._last_count = -1
        self._background: set[asyncio.Future] = set()   # recherches par identifiant encore en cours
        self.extra_bibtex: dict[str, list[dict]] = defaultdict(list)  # clé de version -> BibTeX sup.

    # ------------------------------------------------------------ événements

    def _emit(self, ev: dict):
        self.events.put_nowait(ev)

    def _set_status(self, source: str, state: str, count: int = 0, msg: str = "", ms: int = 0):
        """state : pending | running | ok | error | blocked | timeout. Une source peut
        être appelée plusieurs fois (recherche puis compléments) : l'état affiché
        est dérivé de l'ensemble de ses appels."""
        st = self.status.setdefault(source, {"source": source, "label": LABELS.get(source, source),
                                             "state": "pending", "count": 0, "message": "", "ms": 0,
                                             "_active": 0, "_ok": False, "_err": ""})
        if state == "running":
            st["_active"] += 1
        elif state == "ok":
            st["_active"] = max(0, st["_active"] - 1)
            st["_ok"] = True
            st["count"] += count
        elif state in ("error", "blocked", "timeout"):
            st["_active"] = max(0, st["_active"] - 1)
            st["_err"] = state
            st["message"] = msg
        if ms:
            st["ms"] = max(st["ms"], ms)
        if st["_active"]:
            st["state"] = "running"
        elif st["count"]:
            st["state"] = "ok"
        elif st["_ok"]:
            st["state"] = "empty"
        elif st["_err"]:
            st["state"] = st["_err"]
        else:
            st["state"] = "pending"
        self._emit({"type": "source", **{k: v for k, v in st.items() if not k.startswith("_")}})

    async def _call(self, source: str, coro, key: str | None = None) -> list[Record]:
        if key:
            if key in self.done:
                coro.close()
                return []
            self.done.add(key)
        self._set_status(source, "running")
        t = time.monotonic()
        try:
            res = await asyncio.wait_for(coro, TIMEOUTS.get(source, DEFAULT_TIMEOUT))
        except SourceError as e:
            self._set_status(source, "blocked" if e.kind in ("blocked", "unavailable", "ratelimited") else "error",
                             msg=e.message, ms=int((time.monotonic() - t) * 1000))
            return []
        except asyncio.TimeoutError:
            self._set_status(source, "timeout", msg="timed out", ms=int((time.monotonic() - t) * 1000))
            return []
        except asyncio.CancelledError:
            self._set_status(source, "timeout", msg="abandoned (too slow)", ms=int((time.monotonic() - t) * 1000))
            raise
        except Exception as e:  # bug d'une source : on ne casse pas la recherche
            logger.exception("Source %s en échec", source)
            self._set_status(source, "error", msg=f"internal error ({type(e).__name__})")
            return []
        recs = [r for r in (res if isinstance(res, list) else [res]) if r is not None and r.title]
        # Une source réinterrogée (titre complet, compléments) renvoie souvent la même
        # notice : la compter deux fois gonflerait les preuves et doublerait l'affichage.
        seen = {_dup_key(r) for r in self.records}
        recs = [r for r in recs if _dup_key(r) not in seen]
        for r in recs:
            r.score = record_match(r, self.q)
        self.records.extend(recs)
        self._set_status(source, "ok", count=len(recs), ms=int((time.monotonic() - t) * 1000))
        return recs

    # ------------------------------------------------------------ phases

    async def _phase_ids(self):
        q, ctx, tasks = self.q, self.ctx, []
        if q.doi:
            tasks += [
                self._call("crossref", crossref.lookup(ctx, q.doi), "crossref:" + q.doi),
                self._call("doi", doi_src.lookup(ctx, q.doi), "doi:" + q.doi),
                self._call("openalex", openalex.lookup(ctx, q.doi), "openalex:" + q.doi),
                self._call("dblp", dblp.lookup(ctx, doi=q.doi), "dblp:doi:" + q.doi),
                self._call("s2", s2.lookup(ctx, [f"DOI:{q.doi}"]), "s2:" + q.doi),
                self._call("europepmc", europepmc.lookup(ctx, doi=q.doi), "epmc:" + q.doi),
            ]
            if is_preprint_doi(q.doi) or is_repository_doi(q.doi):
                tasks.append(self._call("datacite", datacite.lookup(ctx, q.doi), "datacite:" + q.doi))
        if q.arxiv:
            tasks += [
                self._call("arxiv", arxiv.lookup(ctx, [q.arxiv]), "arxiv:" + q.arxiv),
                self._call("s2", s2.lookup(ctx, [f"ARXIV:{q.arxiv}"]), "s2:arxiv:" + q.arxiv),
                self._call("dblp", dblp.lookup(ctx, arxiv=q.arxiv), "dblp:arxiv:" + q.arxiv),
                self._call("openalex", openalex.lookup(ctx, arxiv_doi(q.arxiv)), "openalex:" + q.arxiv),
            ]
        ids = q.ids
        if ids.get("openreview"):
            tasks.append(self._call("openreview", openreview.lookup(ctx, ids["openreview"]), "or:" + ids["openreview"]))
            tasks.append(self._call("dblp", dblp.lookup(ctx, url=f"https://openreview.net/forum?id={ids['openreview']}"),
                                    "dblp:url:or:" + ids["openreview"]))
        if ids.get("acl"):
            tasks.append(self._call("acl", pages.acl_by_id(ctx, ids["acl"]), "page:acl:" + ids["acl"]))
            tasks.append(self._call("s2", s2.lookup(ctx, [f"ACL:{ids['acl']}"]), "s2:acl:" + ids["acl"]))
        if ids.get("pmid"):
            tasks.append(self._call("europepmc", europepmc.lookup(ctx, pmid=ids["pmid"]), "epmc:" + ids["pmid"]))
            tasks.append(self._call("s2", s2.lookup(ctx, [f"PMID:{ids['pmid']}"]), "s2:pmid:" + ids["pmid"]))
        if ids.get("pmcid"):
            tasks.append(self._call("s2", s2.lookup(ctx, [f"PMCID:{ids['pmcid']}"]), "s2:pmc:" + ids["pmcid"]))
        if ids.get("hal"):
            tasks.append(self._call("hal", hal.lookup(ctx, hal=ids["hal"]), "hal:" + ids["hal"]))
        if ids.get("s2"):
            tasks.append(self._call("s2", s2.lookup(ctx, [ids["s2"]]), "s2:" + ids["s2"]))
        if ids.get("dblp"):
            tasks.append(self._call("dblp", dblp.lookup(ctx, dblp=ids["dblp"]), "dblp:" + ids["dblp"]))
        if ids.get("openalex"):
            tasks.append(self._call("openalex", self._openalex_id(ids["openalex"]), "openalex:" + ids["openalex"]))
        if q.url and pages.is_official(q.url):
            tasks.append(self._call("proceedings", pages.official(ctx, q.url), "page:" + q.url))
            tasks.append(self._call("dblp", dblp.lookup(ctx, url=q.url), "dblp:url:" + q.url))
        if not tasks:
            return
        # Crossref, doi.org ou arXiv donnent vite une notice fiable : on l'affiche
        # et on lance la recherche par titre sans attendre les sources lentes
        # (DBLP), qui finissent en arrière-plan.
        pending = {asyncio.ensure_future(t) for t in tasks}
        reliable_at = None
        while pending:
            _done, pending = await asyncio.wait(pending, timeout=0.25, return_when=asyncio.FIRST_COMPLETED)
            if len(self.records) != self._last_count:
                self._emit_results(final=False)
            if reliable_at is None and any(r.weight >= 70 and not r.aggregate for r in self.records):
                reliable_at = time.monotonic()
            if pending and reliable_at is not None and time.monotonic() - reliable_at >= IDS_GRACE:
                break
        self._background |= pending

    async def _join_background(self):
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)
            self._background.clear()

    async def _openalex_id(self, wid: str):
        data = await openalex.get(self.ctx, f"{openalex.API}/{wid}", openalex._params({"select": openalex.SELECT}),
                                  ttl=86400 * 3, refresh=self.ctx.refresh)
        return openalex._parse(data)

    async def _phase_search(self):
        q = self.q
        if not q.title and (q.has_ids() or q.url):
            # Le titre vient de la notice trouvée par identifiant ou sur la page : il sert à
            # chercher les autres versions.
            best = max((r for r in self.records if r.weight >= 60), key=lambda r: r.weight, default=None)
            if not best:
                best = max(self.records, key=lambda r: r.weight, default=None)
            if not best:
                return
            q.title = best.title
            if not q.authors:
                q.authors = [a.family for a in best.authors[:4]]
            for r in self.records:
                r.score = record_match(r, q)
        tasks = []
        for name, fn in SEARCHERS.items():
            if name == "arxiv" and q.arxiv and "arxiv:" + q.arxiv in self.done:
                continue  # déjà lu par identifiant ; l'API arXiv impose 3 s entre deux appels
            if name == "scholar" and not (self.ctx.serpapi_keys or self.ctx.scholar_direct):
                continue  # ni clé SerpAPI ni accès direct permis : Google Scholar n'est pas interrogé
            tasks.append(asyncio.create_task(self._call(name, fn(self.ctx, q), f"search:{name}")))
        pending = set(tasks)
        while pending:
            done, pending = await asyncio.wait(pending, timeout=1.0, return_when=asyncio.FIRST_COMPLETED)
            finished = sum(1 for t in tasks if t.done())
            if finished >= 3 and len(self.records) != self._last_count and time.monotonic() - self._last_partial > 1.2:
                self._emit_results(final=False)

    async def _spell_fix(self) -> bool:
        """Résultats faibles sur un titre : on cherche une orthographe plus
        courante de chaque mot dans des titres proches (corpus DataCite + notices
        déjà reçues), et on relance si la requête change."""
        from rapidfuzz.distance import Levenshtein
        q = self.q
        words = norm_title(q.title).split()
        if len(words) < 2:
            return False
        try:
            titles = await asyncio.wait_for(datacite.fuzzy_titles(self.ctx, words), 10)
        except Exception:
            return False
        vocab = Counter()
        for t in titles + [r.title for r in self.records]:
            vocab.update(set(norm_title(t).split()))
        fixed = []
        for w in words:
            best = w
            if len(w) >= 3:
                limit = 1 if len(w) <= 5 else 2
                # mot de 3 lettres (« ned ») : seulement vers un mot bien attesté (« need »)
                floor = 3 if len(w) == 3 else 1
                cands = [(c, n) for c, n in vocab.items() if c != w and abs(len(c) - len(w)) <= limit
                         and Levenshtein.distance(c, w) <= limit and n > vocab.get(w, 0) and n >= floor]
                if cands:
                    best = max(cands, key=lambda x: (x[1], -Levenshtein.distance(x[0], w)))[0]
            fixed.append(best)
        if fixed == words:
            return False
        corrected = " ".join(fixed)
        self._emit({"type": "notice", "message": f'Nothing convincing for the exact input: searched again with "{corrected}".'})
        q.title = corrected
        for r in self.records:
            r.score = record_match(r, q)
        for name in SEARCHERS:
            self.done.discard(f"search:{name}")
        self._emit({"type": "query", "query": q.public()})
        await self._phase_search()
        return True

    async def _phase_full_title(self):
        """Titre partiel, nom court (« mTSBench ») ou mots-clés : une fois le papier
        reconnu, les sources sont réinterrogées avec son titre complet. Leur propre
        moteur n'avait pas forcément compris la saisie, et c'est souvent l'une d'elles
        qui connaît la version publiée (OpenReview pour TMLR, Crossref, DBLP)."""
        q = self.q
        if q.kind != "title" or not q.title or q.has_ids():
            return
        top = self._top_clusters(1)
        if not top or cluster_score(top[0], q) < 0.85:
            return
        full = max(top[0], key=lambda r: _weight(r, "title")).title
        if title_similarity(full, q.title) >= 0.9:
            return   # le titre tapé était déjà complet
        best = max(top[0], key=lambda r: _weight(r, "authors"))
        q2 = dataclasses.replace(q, raw=full, title=full, authors=q.authors or [a.family for a in best.authors[:3]])
        # arXiv a déjà répondu (une requête toutes les 3 s), Scholar coûte du quota.
        tasks = [asyncio.create_task(self._call(name, fn(self.ctx, q2), f"search-full:{name}:{full[:80]}"))
                 for name, fn in SEARCHERS.items() if name not in ("arxiv", "scholar")]
        # Chaque réponse s'affiche dès qu'elle arrive : la version publiée (OpenReview
        # en 2 s) n'attend pas la source la plus lente.
        pending, deadline = set(tasks), time.monotonic() + FULL_TITLE_BUDGET
        while pending and (left := deadline - time.monotonic()) > 0:
            _done, pending = await asyncio.wait(pending, timeout=min(1.0, left), return_when=asyncio.FIRST_COMPLETED)
            if len(self.records) != self._last_count:
                self._emit_results(final=False)
        for t in pending:
            t.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    def _top_clusters(self, n: int = TOP_CLUSTERS) -> list[list[Record]]:
        clusters = cluster(self.records)
        scored = sorted(((cluster_score(c, self.q), c) for c in clusters), key=lambda x: -x[0])
        if not scored:
            return []
        top = scored[0][0]
        return [c for s, c in scored[:n] if s >= max(0.5, top - 0.3)]

    async def _phase_expand(self, round_no: int):
        ctx, tasks = self.ctx, []
        top = self._top_clusters()
        for ci, recs in enumerate(top):
            dois, arxivs, urls, acl_ids, or_ids, s2_ids = set(), set(), set(), set(), set(), []
            raw_doi = {}
            for r in recs:
                if r.doi:
                    dois.add(r.doi)
                    if r.ids.get("doi_raw"):
                        raw_doi[r.doi] = r.ids["doi_raw"]
                if r.arxiv:
                    arxivs.add(r.arxiv)
                for rel in r.related:
                    if rel[0] == "doi" and rel[1]:
                        dois.add(rel[1])
                    elif rel[0] == "arxiv" and rel[1]:
                        arxivs.add(rel[1])
                for u in (r.url, r.link, r.pdf_url, *r.ids.get("ee_all", "").split("\n")):
                    if u and pages.is_official(u):
                        urls.add(u)
                if r.ids.get("acl"):
                    acl_ids.add(r.ids["acl"])
                if r.ids.get("openreview") and r.source != "openreview":
                    or_ids.add(r.ids["openreview"])
            for d in dois:
                m = re.match(r"^10\.18653/v1/(.+)$", d)
                if m:
                    acl_ids.add(m.group(1).upper() if re.match(r"^[a-z]\d{2}-\d{4}$", m.group(1)) else m.group(1))
            sources_in = {r.source for r in recs}
            for d in _priority_dois(dois, recs):
                tasks.append(self._call("crossref", crossref.lookup(ctx, d), "crossref:" + d))
                if ci == 0 or not is_preprint_doi(d):
                    tasks.append(self._call("doi", doi_src.lookup(ctx, d), "doi:" + d))
                if is_preprint_doi(d) and not d.startswith("10.1101/"):
                    tasks.append(self._call("datacite", datacite.lookup(ctx, d), "datacite:" + d))
            # Notices déjà renvoyées par la recherche arXiv : inutile de les relire
            # (arXiv impose 3 s entre deux appels).
            have_arx = {r.arxiv for r in self.records if r.source == "arxiv"}
            new_arx = [a for a in sorted(arxivs) if "arxiv:" + a not in self.done and a not in have_arx]
            if new_arx:
                for a in new_arx:
                    self.done.add("arxiv:" + a)
                tasks.append(self._call("arxiv", arxiv.lookup(ctx, new_arx)))
            if "dblp" not in sources_in or round_no == 1:
                best_title = max(recs, key=lambda r: _weight(r, "title")).title
                titles = list(dict.fromkeys(r.title for r in recs if r.weight >= 50))[:6]
                main_doi = next((d for d in _priority_dois(dois, recs) if not is_preprint_doi(d)), "")
                tasks.append(self._call(
                    "dblp", dblp.lookup(ctx, doi=main_doi, arxiv=next(iter(sorted(arxivs)), ""),
                                        title=best_title, titles=titles,
                                        contains=ci == 0 and "dblp" not in sources_in and _looks_cs(recs),
                                        raw_doi=raw_doi.get(main_doi, "")),
                    f"dblp:exp:{main_doi}:{sorted(arxivs)}:{best_title[:60]}"))
            for u in sorted(urls):
                tasks.append(self._call("proceedings", pages.official(ctx, u), "page:" + _page_key(u)))
            for a in sorted(acl_ids):
                tasks.append(self._call("acl", pages.acl_by_id(ctx, a), "page:acl:" + a.lower()))
            if not urls and "proceedings" not in sources_in:
                jm = next((r for r in recs if venues.family(r.venue or r.venue_short) == "jmlr"
                           and str(r.volume).isdigit()), None)
                if jm:
                    tasks.append(self._call("proceedings", pages.jmlr_by_title(ctx, jm.volume, jm.title),
                                            f"page:jmlr:{jm.volume}:{jm.title[:50]}"))
            for o in sorted(or_ids):
                tasks.append(self._call("openreview", openreview.lookup(ctx, o), "or:" + o))
            if "s2" not in sources_in:
                main_doi = next((d for d in sorted(dois) if not is_preprint_doi(d)), "")
                if main_doi:
                    s2_ids.append(f"DOI:{main_doi}")
                s2_ids += [f"ARXIV:{a}" for a in sorted(arxivs)]
                if s2_ids:
                    tasks.append(self._call("s2", s2.lookup(ctx, s2_ids), "s2:" + s2_ids[0]))
            if ci == 0:
                for r in recs:
                    if r.source == "inspire" and r.ids.get("inspire"):
                        tasks.append(self._extra_bib("inspire", inspire.fetch_bibtex(ctx, r.ids["inspire"]),
                                                     r, "INSPIRE-HEP", "inspire-bib:" + r.ids["inspire"]))
                    if r.source == "scholar" and r.ids.get("scholar") and r.score >= 0.9 and ctx.scholar_direct:
                        tasks.append(self._extra_bib("scholar", scholar.try_bibtex(ctx, r.ids["scholar"]),
                                                     r, "Google Scholar", "scholar-bib:" + r.ids["scholar"]))
                for a in sorted(arxivs)[:2]:
                    rr = next((r for r in recs if r.arxiv == a and r.source == "arxiv"), None)
                    if rr:
                        tasks.append(self._extra_bib("arxiv", arxiv.official_bibtex(ctx, a), rr,
                                                     "arXiv (official export)", "arxiv-bib:" + a))
        if tasks:
            # Budget : ce qui n'a pas répondu à temps est abandonné (les notices déjà
            # reçues suffisent) plutôt que de faire attendre l'utilisateur.
            futs = [asyncio.ensure_future(t) for t in tasks]
            done, pending = await asyncio.wait(futs, timeout=EXPAND_BUDGET)
            for f in pending:
                f.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

    async def _extra_bib(self, source: str, coro, rec: Record, label: str, key: str):
        if key in self.done:
            coro.close()
            return
        self.done.add(key)
        try:
            text = await asyncio.wait_for(coro, 12)
        except Exception:
            return
        if text and not rec.bibtex:
            rec.bibtex, rec.bibtex_label = text, label
        elif text:
            self.extra_bibtex[id(rec)].append({"bibtex": text, "label": label, "source": source})

    # ------------------------------------------------------------ résultats

    def results(self) -> list[dict]:
        clusters = cluster(self.records)
        scored = sorted(((cluster_score(c, self.q), c) for c in clusters), key=lambda x: -x[0])
        if not scored:
            return []
        top = scored[0][0]
        keep = [(s, c) for s, c in scored if s >= max(0.45, top - 0.35)][:6] or scored[:3]
        works = []
        for i, (score, recs) in enumerate(keep):
            works.append(self._work(f"w{i}", score, recs))
        if len(works) > 1:
            a, b = works[0], works[1]
            cites_a = (a["citations"] or {}).get("count") or 0
            cites_b = (b["citations"] or {}).get("count") or 0
            # Un homonyme peu attesté ne rend pas le résultat douteux.
            dominant = a["evidence"] >= b["evidence"] + 2 or (
                a["evidence"] > b["evidence"] and cites_a >= 50 and cites_a >= 20 * max(cites_b, 1))
            works[0]["confidence"] = "high" if a["score"] >= 0.9 and (b["score"] < 0.8 or dominant) else \
                ("medium" if a["score"] >= 0.75 else "low")
        elif works:
            works[0]["confidence"] = "high" if works[0]["score"] >= 0.88 else ("medium" if works[0]["score"] >= 0.7 else "low")
        return works

    def _work(self, wid: str, score: float, recs: list[Record]) -> dict:
        versions = build_versions(recs)
        out_versions = []
        for vi, v in enumerate(versions):
            spec = merge_version(v, recs)
            rendered = bibtex.render(spec, self.settings)
            vrecs = v["records"]
            sources = _source_entries(vrecs)
            for r in vrecs:
                for extra in self.extra_bibtex.get(id(r), []):
                    sources.append({"source": extra["source"], "label": LABELS.get(extra["source"], extra["source"]),
                                    "link": r.link, "bibtex": extra["bibtex"], "bibtex_label": extra["label"],
                                    "status_note": "", "title": r.title, "year": r.year, "venue": ""})
            notices = []
            for r in vrecs:
                for n in r.notices:
                    if n not in notices:
                        notices.append(n)
            arx = [r for r in vrecs if r.source == "arxiv"]
            info = {}
            if arx:
                a = max(arx, key=lambda r: r.arxiv_version or 0)
                info = {"arxiv_version": a.arxiv_version, "arxiv_updated": a.ids.get("arxiv_updated", ""),
                        "arxiv_first": a.date, "comment": a.ids.get("comment", ""),
                        "journal_ref": a.ids.get("journal_ref", "")}
            primary_weight = sum(max((r.weight for r in vrecs if r.source == s and not r.aggregate), default=0)
                                 for s in {r.source for r in vrecs})
            # Libellé précis des sources génériques pour la marge (« PMLR », « NeurIPS Proceedings »...).
            prov_labels = {}
            for r in sorted(vrecs, key=lambda r: -r.weight):
                if r.source in ("proceedings", "web", "doi") and r.bibtex_label and r.source not in prov_labels:
                    prov_labels[r.source] = r.bibtex_label
            out_versions.append({
                "id": f"{wid}v{vi}", "status": v["status"], "kind": spec["kind"],
                "label": _version_label(spec, vrecs), "year": spec.get("year"),
                "venue": spec.get("venue", ""), "doi": spec.get("doi", ""), "arxiv": spec.get("arxiv", ""),
                "hal": spec.get("hal", ""), "url": spec.get("url", ""), "spec": spec, "bibtex": rendered["bibtex"], "key": rendered["key"],
                "prov": rendered["prov"], "prov_labels": prov_labels, "sources": sources, "notices": notices,
                "retracted": any(r.retracted for r in vrecs), "conflicts": _conflicts(vrecs, spec),
                "info": info, "support": primary_weight,
                "n_sources": len({r.source for r in vrecs}),
            })
        _flag_suspicious(out_versions, recs)
        rec_id = _recommend(out_versions, self.q)
        out_versions.sort(key=lambda v: (v["id"] != rec_id, v["suspicious"], {"published": 0, "preprint": 1, "submitted": 2}.get(v["status"], 3),
                                         -(v["year"] or 0)))
        main = next(v for v in out_versions if v["id"] == rec_id)
        cites = [(r.citations, r.source) for r in recs if r.citations]
        best_cite = max(cites, default=None)
        hints = _hints(out_versions, recs)
        links = {}
        for r in recs:
            if r.source in ("s2", "dblp", "openalex", "scholar", "openreview", "europepmc", "hal", "inspire") \
                    and r.link and r.source not in links:
                links[r.source] = r.link
        links.setdefault("scholar", scholar.search_url(main["spec"].get("title", "")))
        return {
            "id": wid, "score": round(min(score, 1.0), 3), "confidence": "", "recommended": rec_id,
            "title": main["spec"].get("title", ""),
            "authors": [Author.from_dict(a).display() for a in main["spec"].get("authors", [])],
            "year": main.get("year"), "versions": out_versions,
            "citations": {"count": best_cite[0], "source": LABELS.get(best_cite[1], best_cite[1])} if best_cite else None,
            "hints": hints, "links": {LABELS.get(k, k): v for k, v in links.items()},
            "retracted": any(v["retracted"] for v in out_versions),
            "n_sources": len({r.source for r in recs}),
            "evidence": len(evidence_groups(recs)),
        }

    def _emit_results(self, final: bool):
        self._last_partial = time.monotonic()
        self._last_count = len(self.records)
        try:
            works = self.results()
        except Exception:
            logger.exception("Calcul des résultats impossible")
            return
        self._emit({"type": "results", "final": final, "works": works,
                    "elapsed": int((time.monotonic() - self.t0) * 1000)})

    async def _phase_url(self):
        """URL quelconque : métadonnées de la page, ou PDF téléchargé puis analysé."""
        q = self.q
        self._set_status("web", "running")
        try:
            rec, ctype, data = await asyncio.wait_for(pages.generic(self.ctx, q.url), 40)
            if ctype == "pdf":
                info = await asyncio.to_thread(pdfinfo.extract_isolated, data)
                apply_pdf_info(q, info)
                self._set_status("web", "ok", count=1)
                self._emit({"type": "query", "query": q.public()})
                return
        except SourceError as e:
            self._set_status("web", "blocked" if e.kind == "blocked" else "error", msg=e.message)
            return
        except (asyncio.TimeoutError, ValueError) as e:
            self._set_status("web", "error", msg=str(e) or "timed out")
            return
        if not rec:
            self._set_status("web", "ok", count=0)
            return
        rec.score = 1.0
        self.records.append(rec)
        self._set_status("web", "ok", count=1)
        q.title = q.title or rec.title
        q.doi = q.doi or rec.doi
        q.arxiv = q.arxiv or rec.arxiv
        q.authors = q.authors or [a.family for a in rec.authors[:4]]
        q.year = q.year or rec.year
        self._emit({"type": "query", "query": q.public()})

    async def run(self):
        try:
            for name in list(SEARCHERS):
                self._set_status(name, "pending")
            self._emit({"type": "query", "query": self.q.public()})
            official = bool(self.q.url) and pages.is_official(self.q.url)
            if self.q.kind == "url" and self.q.url and not official:
                await self._phase_url()
            if self.q.has_ids() or official:
                await self._phase_ids()
            if official and not self.records:
                await self._phase_url()   # page officielle illisible : extraction générique
            await self._phase_search()
            await self._join_background()
            if self.q.kind == "title" and self.q.title and not self.q.has_ids():
                clusters = cluster(self.records)
                best = max((cluster_score(c, self.q) for c in clusters), default=0.0)
                if best < 0.75:
                    self._emit_results(final=False)
                    await self._spell_fix()
            self._emit_results(final=False)
            await self._phase_full_title()
            self._emit({"type": "phase", "phase": "expand"})
            await self._phase_expand(1)
            self._emit_results(final=False)
            await self._phase_expand(2)
            self._emit_results(final=True)
        except Exception as e:
            logger.exception("Recherche en échec")
            self._emit({"type": "error", "message": f"Internal error: {type(e).__name__}"})
        finally:
            for f in self._background:
                f.cancel()
            for st in self.status.values():
                if st["state"] in ("pending", "running"):
                    st["state"] = "skipped" if st["state"] == "pending" else "timeout"
                    self._emit({"type": "source", **st})
            self._emit({"type": "done", "elapsed": int((time.monotonic() - self.t0) * 1000)})
            self.events.put_nowait(None)

    async def stream(self):
        task = asyncio.create_task(self.run())
        try:
            while True:
                ev = await self.events.get()
                if ev is None:
                    break
                yield ev
        finally:
            if not task.done():
                task.cancel()


def apply_pdf_info(q: Query, info: dict) -> Query:
    q.kind = "pdf" if q.kind in ("title", "url", "pdf") else q.kind
    q.title = info.get("title") or q.title
    q.doi = q.doi or info.get("doi", "")
    q.arxiv = q.arxiv or info.get("arxiv", "")
    if info.get("hal") and not q.ids.get("hal"):
        q.ids["hal"] = info["hal"]
    q.context = info.get("context", "")
    q.venue_hint = info.get("venue_hint", "")
    q.original = {"pdf": {k: info.get(k) for k in ("title", "meta_title", "doi", "arxiv", "hal", "venue_hint", "pages")}}
    return q


def query_from_pdf(info: dict, filename: str = "") -> Query:
    q = Query(raw=filename or "PDF", kind="pdf")
    return apply_pdf_info(q, info)


def _page_key(u: str) -> str:
    return re.sub(r"(-Abstract[\w-]*\.html|-Paper[\w-]*\.pdf|\.pdf|\.html|/)$", "", u.split("#")[0])


def _recommend(versions: list[dict], q: Query) -> str:
    """Version à citer : la version publiée la mieux attestée (puis la plus
    récente) ; à défaut la prépublication. Si le PDF annonce une conférence,
    on privilégie celle-ci."""
    def score(v):
        s = {"published": 10000, "preprint": 2000, "submitted": 1000}.get(v["status"], 0)
        s += min(v["support"], 400)
        if v["status"] == "published" and v["year"]:
            s += max(0, v["year"] - 1990) * 3
        if v["retracted"]:
            s -= 100
        if q.venue_hint and v["status"] == "published":
            hint = venues.family(q.venue_hint)
            if hint and hint == venues.family(v["venue"] or v["label"]):
                s += 600
        return s
    pool = [v for v in versions if not v.get("suspicious")] or versions
    return max(pool, key=score)["id"]


def _hints(versions: list[dict], recs: list[Record]) -> list[str]:
    out = []
    has_pub = any(v["status"] == "published" for v in versions)
    for r in recs:
        if r.source == "arxiv" and not has_pub:
            jr = r.ids.get("journal_ref")
            if jr:
                out.append(f'arXiv reports a publication (journal-ref): "{jr}". No published record was found, '
                           f"check the reference.")
            c = r.ids.get("comment", "")
            if c and arxiv.ACCEPT_RE.search(c):
                out.append(f'Authors\' comment on arXiv: "{c[:200]}"')
    sub = [v for v in versions if v["status"] == "submitted"]
    if sub and not has_pub:
        out.append("An OpenReview submission exists but has not been accepted (yet).")
    return list(dict.fromkeys(out))
