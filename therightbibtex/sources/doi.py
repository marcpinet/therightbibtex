"""doi.org en négociation de contenu : le BibTeX tel que le produit l'agence
d'enregistrement du DOI (Crossref, DataCite, mEDRA, JaLC...)."""
from __future__ import annotations

from .. import bibtex
from ..textutil import clean_title, is_preprint_doi, norm_doi

from .base import Ctx, Record, WEIGHTS
from .http import SourceError

NAME = "doi"

_KIND = {"article": "journal", "inproceedings": "conference", "incollection": "chapter",
         "inbook": "chapter", "book": "book", "phdthesis": "thesis", "techreport": "report"}


async def lookup(ctx: Ctx, doi: str) -> Record | None:
    try:
        r = await ctx.http.request(
            NAME, f"https://doi.org/{doi}", headers={"Accept": "application/x-bibtex; charset=utf-8"},
            ttl=86400 * 7, refresh=ctx.refresh, neg_ttl=86400, timeout=12,
        )
    except SourceError as e:
        if e.status == 404:
            return None
        raise
    text = r.text.strip()
    if not text.startswith("@"):
        return None
    e = bibtex.parse_one(text)
    if not e:
        return None
    agency = "DataCite" if "datacite" in r.url or doi.startswith(("10.48550/", "10.5281/")) else "Crossref"
    kind = _KIND.get(e.type, "other")
    if is_preprint_doi(doi):
        kind = "preprint"
    year = e.get("year")
    rec = Record(
        source=NAME, title=clean_title(e.text("title")), authors=e.authors(),
        year=int(year) if year.isdigit() else None, month=bibtex.month_number(e.get("month")),
        kind=kind, venue=e.text("journal") or e.text("booktitle") or e.text("publisher"),
        volume=e.get("volume"), number=e.get("number"), pages=e.get("pages").replace("–", "--"),
        publisher=e.text("publisher"), doi=norm_doi(e.get("doi") or doi),
        url=e.get("url"), status="preprint" if kind == "preprint" else "published",
        weight=WEIGHTS[NAME], link=f"https://doi.org/{doi}",
    )
    # Les clés produites par Crossref ressemblent à « He_2016 » ; DataCite met l'URL du DOI.
    rec.bibtex = bibtex.reformat(text.replace("–", "--"))
    rec.bibtex_label = f"doi.org ({agency})"
    return rec
