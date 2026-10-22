"""Conférences et revues : nom court canonique (« famille ») à partir des
innombrables façons de les écrire (NIPS, Neural Information Processing
Systems, NeurIPS 2023 poster, Adv. Neural Inf. Process. Syst...), et
abréviation à afficher dans le BibTeX (« ICML », « Findings of EMNLP »...)."""
from __future__ import annotations

import re

from .textutil import norm_title, strip_accents

# (famille, nom court affiché, motif sur le nom normalisé : minuscules, sans
# accents ni ponctuation). L'ordre compte : les motifs précis avant les larges.
VENUES = [
    # --- apprentissage automatique
    ("neurips", "NeurIPS", r"\b(neurips|nips|neural information processing systems|adv neural inf process syst)\b"),
    ("icml", "ICML", r"\b(icml|international conference on machine learning)\b(?! and| applications| for| technolog)"),
    ("iclr", "ICLR", r"\b(iclr|international conference on learning representations|learning representations)\b"),
    ("aistats", "AISTATS", r"\b(aistats|artificial intelligence and statistics)\b"),
    ("uai", "UAI", r"\b(uai|uncertainty in artificial intelligence)\b"),
    ("colt", "COLT", r"\b(colt|conference on learning theory)\b"),
    ("alt", "ALT", r"\balgorithmic learning theory\b"),
    ("ecml", "ECML PKDD", r"\b(ecml|pkdd|machine learning and knowledge discovery in databases)\b"),
    ("acml", "ACML", r"\b(acml|asian conference on machine learning)\b"),
    ("mlsys", "MLSys", r"\b(mlsys|conference on machine learning and systems)\b"),
    ("colm", "COLM", r"\b(colm|conference on language modeling)\b"),
    ("ijcnn", "IJCNN", r"\b(ijcnn|international joint conference on neural networks)\b"),
    ("icann", "ICANN", r"\b(icann|international conference on artificial neural networks)\b"),
    ("iconip", "ICONIP", r"\b(iconip|international conference on neural information processing)\b"),
    ("esann", "ESANN", r"\besann\b"),
    ("tmlr", "TMLR", r"\b(tmlr|transactions on machine learning research)\b"),
    ("jmlr", "JMLR", r"\b(jmlr|journal of machine learning research|j mach learn res)\b"),
    ("mlj", "Mach. Learn.", r"^machine learning$"),
    ("neco", "Neural Comput.", r"^neural computation$"),
    # --- vision, image, multimédia, graphisme
    ("cvpr", "CVPR", r"\b(cvpr|computer vision and pattern recognition)\b"),
    ("iccv", "ICCV", r"\b(iccv|international conference on computer vision)\b(?! theory| systems)"),
    ("eccv", "ECCV", r"\b(eccv|european conference on computer vision)\b"),
    ("wacv", "WACV", r"\b(wacv|winter conference on applications of computer vision)\b"),
    ("bmvc", "BMVC", r"\b(bmvc|british machine vision conference)\b"),
    ("accv", "ACCV", r"\b(accv|asian conference on computer vision)\b"),
    ("3dv", "3DV", r"\b(3dv|international conference on 3d vision)\b"),
    ("icpr", "ICPR", r"\b(icpr|international conference on pattern recognition)\b"),
    ("icip", "ICIP", r"\b(icip|international conference on image processing)\b"),
    ("icme", "ICME", r"\b(icme|international conference on multimedia and expo)\b"),
    ("acmmm", "ACM MM", r"\b(acm international conference on multimedia|acm multimedia)\b"),
    ("tpami", "TPAMI", r"\b(tpami|pattern analysis and machine intelligence|trans pattern anal mach intell)\b"),
    ("ijcv", "IJCV", r"\b(ijcv|international journal of computer vision|int j comput vis)\b"),
    ("tip", "TIP", r"\b(transactions on image processing|trans image process)\b"),
    ("patrec", "Pattern Recognit.", r"^pattern recognition$"),
    ("siggraph", "SIGGRAPH", r"\bsiggraph\b"),
    ("tog", "ACM TOG", r"\b(acm transactions on graphics|acm trans graph)\b"),
    # --- imagerie médicale
    ("miccai", "MICCAI", r"\b(miccai|medical image computing and computer assisted intervention)\b"),
    ("isbi", "ISBI", r"\b(isbi|international symposium on biomedical imaging)\b"),
    ("ipmi", "IPMI", r"\b(ipmi|information processing in medical imaging)\b"),
    ("midl", "MIDL", r"\b(midl|medical imaging with deep learning)\b"),
    ("tmi", "TMI", r"\b(transactions on medical imaging|trans med imaging)\b"),
    ("media", "MedIA", r"^medical image analysis$"),
    # --- intelligence artificielle
    ("aaai", "AAAI", r"\baaai\b(?! spring| fall)"),
    ("ijcai", "IJCAI", r"\b(ijcai|international joint conference on artificial intelligence)\b"),
    ("ecai", "ECAI", r"\b(ecai|european conference on artificial intelligence)\b"),
    ("aamas", "AAMAS", r"\b(aamas|autonomous agents and multi ?agent systems)\b"),
    ("kr", "KR", r"\bprinciples of knowledge representation and reasoning\b"),
    ("jair", "JAIR", r"\b(jair|journal of artificial intelligence research)\b"),
    ("aij", "Artif. Intell.", r"^artificial intelligence$"),
    # --- langue, parole, signal
    ("acl", "ACL", r"^(proceedings of the )?(\d+(st|nd|rd|th) )?annual meeting of the association for computational linguistics|\bacl\b(?! anthology)"),
    ("emnlp", "EMNLP", r"\b(emnlp|empirical methods in natural language processing)\b"),
    ("naacl", "NAACL", r"\b(naacl|north american chapter of the association for computational linguistics)\b"),
    ("eacl", "EACL", r"\b(eacl|european chapter of the association for computational linguistics)\b"),
    ("aacl", "AACL", r"\b(aacl|asia pacific chapter of the association for computational linguistics)\b"),
    ("coling", "COLING", r"\b(coling|international conference on computational linguistics)\b"),
    ("conll", "CoNLL", r"\b(conll|computational natural language learning)\b"),
    ("lrec", "LREC", r"\b(lrec|language resources and evaluation)\b"),
    ("tacl", "TACL", r"\b(tacl|transactions of the association for computational linguistics)\b"),
    ("icassp", "ICASSP", r"\b(icassp|acoustics speech and signal processing)\b"),
    ("interspeech", "Interspeech", r"\binterspeech\b"),
    ("asru", "ASRU", r"\b(asru|automatic speech recognition and understanding)\b"),
    ("tsp", "TSP", r"\b(transactions on signal processing|trans signal process)\b"),
    # --- données, recherche d'information, web
    ("kdd", "KDD", r"\b(kdd|knowledge discovery and data mining)\b"),
    ("www", "WWW", r"\b(www|the web conference|world wide web conference)\b"),
    ("sigir", "SIGIR", r"\b(sigir|research and development in information retrieval)\b"),
    ("wsdm", "WSDM", r"\b(wsdm|web search and data mining)\b"),
    ("cikm", "CIKM", r"\b(cikm|information and knowledge management)\b"),
    ("icdm", "ICDM", r"\b(icdm|ieee international conference on data mining)\b"),
    ("sdm", "SDM", r"\b(sdm|siam international conference on data mining)\b"),
    ("recsys", "RecSys", r"\b(recsys|conference on recommender systems)\b"),
    ("ecir", "ECIR", r"\b(ecir|european conference on information retrieval)\b"),
    ("icwsm", "ICWSM", r"\b(icwsm|weblogs and social media|web and social media)\b"),
    ("vldb", "VLDB", r"\b(vldb|very large data bases|pvldb)\b"),
    ("sigmod", "SIGMOD", r"\b(sigmod|management of data)\b"),
    ("icde", "ICDE", r"\b(icde|international conference on data engineering)\b"),
    ("pods", "PODS", r"\b(pods|principles of database systems)\b"),
    ("edbt", "EDBT", r"\b(edbt|extending database technology)\b"),
    ("tkde", "TKDE", r"\b(tkde|knowledge and data engineering|trans knowl data eng)\b"),
    ("tkdd", "TKDD", r"\b(tkdd|transactions on knowledge discovery from data)\b"),
    ("tois", "TOIS", r"\b(tois|acm transactions on information systems)\b"),
    # --- robotique
    ("corl", "CoRL", r"\b(corl|conference on robot learning)\b"),
    ("icra", "ICRA", r"\b(icra|international conference on robotics and automation)\b"),
    ("iros", "IROS", r"\b(iros|intelligent robots and systems)\b"),
    ("rss", "RSS", r"\brobotics science and systems\b"),
    ("ral", "RA-L", r"\b(robotics and automation letters|robotics autom lett)\b"),
    ("ijrr", "IJRR", r"\b(ijrr|international journal of robotics research)\b"),
    ("tnnls", "TNNLS", r"\b(tnnls|neural networks and learning systems|trans neural netw learn syst)\b"),
    # --- systèmes, réseaux, architecture
    ("osdi", "OSDI", r"\b(osdi|operating systems design and implementation)\b"),
    ("sosp", "SOSP", r"\b(sosp|symposium on operating systems principles)\b"),
    ("nsdi", "NSDI", r"\b(nsdi|networked systems design and implementation)\b"),
    ("eurosys", "EuroSys", r"\beurosys\b"),
    ("atc", "USENIX ATC", r"\busenix annual technical conference\b"),
    ("asplos", "ASPLOS", r"\b(asplos|architectural support for programming languages and operating systems)\b"),
    ("isca", "ISCA", r"\b(isca|international symposium on computer architecture)\b"),
    ("micro", "MICRO", r"\binternational symposium on microarchitecture\b"),
    ("hpca", "HPCA", r"\b(hpca|high performance computer architecture)\b"),
    ("sigcomm", "SIGCOMM", r"\bsigcomm\b"),
    ("mobicom", "MobiCom", r"\b(mobicom|mobile computing and networking)\b"),
    ("infocom", "INFOCOM", r"\binfocom\b"),
    ("sc", "SC", r"\bhigh performance computing networking storage and analysis\b"),
    ("dac", "DAC", r"^(proceedings of the )?(\d+(st|nd|rd|th) )?(acm ieee )?design automation conference\b"),
    # --- sécurité
    ("ccs", "CCS", r"\bcomputer and communications security\b"),
    ("sp", "IEEE S&P", r"\bieee symposium on security and privacy\b"),
    ("usenixsec", "USENIX Security", r"\busenix security symposium\b"),
    ("ndss", "NDSS", r"\b(ndss|network and distributed system security)\b"),
    ("eurocrypt", "EUROCRYPT", r"\beurocrypt\b"),
    ("crypto", "CRYPTO", r"^(advances in cryptology )?crypto\b"),
    # --- langages, génie logiciel, théorie, IHM
    ("popl", "POPL", r"\b(popl|principles of programming languages)\b"),
    ("pldi", "PLDI", r"\b(pldi|programming language design and implementation)\b"),
    ("oopsla", "OOPSLA", r"\boopsla\b"),
    ("icfp", "ICFP", r"\b(icfp|international conference on functional programming)\b"),
    ("icse", "ICSE", r"\b(icse|international conference on software engineering)\b"),
    ("fse", "FSE", r"\b(esec fse|foundations of software engineering)\b"),
    ("ase", "ASE", r"\bautomated software engineering\b"),
    ("stoc", "STOC", r"\b(stoc|symposium on theory of computing)\b"),
    ("focs", "FOCS", r"\b(focs|foundations of computer science)\b"),
    ("soda", "SODA", r"\b(soda|symposium on discrete algorithms)\b"),
    ("icalp", "ICALP", r"\b(icalp|automata languages and programming)\b"),
    ("lics", "LICS", r"\b(lics|logic in computer science)\b"),
    ("chi", "CHI", r"\bhuman factors in computing systems\b"),
    ("uist", "UIST", r"\b(uist|user interface software and technology)\b"),
    ("cscw", "CSCW", r"\b(cscw|computer supported cooperative work)\b"),
    # --- revues générales
    ("nature", "Nature", r"^nature$"),
    ("natcomm", "Nat. Commun.", r"^nature communications$"),
    ("natmi", "Nat. Mach. Intell.", r"^nature machine intelligence$"),
    ("natmeth", "Nat. Methods", r"^nature methods$"),
    ("science", "Science", r"^science$"),
    ("sciadv", "Sci. Adv.", r"^science advances$"),
    ("pnas", "PNAS", r"\b(pnas|proceedings of the national academy of sciences|proc natl acad sci)\b"),
    ("prl", "Phys. Rev. Lett.", r"^physical review letters$"),
    ("jasa", "JASA", r"^journal of the american statistical association$"),
    ("aos", "Ann. Stat.", r"^(the )?annals of statistics$"),
    ("jrssb", "J. R. Stat. Soc. B", r"^journal of the royal statistical society series b"),
    ("tit", "IEEE Trans. Inf. Theory", r"\b(transactions on information theory|trans inf theory)\b"),
]
_COMPILED = [(f, s, re.compile(p)) for f, s, p in VENUES]
_ABBR_STOP = {"of", "the", "and", "on", "in", "for", "a", "an", "de", "la", "le", "des", "du", "et"}
# Noms complets en toutes lettres tirés des motifs (« transactions on machine learning
# research »), pour reconnaître leurs abréviations.
_FULL_NAMES = [
    (f, [w for w in alt.split() if w not in _ABBR_STOP])
    for f, _s, p in VENUES
    for alt in re.sub(r"\(\?[!=][^)]*\)|\\b|[\^$()]", "", p).split("|")
    if re.fullmatch(r"[a-z0-9 ]+", alt) and len(alt.split()) >= 2
]


