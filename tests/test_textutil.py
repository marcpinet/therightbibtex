import support  # noqa: F401  (isole l'environnement)

import unittest

from therightbibtex.textutil import (
    Author, norm_arxiv, norm_doi, norm_pages, norm_title, parse_name, protect_title, text_to_latex,
    main_title_match, title_match, title_similarity, year_of,
)

EN, EM = "\N{EN DASH}", "\N{EM DASH}"


class Identifiers(unittest.TestCase):
    def test_doi(self):
        self.assertEqual(norm_doi("https://doi.org/10.1145/3292500.3330701."), "10.1145/3292500.3330701")
        self.assertEqual(norm_doi("doi: 10.1016/S0140-6736(20)30183-5"), "10.1016/s0140-6736(20)30183-5")
        self.assertEqual(norm_doi("(see 10.1038/nature14539)"), "10.1038/nature14539")
        self.assertEqual(norm_doi("pas un doi"), "")

    def test_arxiv(self):
        for s in ("arXiv:1706.03762v5", "https://arxiv.org/abs/1706.03762", "https://arxiv.org/pdf/1706.03762v2.pdf"):
            self.assertEqual(norm_arxiv(s), "1706.03762", s)
        self.assertEqual(norm_arxiv("hep-th/9901001v2"), "hep-th/9901001")


class Text(unittest.TestCase):
    def test_pages(self):
        self.assertEqual(norm_pages(f"12{EM}34"), "12--34")
        self.assertEqual(norm_pages(f"12 {EN} 34"), "12--34")
        self.assertEqual(norm_pages("5\N{NON-BREAKING HYPHEN}9"), "5--9")
        self.assertEqual(norm_pages("770-778"), "770--778")
        self.assertEqual(norm_pages("42--42"), "42")

    def test_latex(self):
        self.assertEqual(text_to_latex(f"A{EM}B"), "A---B")
        self.assertEqual(text_to_latex(f"A{EN}B"), "A--B")
        self.assertEqual(text_to_latex(f"A{EM}B", "unicode"), f"A{EM}B")
        self.assertEqual(text_to_latex("a\N{NO-BREAK SPACE}b"), "a~b")
        self.assertEqual(text_to_latex("R&D 100%"), r"R\&D 100\%")
        self.assertEqual(text_to_latex("Erdős"), r"Erd{\H{o}}s")
        self.assertEqual(text_to_latex("Erdős", "unicode"), "Erdős")
        self.assertEqual(text_to_latex("$x_1$ & y", protect_math=True), r"{$x_1$} \& y")

    def test_protect(self):
        self.assertEqual(protect_title("BERT for Bayesian inference"), "{BERT} for {Bayesian} inference")
        self.assertEqual(protect_title("Q-learning in 3D"), "{Q-learning} in {3D}")
        self.assertEqual(protect_title("A simple model"), "A simple model")

    def test_titles(self):
        self.assertEqual(norm_title("Attention Is All You Need."), "attention is all you need")
        self.assertEqual(title_similarity("Attention is all you need", "Attention Is All You Need"), 1.0)
        self.assertLess(title_similarity("U-Net: convolutional networks", "INet: convolutional networks"), 0.9)
        # Titre tapé sans son sous-titre : retenu par les sources qui filtrent elles-mêmes.
        full = "When Foundation Models are One-Liners: Limitations and Future Directions for Time Series Anomaly Detection"
        self.assertGreaterEqual(title_match(full, "When Foundation Models are One-Liners"), 0.9)
        self.assertLess(title_match(full, "Foundation Models"), 0.8)   # trop court pour compter
        # Nom court du papier, avant les deux-points.
        mts = "mTSBench: Benchmarking Multivariate Time Series Anomaly Detection and Model Selection at Scale"
        self.assertGreaterEqual(title_match(mts, "mTSBench"), 0.9)
        self.assertEqual(main_title_match("Benchmarking", mts), 0.0)    # mot du sous-titre, pas le nom
        self.assertEqual(main_title_match("The", "The: a tool"), 0.0)   # mot vide

    def test_year(self):
        self.assertEqual(year_of("NeurIPS 2017"), 2017)
        self.assertIsNone(year_of("sans année"))


class Names(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_name("Laurens van der Maaten"), Author("Laurens", "van der Maaten"))
        self.assertEqual(parse_name("van der Maaten, Laurens"), Author("Laurens", "van der Maaten"))
        self.assertEqual(parse_name("Wei Wang 0001"), Author("Wei", "Wang"))
        self.assertEqual(parse_name("King, Jr., Martin"), Author("Martin", "King Jr."))
        self.assertTrue(parse_name("OpenAI Team").corporate)


if __name__ == "__main__":
    unittest.main()
