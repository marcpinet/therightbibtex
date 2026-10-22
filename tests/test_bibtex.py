import support  # noqa: F401

import unittest

from therightbibtex import bibtex
from therightbibtex import venues

SPEC = {
    "kind": "conference", "status": "published", "preprint": False, "type": "inproceedings",
    "title": "Learning Transferable Visual Models From Natural Language Supervision",
    "authors": [{"given": "Alec", "family": "Radford"}, {"given": "Jong Wook", "family": "Kim"}],
    "year": 2021, "venue": "Proceedings of the 38th International Conference on Machine Learning",
    "venue_short": "ICML", "series": "Proceedings of Machine Learning Research", "volume": "139",
    "pages": "8748-8763", "publisher": "PMLR", "doi": "", "url": "https://proceedings.mlr.press/v139/radford21a.html",
    "dblp": "conf/icml/RadfordKHRGASAM21", "prov": {"title": "proceedings", "venue": "proceedings"},
}


def fields(text):
    return bibtex.parse_one(text).fields


class Parse(unittest.TestCase):
    def test_tolerant(self):
        text = """@string{neur = "Neural Computation"}
        @comment{ignoré}
        @article{hochreiter1997,
          author = {Hochreiter, Sepp and Schmidhuber, J{\\"u}rgen},
          title = "Long Short-Term {Memory}",
          journal = neur,
          month = nov # "~",
          year = 1997,
        }"""
        e = bibtex.parse(text)
        self.assertEqual(len(e), 1)
        e = e[0]
        self.assertEqual(e.key, "hochreiter1997")
        self.assertEqual(e.get("journal"), "Neural Computation")
        self.assertEqual(e.get("year"), "1997")
        self.assertEqual([a.family for a in e.authors()], ["Hochreiter", "Schmidhuber"])
        self.assertEqual(e.authors()[1].display(), "Jürgen Schmidhuber")

    def test_corporate_author(self):
        e = bibtex.parse_one("@misc{k, author = {{OpenAI} and Smith, John}, title = {X}}")
        self.assertTrue(e.authors()[0].corporate)

    def test_set_key(self):
        self.assertTrue(bibtex.set_key("@article{old,\n title={x}}", "new").startswith("@article{new,"))


class Render(unittest.TestCase):
    def test_default(self):
        r = bibtex.render(SPEC)
        self.assertEqual(r["key"], "radford2021learning")
        f = fields(r["bibtex"])
        self.assertEqual(f["booktitle"], SPEC["venue"])
        self.assertEqual(f["series"], "Proceedings of Machine Learning Research")
        self.assertEqual(f["pages"], "8748--8763")
        self.assertEqual(f["url"], SPEC["url"])                 # pas de DOI : URL gardée
        self.assertEqual(r["prov"]["booktitle"], "proceedings")

    def test_key_styles(self):
        self.assertEqual(bibtex.render(SPEC, {"key_style": "AuthorYear"})["key"], "Radford2021")
        self.assertEqual(bibtex.render(SPEC, {"key_style": "AuthorYearWord"})["key"], "Radford2021Learning")
        self.assertEqual(bibtex.render(SPEC, {"key_style": "dblp"})["key"], "DBLP:conf/icml/RadfordKHRGASAM21")

    def test_short_venue(self):
        f = fields(bibtex.render(SPEC, {"venue_style": "short"})["bibtex"])
        self.assertEqual(f["booktitle"], "ICML")
        for gone in ("series", "volume", "publisher"):
            self.assertNotIn(gone, f)
        f = fields(bibtex.render(SPEC, {"venue_style": "short_year"})["bibtex"])
        self.assertEqual(f["booktitle"], "ICML 2021")

    def test_preprint(self):
        spec = {"kind": "preprint", "status": "preprint", "preprint": True, "type": "misc", "title": "Attention Is All You Need",
                "authors": [{"given": "Ashish", "family": "Vaswani"}], "year": 2017, "arxiv": "1706.03762",
                "arxiv_class": "cs.CL", "url": "https://arxiv.org/abs/1706.03762"}
        r = bibtex.render(spec)
        self.assertTrue(r["bibtex"].startswith("@article{"))
        self.assertEqual(fields(r["bibtex"])["journal"], "arXiv preprint arXiv:1706.03762")
        f = fields(bibtex.render(spec, {"arxiv_style": "misc"})["bibtex"])
        self.assertEqual((f["eprint"], f["archiveprefix"], f["primaryclass"]), ("1706.03762", "arXiv", "cs.CL"))

    def test_accents_and_caps(self):
        spec = dict(SPEC, title="Gaussian processes for Erdős graphs", authors=[{"given": "Pál", "family": "Erdős"}])
        f = fields(bibtex.render(spec)["bibtex"])
        self.assertIn("{Gaussian}", f["title"])
        self.assertIn(r"\H{o}", f["author"])
        f = fields(bibtex.render(spec, {"accents": "unicode", "protect_caps": False})["bibtex"])
        self.assertEqual(f["author"], "Erdős, Pál")
        self.assertNotIn("{", f["title"])

    def test_bad_settings_are_ignored(self):
        st = bibtex.merged_settings({"key_style": "rien", "max_authors": "beaucoup", "url": 3})
        self.assertEqual((st["key_style"], st["max_authors"], st["url"]), ("scholar", 100, "auto"))


