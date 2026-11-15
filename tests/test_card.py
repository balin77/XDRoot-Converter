import shutil
import tempfile
import unittest
from pathlib import Path

from tests.helpers import fixture_bytes
from xdroot_converter import card


class CodecString(unittest.TestCase):
    def test_derive(self):
        self.assertEqual(card.derive_xavcl_codec("AVC100CBG_1920_1080_H422IP@L42"), "AVC50_1920_1080_H422P@L42")
        self.assertEqual(card.derive_xavcl_codec("AVC100CBG_1920_1080_H422IP@L41"), "AVC50_1920_1080_H422P@L41")

    def test_unknown_not_guessed(self):
        for s in ("", "MPEG2HD50_1920_1080", "AVC50_1920_1080_H422P@L42", "AVC100CBG_3840_2160_H422IP@L51",
                  "AVC100CBG_1920_1080_H422IP@L4", "AVC300CBG_1920_1080_H422IP@L42"):
            self.assertIsNone(card.derive_xavcl_codec(s), s)

    def test_is_xavcl_and_level(self):
        self.assertTrue(card.is_xavcl_codec("AVC50_1920_1080_H422P@L42"))
        self.assertFalse(card.is_xavcl_codec("AVC100CBG_1920_1080_H422IP@L42"))
        self.assertEqual(card.level_from_codec("AVC50_1920_1080_H422P@L42"), "4.2")
        self.assertEqual(card.level_from_codec("AVC50_1920_1080_H422P@L41"), "4.1")
        self.assertIsNone(card.level_from_codec("x"))


class MediaProParse(unittest.TestCase):
    def test_f55(self):
        mp = card.parse_mediapro(fixture_bytes("f55_MEDIAPRO.XML"))
        self.assertEqual(mp.system_kind, "PMW-F55 ver.9.000")
        self.assertEqual(len(mp.materials), 1)  # PD-EDL (Take) zaehlt nicht
        m = mp.materials[0]
        self.assertEqual(m.rel_path, "Clip/PDW-F55_6753.MXF")
        self.assertEqual(m.dur, 94760)
        self.assertEqual(m.ch, 8)
        self.assertEqual(m.video_type, "AVC100CBG_1920_1080_H422IP@L42")
        self.assertEqual(m.umid, "060A2B340101010501010D431300000041A62C70591306DD08004602026B88C4")
        self.assertIs(mp.find("clip/pdw-f55_6753.mxf"), m)

    def test_fx6(self):
        mp = card.parse_mediapro(fixture_bytes("fx6_MEDIAPRO.XML"))
        self.assertGreaterEqual(len(mp.materials), 6)
        self.assertTrue(all(card.is_xavcl_codec(m.video_type) for m in mp.materials))

    def test_clip_xml(self):
        c = card.parse_clip_xml(fixture_bytes("f55_M01.XML"))
        self.assertEqual(c.video_codec, "AVC100CBG_1920_1080_H422IP@L42")
        self.assertEqual(c.duration, 94760)
        self.assertEqual(c.num_audio, 8)
        self.assertEqual(c.umid_ref, "060A2B340101010501010D431300000041A62C70591306DD08004602026B88C4")
        c2 = card.parse_clip_xml(fixture_bytes("fx6_M01.XML"))
        self.assertEqual(c2.video_codec, "AVC50_1920_1080_H422P@L42")


