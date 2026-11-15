import unittest
from fractions import Fraction

from xdroot_converter import converter, verify


class Helpers(unittest.TestCase):
    def test_date_arg(self):
        self.assertEqual(converter.mxf_date_to_arg("2023-05-29 16:46:52.000"), "2023-05-29T16:46:52:00")
        self.assertEqual(converter.mxf_date_to_arg("2023-05-29 16:46:52.500"), "2023-05-29T16:46:52:125")
        self.assertIsNone(converter.mxf_date_to_arg("garbage"))

    def test_estimate(self):
        # 31:35 min 50p, 8 Kanaele: ca. 12 GB Video + 2.2 GB Audio
        b = converter.estimate_clip_bytes(94760, Fraction(50), 8, 50)
        self.assertTrue(12e9 < b < 16e9, b)

    def test_format(self):
        self.assertEqual(converter.fmt_size(512), "512 B")
        self.assertEqual(converter.fmt_size(1536), "1.50 KB")
        self.assertEqual(converter.fmt_duration(75), "1:15")
        self.assertEqual(converter.fmt_duration(3725), "1:02:05")

    def test_constants_match_fx6_reference(self):
        self.assertEqual(verify.XAVCL_ESSENCE_TYPE, "AVC_High_422")
        self.assertTrue(verify.XAVCL_CODING_LABEL.endswith("04010202.01316001"))

    def test_summary_text(self):
        s = converter.RunSummary(results=[converter.ClipResult("a.MXF", converter.ST_CONVERTED, 1000, 250, 10, 1.0)])
        self.assertIn("umgewandelt: 1", s.text())


if __name__ == "__main__":
    unittest.main()
