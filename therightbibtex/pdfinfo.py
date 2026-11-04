"""Extraction des indices bibliographiques d'un PDF : DOI, identifiant arXiv
(tampon vertical des PDF arXiv), titre (plus gros corps de la 1re page),
métadonnées XMP/Info, et mention de publication (« Published as a
conference paper at ICLR 2024 »...).

Le serveur passe par extract_isolated() : l'analyse tourne dans un processus
à part (ce fichier lancé comme script), avec un délai et une mémoire plafonnés,
pour qu'un PDF piégé ou démesuré ne puisse ni planter ni bloquer le serveur."""
from __future__ import annotations

import json
import logging
import re
import subprocess
import sys

from .textutil import DOI_RE, HAL_ID_RE, clean_markup, norm_arxiv, norm_doi

logger = logging.getLogger(__name__)

WORKER_TIMEOUT = 45                      # secondes
WORKER_MEMORY = 1536 * 1024 * 1024       # espace d'adressage max du processus d'analyse (Linux)

ARXIV_STAMP_RE = re.compile(r"arXiv:\s*(\d{4}\.\d{4,5}|[a-z\-]+(?:\.[A-Z]{2})?/\d{7})(v\d+)?\s*\[[^\]]+\]", re.I)
VENUE_HINTS = [
    (re.compile(r"Published as a conference paper at (ICLR \d{4})", re.I), None),
    (re.compile(r"Under review as a conference paper at (ICLR \d{4})", re.I), "submitted to "),
    (re.compile(r"\((NeurIPS \d{4})\)", re.I), None),
    (re.compile(r"Conference on Neural Information Processing Systems \((NeurIPS|NIPS) (\d{4})\)", re.I), None),
    (re.compile(r"Proceedings of the \d+(?:st|nd|rd|th) International Conference on Machine Learning[^\n]{0,40}?(\d{4})", re.I), "ICML "),
    (re.compile(r"Published in Transactions on Machine Learning Research \(([^)]+)\)", re.I), "TMLR "),
    (re.compile(r"Proceedings of the (\d+(?:st|nd|rd|th) [^\n]{5,90}?)(?:\n|,\s*pages)", re.I), ""),
    (re.compile(r"\b(Preprint\.? Under review\.?)", re.I), ""),
]
_BAD_TITLES = re.compile(r"^(untitled|microsoft word|document\d*|paper|main|manuscript|article|arxiv|"
                         r"\S+\.(docx?|tex|pdf|dvi))", re.I)


def _venue_hint(text: str) -> str:
    for rx, prefix in VENUE_HINTS:
        m = rx.search(text)
        if m:
            val = " ".join(g for g in m.groups() if g)
            return ((prefix or "") + val).strip()
    return ""


def _doi_candidates(text: str) -> list[str]:
    out = []
    for m in DOI_RE.finditer(text):
        d = norm_doi(m.group(1))
        if d and not re.search(r"x{3,}|n{4,}|\.\.\.", d) and d not in out:
            out.append(d)
    return out


def _title_from_layout(page) -> str:
    """Lignes de plus gros corps dans le haut de la 1re page (hors tampon arXiv)."""
    d = page.get_text("dict")
    height = page.rect.height or 792
    lines = []
    for block in d.get("blocks", []):
        for line in block.get("lines", []):
            if abs(line.get("dir", (1, 0))[1]) > 0.1:
                continue  # texte vertical (tampon arXiv dans la marge)
            spans = [s for s in line.get("spans", []) if s.get("text", "").strip()]
            if not spans:
                continue
            text = "".join(s["text"] for s in spans).strip()
            size = max(s.get("size", 0) for s in spans)
            y = line["bbox"][1]
            if y > height * 0.7 or len(text) < 2:
                continue
            lines.append((size, y, text))
    if not lines:
        return ""
    # Le titre = lignes de corps maximal (à 0,6 pt près), contiguës verticalement.
    candidates = [l for l in lines if not re.match(r"^(arxiv|preprint|published|under review|proceedings|journal|vol\.|doi)", l[2], re.I)]
    if not candidates:
        return ""
    max_size = max(l[0] for l in candidates)
    big = sorted([l for l in candidates if l[0] >= max_size - 0.6], key=lambda l: l[1])
    title_lines, last_y = [], None
    for size, y, text in big:
        if last_y is not None and y - last_y > size * 2.2:
            break
        title_lines.append(text)
        last_y = y
    title = " ".join(title_lines)
    title = re.sub(r"\s+", " ", title).strip()
    return re.sub(r"(\w)- (\w)", r"\1\2", title) if re.search(r"\w- \w", title) else title


