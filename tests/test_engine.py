import support  # noqa: F401

import asyncio
import time
import unittest

from therightbibtex import bibtex
from therightbibtex import engine
from therightbibtex import query
import replay
from therightbibtex.sources.base import Record


def rec(source, title, authors=("Ashish Vaswani", "Noam Shazeer"), year=2017, **kw):
    from therightbibtex.textutil import parse_name
    return Record(source=source, title=title, authors=[parse_name(a) for a in authors], year=year, **kw)


class Clustering(unittest.TestCase):
    def test_same_work(self):
        a = rec("crossref", "Attention is all you need", status="published", kind="conference", doi="10.5555/1")
        b = rec("arxiv", "Attention Is All You Need.", status="preprint", kind="preprint", arxiv="1706.03762")
        c = rec("crossref", "Erratum to: Attention is all you need", status="published", kind="journal")
        d = rec("crossref", "Attention is all you need", authors=("John Smith", "Jane Doe"), year=2024)
        groups = engine.cluster([a, b, c, d])
        self.assertIn(sorted([a.source, b.source]), [sorted(r.source for r in g) for g in groups])
        self.assertEqual(len(groups), 3)   # l'erratum et l'homonyme restent à part

    def test_foreign_doi_dropped(self):
        """OpenAlex colle à l'arXiv de « GPT-4 Technical Report » le DOI d'un article de
        COSIT 2024 : doi.org fait foi, la notice OpenAlex perd ce DOI et rien n'est fusionné."""
        pre = rec("openalex", "GPT-4 Technical Report", authors=("OpenAI",), year=2023, kind="preprint",
                  arxiv="2303.08774", doi="10.4230/lipics.cosit.2024.11")
        arx = rec("arxiv", "GPT-4 Technical Report", authors=("OpenAI",), year=2023, kind="preprint", arxiv="2303.08774")
        other = rec("doi", "Semantic Perspectives on the Lake District Writing", authors=("Erum Haris",), year=2024,
                    kind="conference", doi="10.4230/lipics.cosit.2024.11")
        groups = engine.cluster([pre, arx, other])
        self.assertEqual(sorted(len(g) for g in groups), [1, 2])
        self.assertEqual(pre.doi, "")

    def test_shared_id_needs_matching_titles(self):
        """Même identifiant, titres qui ne partagent que « time series anomaly detection » : pas fusionnés."""
        a = rec("s2", "Anomaly Transformer: Time Series Anomaly Detection with Association Discrepancy",
                authors=("Jiehui Xu",), year=2021, ids={"dblp": "x/1"})
        b = rec("dblp", "Time Series Anomaly Detection Leveraging MSE Feedback with AutoEncoder and RNN",
                authors=("Ibrahim Delibasoglu",), year=2024, ids={"dblp": "x/1"})
        self.assertEqual(len(engine.cluster([a, b])), 2)
        same = rec("dblp", "Generative Adversarial Networks", authors=("Ian Goodfellow",), year=2014, ids={"dblp": "g"})
        nets = rec("s2", "Generative adversarial nets", authors=("Ian Goodfellow",), year=2014, ids={"dblp": "g"})
        self.assertEqual(len(engine.cluster([same, nets])), 1)

    def test_reference_entries_are_not_papers(self):
        from therightbibtex.sources import crossref
        it = {"DOI": "10.1093/benz/9780199773787.article.b00018256", "type": "reference-entry", "title": ["Bert, J."],
              "container-title": ["Benezit Dictionary of Artists"], "issued": {"date-parts": [[2011]]}}
        r = crossref.parse_item(it)
        self.assertEqual(r.kind, "reference")
        paper = rec("arxiv", "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
                    authors=("Jacob Devlin",), year=2018, kind="preprint")
        q = query.parse_input("BERT")
        self.assertGreater(engine.record_match(paper, q), engine.record_match(r, q) + 0.2)

    def test_short_name(self):
        """« Chronos » tout court (un livre) passe derrière « Chronos: Learning the Language of Time Series »."""
        q = query.parse_input("Chronos")
        book = rec("crossref", "Chronos", authors=("Someone Else",), year=2020, kind="book")
        paper = rec("openalex", "Chronos: Learning the Language of Time Series", authors=("Abdul Fatir Ansari",),
                    year=2024, kind="preprint")
        self.assertGreater(engine.record_match(paper, q), engine.record_match(book, q))

    def test_supplement_stays_apart(self):
        """Le jeu de données déposé à côté du papier n'est ni fusionné avec lui, ni classé devant."""
        title = "When Foundation Models are One-Liners: Limitations and Future Directions for Time Series Anomaly Detection"
        paper = rec("openreview", title, authors=("Xiaokun Zhu",), year=2026, kind="conference", status="published")
        data = rec("datacite", f'Replication Data for: "{title}"', authors=("Xiaokun Zhu",), year=2026, kind="dataset")
        self.assertEqual(len(engine.cluster([paper, data])), 2)
        q = query.parse_input("When Foundation Models are One-Liners")
        self.assertGreaterEqual(engine.record_match(paper, q), 0.95)    # titre principal, sans sous-titre
        self.assertLess(engine.record_match(data, q), 0.75)

    def test_versions(self):
        pub = rec("crossref", "Attention is all you need", status="published", kind="conference",
                  venue="Advances in Neural Information Processing Systems", doi="10.5555/1")
        pre = rec("arxiv", "Attention is all you need", status="preprint", kind="preprint", arxiv="1706.03762")
        versions = engine.build_versions([pub, pre])
        self.assertEqual(sorted(v["status"] for v in versions), ["preprint", "published"])
        spec = engine.merge_version(next(v for v in versions if v["status"] == "published"))
        self.assertEqual((spec["type"], spec["year"], spec["doi"]), ("inproceedings", 2017, "10.5555/1"))

    def test_crossref_without_publication_date(self):
        """HOT SAX (ICDM'05) : Crossref n'a que la date de dépôt du DOI (5 janvier 2006), qu'OpenAlex
        recopie ; l'année vient du nom des actes, sinon le dépôt ne pèse presque rien face à DBLP."""
        from therightbibtex.sources import crossref
        title = "HOT SAX: Efficiently Finding the Most Unusual Time Series Subsequence"
        names = ("Eamonn J. Keogh", "Jessica Lin", "Ada Wai-Chee Fu")
        it = {"DOI": "10.1109/ICDM.2005.79", "type": "proceedings-article", "title": [title],
              "author": [{"given": "E.", "family": "Keogh"}, {"given": "J.", "family": "Lin"},
                         {"given": "A.", "family": "Fu"}],
              "container-title": ["Fifth IEEE International Conference on Data Mining (ICDM'05)"],
              "event": {"name": "Fifth IEEE International Conference on Data Mining (ICDM'05)"},
              "issued": {"date-parts": [[None]]}, "created": {"date-parts": [[2006, 1, 5]]}, "page": "226-233"}
        cr = crossref.parse_item(it)
        self.assertEqual((cr.year, cr.month, cr.year_weak), (2005, None, False))
        undated = crossref.parse_item(dict(it, **{"container-title": ["Data Mining"], "event": {}}))
        self.assertEqual((undated.year, undated.month, undated.year_weak), (2006, None, True))
        for c in (cr, undated):
            dblp = rec("dblp", title, authors=names, year=2005, kind="conference", status="published",
                       doi="10.1109/icdm.2005.79", weight=88)
            oa = rec("openalex", title, authors=names, year=2006, kind="conference", status="published",
                     doi="10.1109/icdm.2005.79", weight=55)
            versions = engine.build_versions([c, dblp, oa])
            self.assertEqual(len(versions), 1)
            self.assertEqual(engine.merge_version(versions[0])["year"], 2005)
        self.assertEqual(crossref._venue_year(["2017 IEEE/ACS 14th International Conference (AICCSA)"], 2017), 2017)
        self.assertEqual(crossref._venue_year(["Fifth IEEE ICDM (ICDM\u203205)"], 2006), 2005)
        self.assertIsNone(crossref._venue_year(["Workshop 2009"], 2006))          # après le dépôt : douteux
        self.assertIsNone(crossref._venue_year(["Joint 2004/2005 Meeting"], 2006))  # deux années


