"""DBLP via son point SPARQL officiel (sparql.dblp.org).

Le site et l'API de recherche de dblp.org sont derrière un anti-bot (Anubis)
depuis 2026 : on n'y touche pas. Le SPARQL donne tout ce qu'il faut pour
reconstruire la notice BibTeX au format DBLP (titre, auteurs dans l'ordre,
proceedings complets, pages, DOI...)."""
from __future__ import annotations

import re

from .. import venues
from ..textutil import clean_title, norm_arxiv, parse_name, title_match, title_similarity

from .base import Ctx, Query, Record, WEIGHTS
from .http import SourceError

NAME = "dblp"
ENDPOINT = "https://sparql.dblp.org/sparql"
# Revues sans volumes, que DBLP numérote par année (« volume 2026 » de TMLR).
_YEAR_AS_VOLUME = {"tmlr"}
PREFIX = "PREFIX dblp: <https://dblp.org/rdf/schema#>\n"


async def _sparql(ctx: Ctx, query: str, timeout: float = 15.0, ttl: float = 86400 * 3) -> list[dict]:
    data = await ctx.http.get_json(
        NAME, ENDPOINT, params={"query": PREFIX + query},
        headers={"Accept": "application/sparql-results+json"}, timeout=timeout, ttl=ttl,
        refresh=ctx.refresh,
    )
    out = []
    for b in data.get("results", {}).get("bindings", []):
        out.append({k: v.get("value", "") for k, v in b.items()})
    return out


def _phrase(title: str) -> str:
    """Plus longue suite de mots sans ponctuation du titre (en minuscules),
    pour un CONTAINS exact côté DBLP."""
    t = title.lower()
    t = re.sub(r"\$[^$]*\$", " | ", t)
    segments = [s.strip() for s in re.split(r"[^\w\s'’]+", t) if s.strip()]
    if not segments:
        return ""
    best = max(segments, key=lambda s: len(s.split()))
    words = best.split()
    if len(words) < 2 and len(best) < 8:
        return ""
    return " ".join(words[:8]).replace('"', "")


def _esc(s: str) -> str:
    return s.replace("\\", "\\\\").replace('"', '\\"')


_SMALL = {"a", "an", "the", "and", "or", "of", "for", "in", "on", "to", "with", "by", "at", "via", "from",
          "is", "are", "as", "vs", "into", "over", "under"}


def _title_variants(titles: list[str]) -> list[str]:
    """Variantes de casse/ponctuation d'un titre pour une recherche exacte
    (instantanée) : DBLP termine ses titres par un point et garde la casse
    de l'éditeur."""
    out: list[str] = []
    for t in titles:
        t = re.sub(r"\s+", " ", (t or "").replace("’", "'")).strip()
        if len(t) < 6 or "$" in t:
            continue
        base = t.rstrip(".")
        words = base.split(" ")
        # Ordre fixe (pas de set) : la requête, donc sa clé de cache et la troncature
        # à 40 variantes, ne doivent pas changer d'un démarrage du serveur à l'autre.
        forms = dict.fromkeys((
            base,
            " ".join(w[:1].upper() + w[1:] for w in words),
            " ".join(w if i and w.lower() in _SMALL else w[:1].upper() + w[1:] for i, w in enumerate(words)),
            " ".join(w if any(c.isupper() for c in w[1:]) else (w.lower() if i else w[:1].upper() + w[1:].lower())
                     for i, w in enumerate(words)),
        ))
        for f in forms:
            end = "" if f.endswith(("?", "!")) else "."
            for v in (f + end, f):
                if v not in out:
                    out.append(v)
    return out[:40]


async def _find_exact(ctx: Ctx, titles: list[str]) -> list[str]:
    variants = _title_variants(titles)
    if not variants:
        return []
    values = " ".join('"' + _esc(v) + '"' for v in variants)
    rows = await _sparql(ctx, f"SELECT DISTINCT ?p WHERE {{ VALUES ?t {{ {values} }} ?p dblp:title ?t . }}",
                         timeout=10.0)
    return [r["p"] for r in rows][:10]