def family(venue: str) -> str:
    """Famille canonique, ou à défaut les premiers mots significatifs du nom."""
    if not venue:
        return ""
    n = norm_title(strip_accents(venue))
    for fam, _short, rx in _COMPILED:
        if rx.search(n):
            return fam
    if "." in venue:
        # Abréviation ISO 4 de DBLP (« Trans. Mach. Learn. Res. ») d'un nom connu.
        words = _abbr_words(venue)
        for fam, full in _FULL_NAMES:
            if len(words) == len(full) and _abbreviates(words, full):
                return fam
    n = re.sub(r"\b(proceedings|of|the|on|in|and|for|international|conference|annual|ieee|acm|\d+(st|nd|rd|th)?|\d{4})\b", " ", n)
    return " ".join(n.split()[:4])


def compatible(a: str, b: str) -> bool:
    """Même revue/conférence malgré les abréviations ISO 4 utilisées par DBLP
    (« J. Mach. Learn. Res. » = « Journal of Machine Learning Research »)."""
    if not a or not b:
        return False
    fa, fb = family(a), family(b)
    if fa and fa == fb:
        return True
    ta, tb = _abbr_words(a), _abbr_words(b)
    if not ta or not tb:
        return False
    # Dans un sens ou dans l'autre : à nombre de mots égal, rien ne dit lequel est abrégé.
    return _abbreviates(ta, tb) or _abbreviates(tb, ta)