class KeywordQueries(unittest.TestCase):
    """« dosovitskiy image worth words » : nom d'auteur et mots du titre mélangés."""

    MINE = rec("arxiv", "An Image is Worth 16x16 Words: Transformers for Image Recognition at Scale",
               authors=("Alexey Dosovitskiy", "Lucas Beyer", "Alexander Kolesnikov"), year=2021)
    JUNK = rec("crossref", "Image recognition with sparse visual words at scale",
               authors=("Alice Smith",), year=2020)

    def score(self, text, r):
        return engine.record_match(r, query.parse_input(text))

    def test_author_and_title_words(self):
        for text in ("dosovitskiy image worth words", "dosovitskiy image worth words transformers",
                     "dosovitskiy transformers image recognition 2021"):
            self.assertGreaterEqual(self.score(text, self.MINE), 0.9, text)
            self.assertLess(self.score(text, self.JUNK), 0.7, text)

    def test_exact_title_still_wins(self):
        exact = rec("crossref", "Attention is All you Need", authors=("Ashish Vaswani",))
        longer = rec("crossref", "Attention is all you need in speech separation", authors=("Cem Subakan",))
        self.assertEqual(self.score("Attention is all you need", exact), 1.0)
        self.assertGreater(self.score("attention all you need", exact), self.score("attention all you need", longer))

    def test_unknown_word_rejects(self):
        self.assertEqual(engine.keyword_score("dosovitskiy image quantum", self.MINE), 0.0)   # « quantum » nulle part

    def test_compound_word_counts_whole(self):
        """Le « one » de « Rank-One » ne remplace pas « One-Liners »."""
        other = rec("crossref", "Adversarial Robustness in Time-Series Foundation Models via Causal Tracing and "
                    "Rank-One Model Editing", authors=("Alice Smith",), year=2026)
        self.assertEqual(engine.keyword_score("When Foundation Models are One-Liners", other), 0.0)
        self.assertGreaterEqual(self.score("dosovitskiy image-recognition transformers", self.MINE), 0.85)