async def _find_by_title(ctx: Ctx, title: str, contains: bool = True, extra: list[str] | None = None) -> list[str]:
    found = await _find_exact(ctx, [title] + list(extra or []))
    if found or not contains:
        return found
    phrase = _phrase(title)
    if not phrase:
        return []
    rows = await _sparql(
        ctx,
        f'SELECT ?p ?t WHERE {{ ?p dblp:title ?t . FILTER(CONTAINS(LCASE(STR(?t)), "{_esc(phrase)}")) }} LIMIT 40',
        timeout=14.0,
    )
    scored = [(title_match(r["t"], title), r["p"]) for r in rows]
    return [p for s, p in sorted(scored, reverse=True) if s >= 0.82][:8]


def _doi_variants(doi: str, raw: str = "") -> list[str]:
    out = [doi, doi.upper(), doi.lower(), raw]
    if doi.startswith("10.48550/arxiv."):
        out += ["10.48550/ARXIV." + doi.split("arxiv.", 1)[1], "10.48550/arXiv." + doi.split("arxiv.", 1)[1]]
    return [f"<https://doi.org/{d}>" for d in dict.fromkeys(out) if d and re.fullmatch(r"[\x21-\x7e]+", d) and ">" not in d]


async def _find_by_ids(ctx: Ctx, doi: str = "", arxiv: str = "", raw_doi: str = "") -> list[str]:
    values = []
    if doi:
        values += _doi_variants(doi, raw_doi)
    if arxiv:
        values += _doi_variants(f"10.48550/arxiv.{arxiv}".lower())
    if not values:
        return []
    rows = await _sparql(ctx, "SELECT DISTINCT ?p WHERE { VALUES ?d { %s } ?p dblp:doi ?d }" % " ".join(values))
    found = [r["p"] for r in rows]
    if arxiv and re.fullmatch(r"\d{4}\.\d{4,5}", arxiv):
        found.append(f"https://dblp.org/rec/journals/corr/abs-{arxiv[:4]}-{arxiv[5:]}")
    return found


S = "https://dblp.org/rdf/schema#"


def _local(pred: str) -> str:
    return pred.rsplit("#", 1)[-1].rsplit("/", 1)[-1]


_TYPE_KIND = {"Inproceedings": "conference", "Article": "journal", "Incollection": "chapter",
              "Book": "book", "Phdthesis": "thesis", "Mastersthesis": "thesis", "Proceedings": "book"}


def _dblp_bibtex(key: str, rec: Record, btype: str, raw_venue: str, booktitle: str, series: str,
                 svol: str, ee: str) -> str:
    """Notice au format exact de dblp.org (sans timestamp/biburl/bibsource)."""
    from ..bibtex import write_entry
    from ..textutil import text_to_latex
    names = " and ".join(text_to_latex(a.display()) for a in rec.authors)
    fields = [("author", names), ("title", text_to_latex(rec.title))]
    etype = btype.lower() if btype else "misc"
    if etype == "inproceedings":
        fields.append(("booktitle", text_to_latex(booktitle or raw_venue)))
        if series:
            fields += [("series", text_to_latex(series)), ("volume", svol)]
        if rec.publisher:
            fields.append(("publisher", text_to_latex(rec.publisher)))
    elif etype == "article":
        fields.append(("journal", text_to_latex(raw_venue)))
        fields += [("volume", rec.volume), ("number", rec.number)]
    elif raw_venue:
        fields.append(("booktitle" if etype == "incollection" else "howpublished", text_to_latex(raw_venue)))
    fields.append(("pages", rec.pages))
    fields.append(("year", str(rec.year or "")))
    if rec.arxiv and raw_venue == "CoRR":
        fields += [("eprinttype", "arXiv"), ("eprint", rec.arxiv)]
    fields.append(("url", ee))
    fields.append(("doi", rec.doi if not rec.doi.startswith("10.48550/") else ""))
    return write_entry(etype, "DBLP:" + key, fields)


async def _siblings(ctx: Ctx, found: list[Record], first_pids: dict[str, str]) -> list[str]:
    """Autres versions du même papier chez DBLP (CoRR <-> conférence/revue) :
    publications du premier auteur dont le titre concorde. Rapide (index
    par auteur) et insensible aux variations de casse du titre."""
    pids = sorted({first_pids[r.link] for r in found if first_pids.get(r.link)})[:3]
    if not pids:
        return []
    rows = await _sparql(ctx, "SELECT ?p ?t WHERE { VALUES ?a { %s } ?p dblp:authoredBy ?a . ?p dblp:title ?t . }"
                         % " ".join(f"<{x}>" for x in pids))
    known = {r.link for r in found}
    out = []
    for row in rows:
        if row["p"] in known:
            continue
        if any(title_similarity(r.title, row["t"]) >= 0.9 for r in found):
            out.append(row["p"])
    return list(dict.fromkeys(out))[:6]