def _abbr_words(venue: str) -> list[str]:
    return [w for w in norm_title(venue).split() if w not in _ABBR_STOP and not w.isdigit()]


def _abbreviates(short: list[str], long_: list[str]) -> bool:
    """Chaque mot de `short` commence un mot de `long_`, dans l'ordre (« trans mach
    learn res » pour « transactions machine learning research »)."""
    if len(short) < 2 or len(short) > len(long_):
        return False
    i = 0
    for w in long_:
        if i < len(short) and w.startswith(short[i]):
            i += 1
    return i == len(short)


def short_name(venue: str) -> str:
    if not venue:
        return ""
    n = norm_title(strip_accents(venue))
    for _fam, short, rx in _COMPILED:
        if rx.search(n):
            return short
    return ""


def short_form(venue: str) -> str:
    """Abréviation à écrire dans le BibTeX, qualificatifs compris : un papier
    d'atelier ou des Findings n'est pas un papier de la conférence principale."""
    short = short_name(venue)
    if not short:
        return ""
    n = norm_title(strip_accents(venue))
    if re.search(r"\bfindings\b", n):
        return f"Findings of {short}"
    if re.search(r"\bworkshops\b", n):
        return f"{short} Workshops"
    if re.search(r"\bworkshop\b", n):
        return f"{short} Workshop"
    if "datasets and benchmarks" in n:
        return f"{short} Datasets and Benchmarks"
    if re.search(r"\b(companion|adjunct)\b", n):
        return f"{short} Companion"
    if re.search(r"\b(system )?demonstrations?\b", n):
        return f"{short} Demos"
    if re.search(r"\bstudent research\b", n):
        return f"{short} SRW"
    return short