class KeywordSources(unittest.TestCase):
    def test_arxiv_and_datacite_search_authors_too(self):
        from therightbibtex.sources import arxiv, datacite
        from therightbibtex.sources.base import Ctx, Query
        from therightbibtex.sources.http import Http, Resp
        self.assertEqual(arxiv._words_query("dosovitskiy transformers image recognition"),
                         "all:dosovitskiy AND all:transformers AND all:image AND all:recognition")
        seen = []

        class Fake(Http):
            async def request(self, source, url, *, params=None, **kw):
                seen.append(params["query"])
                return Resp(200, '{"data": []}', url, {})

        async def go():
            h = Fake()
            q = Query(raw="x", title="dosovitskiy transformers image recognition")
            await datacite.search(Ctx(h), q)
            await h.close()
        asyncio.run(go())
        self.assertEqual(seen[0], 'titles.title:"dosovitskiy transformers image recognition"')
        self.assertIn("(titles.title:transformers~1 OR creators.familyName:transformers", seen[1])
        self.assertIn("creators.familyName:dosovitskiy", seen[1])


async def run_fixture(name: str, http_cls=replay.ReplayHttp):
    fx = replay.load(name)
    http = http_cls(fx["responses"])
    s = engine.Search(http, query.parse_input(fx["query"]))
    works, first_results, t0 = [], None, time.monotonic()
    async for ev in s.stream():
        if ev["type"] == "results" and ev["works"]:
            works = ev["works"]
            if first_results is None:
                first_results = time.monotonic() - t0
    await http.close()
    return works, first_results


def main_version(work: dict) -> dict:
    return next(v for v in work["versions"] if v["id"] == work["recommended"])