async def _details(ctx: Ctx, recs: list[str], siblings: bool = True) -> list[Record]:
    """Requêtes ancrées sur le sujet (rapides) : triplets directs, signatures
    d'auteurs, puis volume de proceedings. Une seule grosse requête avec
    OPTIONAL/GROUP BY prenait ~30 s au planificateur de QLever."""
    import asyncio
    recs = list(dict.fromkeys(r for r in recs if r.startswith("https://dblp.org/rec/")))[:10]
    if not recs:
        return []
    values = " ".join(f"<{r}>" for r in recs)
    triples, sigs = await asyncio.gather(
        _sparql(ctx, "SELECT ?p ?pred ?o WHERE { VALUES ?p { %s } ?p ?pred ?o . }" % values),
        _sparql(ctx, "SELECT ?p ?o ?n ?c WHERE { VALUES ?p { %s } ?p dblp:hasSignature ?s . "
                     "?s dblp:signatureOrdinal ?o . ?s dblp:signatureDblpName ?n . "
                     "OPTIONAL { ?s dblp:signatureCreator ?c } }" % values),
    )
    data: dict[str, dict[str, list[str]]] = {}
    for t in triples:
        data.setdefault(t["p"], {}).setdefault(_local(t["pred"]), []).append(t["o"])
    authors: dict[str, list[tuple[int, str]]] = {}
    first_pids: dict[str, str] = {}
    for sg in sigs:
        try:
            o = int(sg["o"])
        except ValueError:
            continue
        authors.setdefault(sg["p"], []).append((o, sg["n"]))
        if o == 1 and sg.get("c"):
            first_pids[sg["p"]] = sg["c"]
    books = sorted({d.get("publishedAsPartOf", [""])[0] for d in data.values() if d.get("publishedAsPartOf")})
    book_data: dict[str, dict[str, list[str]]] = {}
    if books:
        try:
            rows = await _sparql(ctx, "SELECT ?b ?pred ?o WHERE { VALUES ?b { %s } ?b ?pred ?o . "
                                      "FILTER(?pred IN (dblp:title, dblp:publishedBy, dblp:publishedInSeries, "
                                      "dblp:publishedInSeriesVolume, dblp:isbn)) }" % " ".join(f"<{b}>" for b in books))
            for t in rows:
                book_data.setdefault(t["b"], {}).setdefault(_local(t["pred"]), []).append(t["o"])
        except SourceError:
            pass
    out = []
    for i, p in enumerate(recs):
        d = data.get(p)
        if not d or not d.get("title"):
            continue
        g = lambda k: (d.get(k) or [""])[0]
        key = p.split("/rec/", 1)[1]
        btype = g("bibtexType").rsplit("#", 1)[-1]
        raw_venue = g("publishedIn") or g("publishedInJournal")
        dois = [x.split("doi.org/", 1)[-1] for x in d.get("doi", []) if "doi.org/" in x]
        doi_raw = next((x for x in dois if not x.lower().startswith("10.48550/")), "")
        arxiv = ""
        if raw_venue == "CoRR":
            arxiv = norm_arxiv(g("publishedInJournalVolume").replace("abs/", ""))
            if not arxiv:
                arxiv = next((x.lower().split("arxiv.", 1)[-1] for x in dois if "arxiv." in x.lower()), "")
        bk = book_data.get(g("publishedAsPartOf"), {})
        bg = lambda k: (bk.get(k) or [""])[0]
        booktitle = clean_title(bg("title")) if btype in ("Inproceedings", "Incollection") else ""
        kind = "preprint" if raw_venue == "CoRR" else _TYPE_KIND.get(btype, "other")
        ee = g("primaryDocumentPage")
        year = g("yearOfPublication")
        volume = "" if raw_venue == "CoRR" else g("publishedInJournalVolume")
        if volume == year[:4] and venues.family(raw_venue) in _YEAR_AS_VOLUME:
            volume = ""   # revue sans volumes : DBLP y range les articles par année
        rec = Record(
            source=NAME, title=clean_title(g("title")),
            authors=[parse_name(n) for _, n in sorted(authors.get(p, []))],
            year=int(year[:4]) if year[:4].isdigit() else None, kind=kind,
            venue=booktitle or ("arXiv" if raw_venue == "CoRR" else raw_venue),
            venue_short=raw_venue if raw_venue != "CoRR" else "arXiv",
            volume=volume,
            number=g("publishedInJournalVolumeIssue"), pages=g("pagination"),
            publisher=bg("publishedBy"), series=bg("publishedInSeries"),
            isbn=bg("isbn").replace("urn:isbn:", ""), doi=doi_raw.lower(), arxiv=arxiv,
            ids={"dblp": key, **({"doi_raw": doi_raw} if doi_raw else {})}, url=ee,
            status="preprint" if raw_venue == "CoRR" else "published", link=p,
            weight=WEIGHTS[NAME], rank=i,
        )
        if bg("publishedInSeries") and bg("publishedInSeriesVolume"):
            rec.ids["series_volume"] = bg("publishedInSeriesVolume")
        # Les ee multiples (page officielle + DOI) servent à retrouver le BibTeX officiel.
        others = [x for x in d.get("documentPage", []) if x != ee]
        if others:
            rec.pdf_url = others[0]
            rec.ids["ee_all"] = "\n".join(others[:6])
        rec.bibtex = _dblp_bibtex(key, rec, btype, raw_venue, booktitle, bg("publishedInSeries"),
                                  bg("publishedInSeriesVolume"), ee)
        rec.bibtex_label = "DBLP"
        out.append(rec)
    if siblings and out:
        try:
            more = await _siblings(ctx, out, first_pids)
            if more:
                out += await _details(ctx, more, siblings=False)
        except SourceError:
            pass
    return out