def extract(data: bytes) -> dict:
    import pymupdf as fitz

    info = {"title": "", "doi": "", "arxiv": "", "hal": "", "context": "", "venue_hint": "", "meta_title": "",
            "pages": 0}
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception as e:
        raise ValueError(f"Unreadable PDF ({e.__class__.__name__})")
    try:
        info["pages"] = doc.page_count
        meta = doc.metadata or {}
        mt = clean_markup(meta.get("title") or "")
        if mt and len(mt.split()) >= 3 and not _BAD_TITLES.match(mt):
            info["meta_title"] = mt
        xmp = ""
        try:
            xmp = doc.get_xml_metadata() or ""
        except Exception:
            pass
        first = doc[0] if doc.page_count else None
        text1 = first.get_text("text") if first else ""
        text2 = doc[1].get_text("text") if doc.page_count > 1 else ""
        if first is not None and len(text1.strip()) < 50:
            try:  # PDF scanné : OCR de la 1re page si Tesseract est installé
                tp = first.get_textpage_ocr(language="eng", dpi=200, full=True)
                text1 = first.get_text("text", textpage=tp)
            except Exception as e:
                logger.info("OCR indisponible : %s", e)
        info["context"] = (text1 + "\n" + text2[:1500])[:6000]
        m = ARXIV_STAMP_RE.search(text1) or ARXIV_STAMP_RE.search(xmp)
        if m:
            info["arxiv"] = norm_arxiv(m.group(1))
        else:
            m = re.search(r"arxiv\.org/abs/(\d{4}\.\d{4,5})", text1 + xmp, re.I)
            if m:
                info["arxiv"] = m.group(1)
        # DOI : métadonnées XMP d'abord (posées par l'éditeur), puis en-tête/pied de la 1re page.
        xmp_dois = [d for d in _doi_candidates(re.sub(r"<[^>]+>", " ", xmp)) if not d.startswith("10.48550/")]
        page_dois = [d for d in _doi_candidates(text1) if not d.startswith("10.48550/")]
        meta_dois = _doi_candidates(" ".join(str(v) for v in meta.values() if v))
        for d in xmp_dois + meta_dois + page_dois:
            info["doi"] = d
            break
        # Page de garde ajoutée par HAL : « HAL Id: hal-03692072 ».
        m = re.search(r"HAL Id\s*:\s*([\w-]+)", text1)
        m = HAL_ID_RE.fullmatch(m.group(1).lower()) if m else None
        if m:
            info["hal"] = m.group(1)
        if first is not None:
            info["title"] = _title_from_layout(first)
        if (not info["title"] or len(info["title"]) < 12) and info["meta_title"]:
            info["title"] = info["meta_title"]
        info["venue_hint"] = _venue_hint(text1)
    finally:
        doc.close()
    return info


def extract_isolated(data: bytes, timeout: float = WORKER_TIMEOUT) -> dict:
    """extract() dans un processus à part. Lève ValueError (message affichable)."""
    try:
        p = subprocess.run([sys.executable, "-m", __spec__.name if __spec__ else "therightbibtex.pdfinfo"],
                           input=data, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ValueError("PDF took too long to analyze")
    lines = [l for l in p.stdout.decode("utf-8", "replace").splitlines() if l.startswith("{")]
    if p.returncode != 0 or not lines:
        logger.warning("Analyse du PDF en échec (code %s) : %s", p.returncode,
                       p.stderr.decode("utf-8", "replace")[-500:])
        raise ValueError("Unreadable PDF")
    out = json.loads(lines[-1])
    if "error" in out:
        raise ValueError(out["error"])
    return out


def _worker():
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (WORKER_MEMORY, WORKER_MEMORY))
    except (ImportError, ValueError, OSError):
        pass  # Windows : pas de plafond mémoire, le délai reste
    try:
        out = extract(sys.stdin.buffer.read())
    except ValueError as e:
        out = {"error": str(e)}
    except MemoryError:
        out = {"error": "PDF too heavy to analyze"}
    sys.stdout.write(json.dumps(out) + "\n")


if __name__ == "__main__":
    _worker()