class Csl(unittest.TestCase):
    def test_conference(self):
        e = bibtex.parse_one(bibtex.render(SPEC)["bibtex"])
        c = bibtex.to_csl(e)
        self.assertEqual(c["type"], "paper-conference")
        self.assertEqual(c["container-title"], SPEC["venue"])
        self.assertEqual(c["author"][0], {"family": "Radford", "given": "Alec"})
        self.assertEqual(c["issued"], {"date-parts": [[2021]]})
        self.assertEqual(c["page"], "8748-8763")
        self.assertEqual(c["collection-title"], "Proceedings of Machine Learning Research")

    def test_arxiv(self):
        e = bibtex.parse_one("@article{v, title={Attention Is All You Need}, author={Vaswani, Ashish}, "
                             "journal={arXiv preprint arXiv:1706.03762}, year={2017}}")
        c = bibtex.to_csl(e)
        self.assertEqual((c["type"], c["number"], c["URL"]), ("article", "arXiv:1706.03762", "https://arxiv.org/abs/1706.03762"))
        self.assertNotIn("container-title", c)


class Venues(unittest.TestCase):
    def test_family(self):
        for v in ("NeurIPS 2023 poster", "Advances in Neural Information Processing Systems 30", "NIPS"):
            self.assertEqual(venues.family(v), "neurips", v)
        self.assertEqual(venues.family("Proceedings of the 2019 Conference on Empirical Methods in Natural Language Processing"), "emnlp")

    def test_short_form(self):
        self.assertEqual(venues.short_form("Findings of the Association for Computational Linguistics: EMNLP 2021"), "Findings of EMNLP")
        self.assertEqual(venues.short_form("Proceedings of the IEEE/CVF Conference on Computer Vision and Pattern Recognition Workshops"),
                         "CVPR Workshops")
        self.assertEqual(venues.short_form("Advances in Neural Information Processing Systems Datasets and Benchmarks"),
                         "NeurIPS Datasets and Benchmarks")

    def test_compatible(self):
        self.assertTrue(venues.compatible("J. Mach. Learn. Res.", "Journal of Machine Learning Research"))
        self.assertFalse(venues.compatible("Nature", "Science"))
        # Même nombre de mots des deux côtés : l'ordre des arguments ne doit rien changer.
        self.assertTrue(venues.compatible("Transactions on Machine Learning Research", "Trans. Mach. Learn. Res."))
        self.assertTrue(venues.compatible("Trans. Mach. Learn. Res.", "Transactions on Machine Learning Research"))

    def test_iso4_abbreviations(self):
        """Abréviations de DBLP rattachées aux revues connues (une seule version publiée)."""
        for abbr, fam in (("Trans. Mach. Learn. Res.", "tmlr"), ("Int. J. Comput. Vis.", "ijcv"),
                          ("Nat. Mach. Intell.", "natmi"), ("Mach. Learn.", "mlj")):
            self.assertEqual(venues.family(abbr), fam, abbr)

    def test_acronym(self):
        self.assertEqual(venues.acronym_in("Proceedings of the Symposium on Foo Bar (FOOBAR)"), "FOOBAR")
        self.assertEqual(venues.acronym_in("3rd International Conference, ICLR 2015, San Diego"), "ICLR")


if __name__ == "__main__":
    unittest.main()
