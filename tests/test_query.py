import support  # noqa: F401

import unittest

from therightbibtex.query import parse_input


class Identifiers(unittest.TestCase):
    def check(self, text, **expected):
        q = parse_input(text)
        for k, v in expected.items():
            self.assertEqual(getattr(q, k), v, f"{text!r} : {k}")
        return q

    def test_ids(self):
        self.check("10.1038/nature14539", kind="id", doi="10.1038/nature14539")
        self.check("doi:10.1038/nature14539", doi="10.1038/nature14539")
        self.check("arXiv:2010.11929", kind="id", arxiv="2010.11929")
        self.check("hep-th/9901001", arxiv="hep-th/9901001")
        self.check("math.GT/0309136", kind="id", arxiv="math.GT/0309136")   # pas un lien
        self.check("PMID: 31234567", ids={"pmid": "31234567"})
        self.check("PMC1234567", ids={"pmcid": "PMC1234567"})
        self.check("10.48550/arXiv.1706.03762", arxiv="1706.03762", doi="")

    def test_urls(self):
        self.check("https://arxiv.org/abs/1706.03762v5", kind="id", arxiv="1706.03762")
        self.check("arxiv.org/pdf/1706.03762v5", kind="id", arxiv="1706.03762")           # sans https://
        self.check("doi.org/10.1038/nature14539", doi="10.1038/nature14539")
        self.check("www.nature.com/articles/nature14539", kind="url")
        self.check("https://openreview.net/pdf?id=YicbFdNTTy", ids={"openreview": "YicbFdNTTy"})
        self.check("https://aclanthology.org/N19-1423.pdf", ids={"acl": "N19-1423"})
        self.check("https://dl.acm.org/doi/10.1145/3292500.3330701", doi="10.1145/3292500.3330701")
        self.check("https://www.biorxiv.org/content/10.1101/2020.03.01.123456v2.full", doi="10.1101/2020.03.01.123456")
        q = self.check("https://proceedings.neurips.cc/paper/2017/hash/3f5ee243547dee91fbd053c1c4a845aa-Abstract.html",
                       kind="url")
        self.assertTrue(q.url.startswith("https://proceedings.neurips.cc/"))

    def test_title(self):
        self.check("Attention is all you need", kind="title", title="Attention is all you need")
        self.check("\N{ZERO WIDTH SPACE}Attention\N{SOFT HYPHEN} is all you need.", title="Attention is all you need")

    def test_bibtex(self):
        q = self.check("@article{x, title={Deep {R}esidual Learning}, author={He, Kaiming and Sun, Jian}, "
                       "journal={arXiv preprint arXiv:1512.03385}, year={2015}}", kind="bibtex")
        self.assertEqual(q.arxiv, "1512.03385")
        self.assertEqual(q.authors, ["He", "Sun"])


class References(unittest.TestCase):
    """Titre, année et noms lus dans une référence copiée d'un article."""

    CASES = [
        # APA, avec une césure de fin de ligne
        ("He, K., Zhang, X., Ren, S., & Sun, J. (2016). Deep residual learn-\ning for image recognition. "
         "In CVPR, pages 770-778.", "Deep residual learning for image recognition", 2016, ["He", "Zhang", "Ren", "Sun"]),
        # natbib / NeurIPS, coupé sur deux lignes
        ("Kaiming He, Xiangyu Zhang, Shaoqing Ren, and Jian Sun. Deep residual learning for image\nrecognition. "
         "In CVPR, 2016.", "Deep residual learning for image recognition", 2016, None),
        # APA avec « vs. » dans le titre
        ("Ng, A. Y., & Jordan, M. I. (2002). On discriminative vs. generative classifiers: A comparison of logistic "
         "regression and naive Bayes. NeurIPS.",
         "On discriminative vs. generative classifiers: A comparison of logistic regression and naive Bayes", 2002,
         ["Ng", "Jordan"]),
        # ACL : « Auteurs. 2019. Titre. »
        ("Jacob Devlin, Ming-Wei Chang, Kenton Lee, and Kristina Toutanova. 2019. BERT: Pre-training of deep "
         "bidirectional transformers for language understanding. In Proceedings of NAACL-HLT, pages 4171-4186.",
         "BERT: Pre-training of deep bidirectional transformers for language understanding", 2019, None),
        # Vancouver
        ("He K, Zhang X, Ren S, Sun J. Deep residual learning for image recognition. In: CVPR; 2016. p. 770-8.",
         "Deep residual learning for image recognition", 2016, None),
        # IEEE, titre entre guillemets
        ('K. He, X. Zhang, S. Ren, and J. Sun, "Deep residual learning for image recognition," in CVPR, 2016.',
         "Deep residual learning for image recognition", 2016, None),
        # mot composé coupé sur son trait d'union, et identifiant arXiv qui ressemble à une année
        ("Ting Chen, Simon Kornblith, Mohammad Norouzi, and Geoffrey Hinton. A simple framework for self-\n"
         "supervised contrastive learning. arXiv preprint arXiv:2002.05709, 2020.",
         "A simple framework for self-supervised contrastive learning", 2020, None),
    ]

    def test_styles(self):
        for text, title, year, authors in self.CASES:
            q = parse_input(text)
            self.assertEqual(q.kind, "reference", text)
            self.assertEqual(q.title, title, text)
            self.assertEqual(q.year, year, text)
            if authors:
                self.assertEqual(q.authors, authors, text)

    def test_arxiv_in_reference(self):
        q = parse_input(self.CASES[-1][0])
        self.assertEqual(q.arxiv, "2002.05709")


if __name__ == "__main__":
    unittest.main()
