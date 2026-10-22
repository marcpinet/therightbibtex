import support  # noqa: F401

import unittest

from therightbibtex import pdfinfo


def sample_pdf() -> bytes:
    import pymupdf
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    page.insert_text((72, 100), "Deep Residual Learning for Image Recognition", fontsize=17)
    page.insert_text((72, 140), "Kaiming He  Xiangyu Zhang  Shaoqing Ren  Jian Sun", fontsize=10)
    page.insert_text((72, 180), "Published as a conference paper at ICLR 2024", fontsize=8)
    page.insert_text((72, 700), "DOI: 10.1109/CVPR.2016.90", fontsize=8)
    page.insert_text((30, 600), "arXiv:1512.03385v1 [cs.CV] 10 Dec 2015", fontsize=9, rotate=90)
    data = doc.tobytes()
    doc.close()
    return data


class Extract(unittest.TestCase):
    def test_isolated(self):
        info = pdfinfo.extract_isolated(sample_pdf())
        self.assertEqual(info["title"], "Deep Residual Learning for Image Recognition")
        self.assertEqual(info["doi"], "10.1109/cvpr.2016.90")
        self.assertEqual(info["arxiv"], "1512.03385")
        self.assertEqual(info["venue_hint"], "ICLR 2024")
        self.assertEqual(info["pages"], 1)

    def test_garbage(self):
        with self.assertRaises(ValueError):
            pdfinfo.extract_isolated(b"%PDF-1.4 ceci n'est pas un pdf")

    def test_timeout(self):
        with self.assertRaisesRegex(ValueError, "too long"):
            pdfinfo.extract_isolated(sample_pdf(), timeout=0.01)


if __name__ == "__main__":
    unittest.main()