async def search(ctx: Ctx, q: Query) -> list[Record]:
    """Recherche rapide (titre exact, variantes de casse). La recherche par
    sous-chaîne, lente, est réservée aux compléments sur les meilleures pistes."""
    if q.kind == "reference" and not q.title:
        return []
    title = q.title or q.raw
    if len(title) < 6:
        return []
    return await _details(ctx, await _find_by_title(ctx, title, contains=False))


def _url_variants(url: str) -> list[str]:
    m = re.search(r"openreview\.net/(?:forum|pdf)\?id=([\w-]+)", url)
    if m:
        oid = m.group(1)
        return [f"https://openreview.net/forum?id={oid}", f"https://openreview.net/pdf?id={oid}"]
    base = url.split("#")[0]
    out = dict.fromkeys((base, base.rstrip("/"), base.replace("http://", "https://"), base.replace("https://", "http://")))
    return [u for u in out if re.fullmatch(r"[\x21-\x7e]+", u) and ">" not in u]


async def find_by_url(ctx: Ctx, url: str) -> list[str]:
    """Notices DBLP dont une page (ee) est cette URL. Utile quand le site
    lui-même est fermé aux robots (OpenReview)."""
    variants = _url_variants(url)
    if not variants:
        return []
    values = " ".join(f"<{u}>" for u in variants)
    rows = await _sparql(ctx, "SELECT DISTINCT ?p WHERE { VALUES ?u { %s } "
                              "{ ?p dblp:documentPage ?u } UNION { ?p dblp:primaryDocumentPage ?u } }" % values)
    return [r["p"] for r in rows]


async def lookup(ctx: Ctx, doi: str = "", arxiv: str = "", title: str = "", raw_doi: str = "",
                 dblp: str = "", titles: list[str] | None = None, contains: bool = True,
                 url: str = "") -> list[Record]:
    recs: list[str] = []
    if dblp:
        recs.append(f"https://dblp.org/rec/{dblp}")
    if url:
        recs += await find_by_url(ctx, url)
    if doi or arxiv:
        recs += await _find_by_ids(ctx, doi, arxiv, raw_doi)
    if title:
        try:
            recs += await _find_by_title(ctx, title, contains=contains, extra=titles)
        except SourceError:
            if not recs:
                raise
    return await _details(ctx, recs)