class Patching(unittest.TestCase):
    def test_mediapro_only_videotype_changes(self):
        src = fixture_bytes("f55_MEDIAPRO.XML")
        new, changed = card.patch_mediapro(src, {"Clip/PDW-F55_6753.MXF": "AVC50_1920_1080_H422P@L42"})
        self.assertEqual(changed, ["./Clip/PDW-F55_6753.MXF"])
        self.assertEqual(new, src.replace(b"AVC100CBG_1920_1080_H422IP@L42", b"AVC50_1920_1080_H422P@L42"))
        self.assertIn(b"\r\n", new)  # Zeilenenden bleiben

    def test_mediapro_unlisted_untouched(self):
        src = fixture_bytes("f55_MEDIAPRO.XML")
        new, changed = card.patch_mediapro(src, {"Clip/ANDERS.MXF": "AVC50_1920_1080_H422P@L42"})
        self.assertEqual(new, src)
        self.assertEqual(changed, [])

    def test_mediapro_spaces_umlaut_bom_entities(self):
        xml = (
            '﻿<?xml version="1.0" encoding="UTF-8"?>\r\n<MediaProfile xmlns="http://xmlns.sony.net/pro/metadata/mediaprofile">\r\n'
            '\t<Contents>\r\n'
            '\t\t<Material uri="./Clip/PDW F5_0152 ä&amp;.MXF" type="MXF" dur="1" fps="50p"\r\n'
            '\t\t\tvideoType="AVC100CBG_1920_1080_H422IP@L42" audioType="LPCM24"\r\n\t\t\tumid="AA">\r\n'
            '\t\t</Material>\r\n'
            '\t\t<Material uri="./Clip/B.MXF" type="MXF" videoType="AVC100CBG_1920_1080_H422IP@L42" umid="BB"/>\r\n'
            '\t</Contents>\r\n</MediaProfile>\r\n'
        ).encode("utf-8")
        new, changed = card.patch_mediapro(xml, {"Clip/PDW F5_0152 ä&.MXF": "AVC50_1920_1080_H422P@L42"})
        self.assertEqual(len(changed), 1)
        self.assertEqual(new, xml.replace(b"AVC100CBG_1920_1080_H422IP@L42", b"AVC50_1920_1080_H422P@L42", 1))
        self.assertTrue(new.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(new.count(b"AVC100CBG"), 1)  # B.MXF unveraendert

    def test_clip_xml(self):
        src = fixture_bytes("f55_M01.XML")
        new, ok = card.patch_clip_xml(src, "AVC50_1920_1080_H422P@L42")
        self.assertTrue(ok)
        self.assertEqual(new, src.replace(b"AVC100CBG_1920_1080_H422IP@L42", b"AVC50_1920_1080_H422P@L42"))

    def test_clip_xml_already_target(self):
        src = fixture_bytes("fx6_M01.XML")
        new, ok = card.patch_clip_xml(src, "AVC50_1920_1080_H422P@L42")
        self.assertFalse(ok)
        self.assertEqual(new, src)

    def test_non_utf8_bytes_roundtrip(self):
        src = (b'<NonRealTimeMeta>\r\n<Lens modelName="\xe4\xf6"/>'
               b'<VideoFrame videoCodec="AVC100CBG_1920_1080_H422IP@L42" captureFps="50.00p"/></NonRealTimeMeta>')
        new, ok = card.patch_clip_xml(src, "AVC50_1920_1080_H422P@L42")
        self.assertTrue(ok)
        self.assertEqual(new, src.replace(b"AVC100CBG_1920_1080_H422IP@L42", b"AVC50_1920_1080_H422P@L42"))


class Folders(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def make_card(self, root: Path):
        for d in ("Clip", "Take", "Sub", "Edit", "General", "Thmbnl", "UserData"):
            (root / d).mkdir(parents=True)
        (root / "MEDIAPRO.XML").write_bytes(fixture_bytes("f55_MEDIAPRO.XML"))
        (root / "CUEUP.XML").write_text("x")
        (root / "Clip" / "A B.MXF").write_bytes(b"mxf")
        (root / "Clip" / "A BM01.XML").write_text("m")
        (root / "Clip" / "A BR01.BIM").write_text("b")
        (root / "Clip" / "A B2M01.XML").write_text("other clip")
        (root / "Thmbnl" / "A BT01.JPG").write_text("j")

    def test_find_xdroot(self):
        self.make_card(self.tmp / "card" / "XDROOT")
        self.assertEqual(card.find_xdroot(self.tmp / "card"), self.tmp / "card" / "XDROOT")
        self.assertEqual(card.find_xdroot(self.tmp / "card" / "XDROOT"), self.tmp / "card" / "XDROOT")
        with self.assertRaises(FileNotFoundError):
            card.find_xdroot(self.tmp)

    def test_check_target(self):
        src = self.tmp / "card" / "XDROOT"
        self.make_card(src)
        self.assertEqual(card.check_target(src, self.tmp / "out"), self.tmp / "out" / "XDROOT")
        for bad in (self.tmp / "card", src, src / "Clip"):
            with self.assertRaises(ValueError, msg=str(bad)):
                card.check_target(src, bad)

    def test_copy_structure(self):
        src = self.tmp / "card" / "XDROOT"
        self.make_card(src)
        dst = self.tmp / "out" / "XDROOT"
        n = card.copy_structure(src, dst)
        self.assertTrue((dst / "Sub").is_dir())  # leere Ordner
        self.assertTrue((dst / "Clip" / "A BM01.XML").is_file())
        self.assertFalse((dst / "Clip" / "A B.MXF").exists())
        self.assertEqual(n, 6)
        s, d = (src / "CUEUP.XML").stat().st_mtime, (dst / "CUEUP.XML").stat().st_mtime
        self.assertAlmostEqual(s, d, delta=2)

    def test_copy_structure_skip_stems(self):
        src = self.tmp / "card" / "XDROOT"
        self.make_card(src)
        dst = self.tmp / "out" / "XDROOT"
        card.copy_structure(src, dst, skip_stems={"a b"})
        self.assertFalse((dst / "Clip" / "A BM01.XML").exists())
        self.assertFalse((dst / "Clip" / "A BR01.BIM").exists())
        self.assertFalse((dst / "Thmbnl" / "A BT01.JPG").exists())
        self.assertTrue((dst / "Clip" / "A B2M01.XML").exists())  # anderer Clip "A B2"

    def test_list_clip_mxf(self):
        src = self.tmp / "card" / "XDROOT"
        self.make_card(src)
        self.assertEqual([p.name for p in card.list_clip_mxf(src)], ["A B.MXF"])


if __name__ == "__main__":
    unittest.main()