class Replay(unittest.TestCase):
    """Recherches réelles rejouées hors ligne (tests/fixtures, voir record_fixtures.py)."""

    def check(self, name, title, label, fields):
        works, _ = asyncio.run(run_fixture(name))
        self.assertTrue(works, name)
        w = works[0]
        self.assertEqual(w["title"], title, name)
        self.assertEqual(w["confidence"], "high", name)
        v = main_version(w)
        self.assertEqual((v["label"], v["status"]), (label, "published"), name)
        got = bibtex.parse_one(v["bibtex"]).fields
        for k, val in fields.items():
            self.assertEqual(got.get(k), val, f"{name} : {k}")
        return w, v

    def test_title(self):
        w, v = self.check("attention", "Attention is All you Need", "NeurIPS 2017",
                          {"booktitle": "Advances in Neural Information Processing Systems", "year": "2017",
                           "volume": "30", "pages": "5998--6008"})
        self.assertEqual(v["key"], "vaswani2017attention")
        self.assertEqual(v["prov"]["booktitle"], "proceedings")
        self.assertIn("preprint", {x["status"] for x in w["versions"]})

    def test_doi(self):
        self.check("resnet-doi", "Deep Residual Learning for Image Recognition", "CVPR 2016",
                   {"doi": "10.1109/cvpr.2016.90", "pages": "770--778", "year": "2016"})

    def test_arxiv_id(self):
        self.check("vit-arxiv", "An Image is Worth 16x16 Words: Transformers for Image Recognition at Scale",
                   "ICLR 2021", {"booktitle": "International Conference on Learning Representations", "year": "2021"})

    def test_reference(self):
        self.check("resnet-ref", "Deep Residual Learning for Image Recognition", "CVPR 2016",
                   {"doi": "10.1109/cvpr.2016.90"})

    def test_official_url(self):
        self.check("neurips-url", "Attention is All you Need", "NeurIPS 2017",
                   {"booktitle": "Advances in Neural Information Processing Systems", "volume": "30"})

    def test_main_title_finds_openreview_acceptance(self):
        """« When Foundation Models are One-Liners », sans sous-titre, absent d'arXiv et de
        Crossref : seul OpenReview sait que le papier a été accepté à ICLR 2026."""
        w, _ = self.check("one-liners", "When Foundation Models are One-Liners: Limitations and Future Directions "
                          "for Time Series Anomaly Detection", "ICLR 2026",
                          {"booktitle": "The Fourteenth International Conference on Learning Representations",
                           "year": "2026"})
        self.assertNotIn("other", {x["status"] for x in w["versions"]})   # le jeu de données reste à part

    def test_short_name_finds_journal_version(self):
        """« mTSBench » seul : arXiv trouve le papier, la seconde passe avec le titre
        complet trouve la version TMLR (OpenReview et DBLP réunis, sans volume)."""
        w, v = self.check("mtsbench", "mTSBench: Benchmarking Multivariate Time Series Anomaly Detection and Model "
                          "Selection at Scale", "TMLR 2026",
                          {"journal": "Transactions on Machine Learning Research", "year": "2026"})
        self.assertNotIn("volume", bibtex.parse_one(v["bibtex"]).fields)
        self.assertEqual(sorted(x["status"] for x in w["versions"]), ["preprint", "published"])
        self.assertEqual(sorted(x["label"] for x in v["sources"]), ["DBLP", "OpenReview"])   # sans doublon

    def test_short_names(self):
        """Un seul mot : le papier qui porte ce nom, pas les entrées de dictionnaire
        (« Bert, J. ») ni les homonymes moins attestés (« Chronos: A timing analyzer... »)."""
        self.check("short-bert", "BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
                   "NAACL 2019", {"year": "2019"})
        self.check("chronos", "Chronos: Learning the Language of Time Series", "TMLR 2024",
                   {"journal": "Transactions on Machine Learning Research"})

    def test_foreign_doi_never_merges(self):
        """OpenAlex prête à ces prépublications le DOI d'un autre article (COSIT, TIME 2024)."""
        works, _ = asyncio.run(run_fixture("gpt4"))
        self.assertEqual(works[0]["title"], "GPT-4 Technical Report")
        self.assertEqual([v["label"] for v in works[0]["versions"]], ["arXiv 2303.08774v6"])
        self.assertNotIn("doi", bibtex.parse_one(main_version(works[0])["bibtex"]).fields)
        self.check("anomaly-transformer", "Anomaly Transformer: Time Series Anomaly Detection with Association "
                   "Discrepancy", "ICLR 2022", {"year": "2022"})

    def test_typos_and_keywords(self):
        self.check("typo-attention", "Attention is All you Need", "NeurIPS 2017", {"year": "2017"})
        w, _ = self.check("vaswani-kw", "Attention is All you Need", "NeurIPS 2017", {"year": "2017"})
        self.assertEqual([v["label"] for v in w["versions"] if not v["suspicious"]], ["NeurIPS 2017", "arXiv 1706.03762v7"])

    def test_slow_source_does_not_block(self):
        """Recherche par DOI : DBLP qui traîne ne retarde plus les premiers résultats."""
        class SlowDblp(replay.ReplayHttp):
            async def request(self, source, url, **kw):
                if source == "dblp":
                    await asyncio.sleep(2.5)
                return await super().request(source, url, **kw)

        works, first = asyncio.run(run_fixture("resnet-doi", SlowDblp))
        self.assertLess(first, 1.0)
        self.assertEqual(works[0]["title"], "Deep Residual Learning for Image Recognition")


if __name__ == "__main__":
    unittest.main()