# Sigles à ne jamais prendre pour le nom d'une conférence.
_NOT_ACRONYMS = {"USA", "UK", "IEEE", "ACM", "SIAM", "IEEE/CVF", "CVF", "LNCS", "LNAI", "CCIS", "PMLR",
                 "AI", "ML", "II", "III", "IV", "VI", "VII", "VIII", "IX", "XI", "XII", "NY", "CA", "MA",
                 "PA", "WA", "TX", "UAE", "PRC", "EU", "OK"}


def acronym_in(venue: str) -> str:
    """Sigle écrit dans le nom complet des actes : « ... (CVPR) » ou, chez
    DBLP, « ..., ICLR 2015, San Diego, ... »."""
    if not venue:
        return ""
    cands = re.findall(r"\(([A-Z][A-Za-z0-9&+/\-]{1,14})(?:\s*['’]?(?:19|20)?\d\d)?\)", venue)
    cands += re.findall(r"(?:^|,\s*)([A-Z][A-Za-z0-9&+\-]{1,14})\s*'?(?:19|20)\d\d\b", venue)
    for c in reversed(cands):
        c = c.strip("-/")
        if c.upper() in _NOT_ACRONYMS or sum(ch.isupper() for ch in c) < 2:
            continue
        return c
    return ""


def is_preprint_venue(venue: str) -> bool:
    n = norm_title(venue)
    return bool(re.search(r"\b(arxiv|corr|biorxiv|medrxiv|ssrn|preprint|research square|techrxiv|chemrxiv|"
                          r"psyarxiv|socarxiv|osf preprints|preprints org|hal)\b", n)) or n in ("", "openreview")
