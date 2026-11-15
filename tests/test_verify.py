import copy
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.helpers import fixture_bytes, fixture_text
from xdroot_converter import card, converter, mxfinfo, verify


def _info(name: str) -> mxfinfo.MxfInfo:
    return mxfinfo.parse_mxf2raw(fixture_text(name))


class CompareInfoHeader(unittest.TestCase):
    def test_identical_is_clean(self):
        for n in ("f55_mxf2raw.txt", "fx6_mxf2raw.txt"):
            self.assertEqual(verify.compare_info_header(_info(n), _info(n)), [], n)

    def test_bmx_identification_detected(self):
        src, tgt = _info("f55_mxf2raw.txt"), _info("f55_mxf2raw.txt")
        tgt.ident_company, tgt.ident_product = "BBC", "bmx"
        tgt.ident_toolkit_version, tgt.ident_platform = "1.6.0.0.0", "libMXF (Linux 64-bit)"
        msgs = verify.compare_info_header(tgt, src)
        text = "\n".join(msgs)
        for k in ("company_name", "product_name", "toolkit_version", "platform"):
            self.assertIn(k, text)

    def test_trailing_space_matters(self):
        src, tgt = _info("f55_mxf2raw.txt"), _info("f55_mxf2raw.txt")
        tgt.ident_product = "Mem"
        self.assertTrue(verify.compare_info_header(tgt, src))

    def test_track_ids_and_audio_descriptor(self):
        src, tgt = _info("f55_mxf2raw.txt"), _info("f55_mxf2raw.txt")
        tgt.video.mp_track_id = 1001
        tgt.audio[0].fp_track_id = 2001
        tgt.audio[1].fp_track_number = "0x16040399"
        tgt.audio[2].descriptor = dict(tgt.audio[2].descriptor, locked="false", audio_ref_level="3")
        text = "\n".join(verify.compare_info_header(tgt, src))
        self.assertIn("Videospur: Track-ID im Material Package 1001 statt 2", text)
        self.assertIn("Audiospur 1: Track-ID im File Package 2001 statt 3", text)
        self.assertIn("Audiospur 2: Track-Nummer", text)
        self.assertIn("locked", text)
        self.assertIn("audio_ref_level", text)


class SonyPattern(unittest.TestCase):
    def test_references_clean(self):
        self.assertEqual(verify.check_sony_pattern(_info("fx6_mxf2raw.txt")), [])
        self.assertEqual(verify.check_sony_pattern(_info("f55_mxf2raw.txt")), [])

    def test_bmx_pattern_detected(self):
        i = _info("f55_mxf2raw.txt")
        i.ident_company, i.ident_product, i.ident_toolkit_version = "BBC", "bmx", "1.6"
        for t in i.tracks:
            t.mp_track_id = {"Picture": 1001, "Sound": 2001, "Data": 3001}[t.kind]
            t.fp_track_id = t.mp_track_id
        text = "\n".join(verify.check_sony_pattern(i))
        self.assertIn("BBC", text)
        self.assertIn("bmx", text)
        self.assertIn("toolkit_version", text)
        self.assertIn("Videospur: Track-ID im Material Package 1001 statt 2", text)
        self.assertIn("ANC-Spur: Track-ID im File Package 3001 statt 11", text)


class ProdInfo(unittest.TestCase):
    def test_from_source(self):
        a = converter.prod_info_args(_info("f55_mxf2raw.txt"))
        self.assertEqual(a, ["--prod-info", "Sony", "Mem ", "0.0.0.0.0", "2.00",
                             "urn:uuid:cede1104-8280-11de-8a39-08004678031c"])

    def test_fallback(self):
        a = converter.prod_info_args(mxfinfo.MxfInfo())
        self.assertEqual(a[1:3], ["Sony", "Mem "])
        self.assertEqual(a[4], "2.00")


class CompareFilesUnlisted(unittest.TestCase):
    """Umgewandelter Clip ohne Eintrag in MEDIAPRO.XML: M01.XML darf keinen Fehlalarm ausloesen."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src, self.tgt = self.tmp / "s", self.tmp / "t"
        for d in (self.src, self.tgt):
            (d / "Clip").mkdir(parents=True)
        self.xml = fixture_bytes("f55_M01.XML")

    def _run(self, tgt_xml: bytes):
        (self.src / "Clip" / "X_0001M01.XML").write_bytes(self.xml)
        (self.tgt / "Clip" / "X_0001M01.XML").write_bytes(tgt_xml)
        rep = verify.VerifyReport()
        empty = card.MediaPro()  # Clip steht NICHT in MEDIAPRO.XML
        verify._compare_files(self.src, self.tgt, empty, empty, rep)
        return rep

    def test_converted_unlisted_ok(self):
        new, _ = card.patch_clip_xml(self.xml, "AVC50_1920_1080_H422P@L42")
        self.assertEqual(self._run(new).problems, [])

    def test_unchanged_unlisted_ok(self):
        self.assertEqual(self._run(self.xml).problems, [])

    def test_other_change_detected(self):
        new, _ = card.patch_clip_xml(self.xml, "AVC50_1920_1080_H422P@L42")
        new = new.replace(b"Duration", b"Durat1on", 1)
        self.assertTrue(self._run(new).problems)

    def test_unrelated_difference_detected(self):
        bad = self.xml.replace(b"AVC100CBG", b"AVC100XBG", 1)
        self.assertTrue(self._run(bad).problems)


if __name__ == "__main__":
    unittest.main()
