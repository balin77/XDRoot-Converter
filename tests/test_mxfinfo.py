import unittest
from fractions import Fraction

from tests.helpers import fixture_text
from xdroot_converter import mxfinfo


class RateHelpers(unittest.TestCase):
    def test_parse_fraction(self):
        self.assertEqual(mxfinfo.parse_fraction("50/1"), Fraction(50))
        self.assertEqual(mxfinfo.parse_fraction("30000/1001"), Fraction(30000, 1001))
        self.assertEqual(mxfinfo.parse_fraction("25"), Fraction(25))
        with self.assertRaises(ValueError):
            mxfinfo.parse_fraction("0/0")

    def test_rate_arg(self):
        self.assertEqual(mxfinfo.rate_arg(Fraction(50)), "50")
        self.assertEqual(mxfinfo.rate_arg(Fraction(30000, 1001)), "30000/1001")
        self.assertEqual(mxfinfo.rate_arg(Fraction(60000, 1001)), "60000/1001")

    def test_gop(self):
        cases = {Fraction(50): 24, Fraction(25): 12, Fraction(30000, 1001): 15, Fraction(60000, 1001): 30,
                 Fraction(24): 12, Fraction(24000, 1001): 12, Fraction(60): 30, Fraction(30): 15}
        for fps, n in cases.items():
            self.assertEqual(mxfinfo.gop_length(fps), n, fps)
            self.assertEqual(mxfinfo.gop_length(fps) % 3, 0)

    def test_umid(self):
        u = "urn:smpte:umid:060a2b34.01010105.01010d43.13000000.fe36c56e.920906db.9c50d1ff.feae4ac3"
        n = mxfinfo.normalize_umid(u)
        self.assertEqual(n, "060A2B340101010501010D4313000000FE36C56E920906DB9C50D1FFFEAE4AC3")
        self.assertEqual(len(n), 64)


class Mxf2rawParse(unittest.TestCase):
    def test_fx6(self):
        i = mxfinfo.parse_mxf2raw(fixture_text("fx6_mxf2raw.txt"))
        self.assertEqual(i.duration, 962)
        self.assertEqual(i.edit_rate, Fraction(50))
        self.assertEqual(i.start_tc, "10:22:08:12")
        self.assertEqual(i.mp_uid, "060A2B340101010501010D4313000000FE36C56E920906DB9C50D1FFFEAE4AC3")
        self.assertEqual(i.fp_uid, "060A2B340101010501010D4313000000FF36C56E920906DB9C50D1FFFEAE4AC3")
        self.assertEqual(i.ident_modified, "2025-11-13 10:23:05.000")
        self.assertEqual(len(i.tracks), 6)
        self.assertEqual(len(i.audio), 4)
        self.assertEqual(i.video.essence_type, "AVC_High_422")
        self.assertEqual(i.video.descriptor["coding_label"], "urn:smpte:ul:060e2b34.0401010d.04010202.01316001")
        self.assertEqual(i.video.ec_label, "urn:smpte:ul:060e2b34.0401010a.0d010301.02106001")
        self.assertEqual(i.audio[0].ec_label, "urn:smpte:ul:060e2b34.04010101.0d010301.02060300")
        self.assertEqual(i.audio[0].descriptor["bits_per_sample"], "24")
        self.assertEqual(len(i.anc_tracks), 1)
        self.assertEqual(len(i.anc_tracks[0].anc_elements), 13)
        self.assertEqual(i.anc_tracks[0].anc_elements[0]["did"], "0x43")
        self.assertEqual(i.anc_tracks[0].anc_elements[12]["did"], "0x53")
        self.assertEqual(len(i.text_objects), 1)
        self.assertEqual(i.text_objects[0]["scheme_id"], "urn:uuid:786d4d22-8280-11de-8a39-08004678031c")
        self.assertEqual(len(i.warnings), 4)

    def test_f55(self):
        i = mxfinfo.parse_mxf2raw(fixture_text("f55_mxf2raw.txt"))
        self.assertEqual(i.duration, 94760)
        self.assertEqual(i.start_tc, "00:00:00:00")
        self.assertEqual(i.video.essence_type, "AVCI_100_1080p")
        self.assertEqual(len(i.audio), 8)
        self.assertEqual(len(i.anc_tracks[0].anc_elements), 1)
        self.assertEqual(i.text_objects, [])
        self.assertEqual(i.audio[0].duration, 94760)


class Mxf2rawHeaderFields(unittest.TestCase):
    """Kopfdaten-Felder (Runde 2): Identification, Track-IDs, TextObject."""

    def test_fx6_identification_keeps_trailing_space(self):
        i = mxfinfo.parse_mxf2raw(fixture_text("fx6_mxf2raw.txt"))
        self.assertEqual(i.ident_company, "Sony")
        self.assertEqual(i.ident_product, "Mem ")
        self.assertEqual(i.ident_version_string, "2.00")
        self.assertEqual(i.ident_product_uid, "urn:uuid:cede1104-8280-11de-8a39-08004678031c")
        self.assertEqual(i.ident_generation_uid, "urn:uuid:dfd78a94-0639-1238-980e-9c50d1ae4ac3")
        self.assertEqual(i.ident_toolkit_version, "")
        self.assertEqual(i.ident_platform, "")
        self.assertEqual(i.ident_product_version, "")

    def test_fx6_track_ids(self):
        i = mxfinfo.parse_mxf2raw(fixture_text("fx6_mxf2raw.txt"))
        self.assertEqual(i.video.mp_track_id, 2)
        self.assertEqual(i.video.fp_track_id, 2)
        self.assertEqual([a.mp_track_id for a in i.audio], [3, 4, 5, 6])
        self.assertEqual([a.fp_track_id for a in i.audio], [3, 4, 5, 6])
        self.assertEqual(i.anc_tracks[0].mp_track_id, 7)
        self.assertEqual(i.anc_tracks[0].fp_track_id, 7)
        self.assertEqual(i.audio[0].fp_track_number, "0x16040300")
        self.assertEqual(i.audio[1].fp_track_number, "0x16040301")
        self.assertEqual(i.video.fp_track_number, "0x15010500")

    def test_f55_track_ids(self):
        i = mxfinfo.parse_mxf2raw(fixture_text("f55_mxf2raw.txt"))
        self.assertEqual(i.video.mp_track_id, 2)
        self.assertEqual(i.video.fp_track_id, 2)
        self.assertEqual([a.mp_track_id for a in i.audio], list(range(3, 11)))
        self.assertEqual([a.fp_track_id for a in i.audio], list(range(3, 11)))
        self.assertEqual(i.anc_tracks[0].mp_track_id, 11)
        self.assertEqual(i.anc_tracks[0].fp_track_id, 11)
        self.assertEqual(i.ident_product, "Mem ")
        self.assertEqual(i.ident_company, "Sony")

    def test_text_object_fields(self):
        i = mxfinfo.parse_mxf2raw(fixture_text("fx6_mxf2raw.txt"))
        t = i.text_objects[0]
        self.assertEqual(t["mime_type"], "text/xml")
        self.assertEqual(t["lang_code"], "und")
        self.assertEqual(t["description"], "")
        self.assertEqual(t["track_id"], "8")
        self.assertEqual(t["scheme_id"], "urn:uuid:786d4d22-8280-11de-8a39-08004678031c")

    def test_bmx_toolkit_platform(self):
        txt = (
            "Files: (1)\n  File #0:\n    Identifications: (1)\n      Identification #0:\n"
            "        generation_uid  : urn:uuid:00000000-0000-0000-0000-000000000001\n"
            "        company_name    : BBC\n        product_name    : bmx\n"
            "        product_version : 1.6.0.0.0\n        version_string  : 1.6\n"
            "        product_uid     : urn:uuid:f3f4f4a6-9c6b-4dc5-9a35-fee1a2b3c4d5\n"
            "        modified_date   : 2026-01-01 10:00:00.000\n"
            "        toolkit_version : libMXF 1.0\n        platform        : Linux x86_64\n"
        )
        i = mxfinfo.parse_mxf2raw(txt)
        self.assertEqual(i.ident_company, "BBC")
        self.assertEqual(i.ident_product, "bmx")
        self.assertEqual(i.ident_toolkit_version, "libMXF 1.0")
        self.assertEqual(i.ident_platform, "Linux x86_64")
        self.assertEqual(i.ident_product_version, "1.6.0.0.0")


class FfprobeParse(unittest.TestCase):
    def test_parse(self):
        p = mxfinfo.parse_ffprobe(fixture_text("fx6_ffprobe.txt"))
        self.assertEqual(p.video["profile"], "High 4:2:2")
        self.assertEqual(p.video["pix_fmt"], "yuv422p10le")
        self.assertEqual(p.video["level"], "42")
        self.assertEqual(len(p.audio), 4)
        self.assertTrue(p.format)

    def test_decide_f55_converts(self):
        i = mxfinfo.parse_mxf2raw(fixture_text("f55_mxf2raw.txt"))
        p = mxfinfo.parse_ffprobe(fixture_text("f55_ffprobe.txt"))
        d = mxfinfo.decide_source(i, p)
        self.assertTrue(d.convert, d.reason)
        self.assertEqual(d.fps, Fraction(50))

    def test_decide_fx6_not_converted(self):
        i = mxfinfo.parse_mxf2raw(fixture_text("fx6_mxf2raw.txt"))
        p = mxfinfo.parse_ffprobe(fixture_text("fx6_ffprobe.txt"))
        d = mxfinfo.decide_source(i, p)
        self.assertFalse(d.convert)
        self.assertIn("Intra", d.reason)

    def test_decide_rejects_4k_and_interlaced(self):
        i = mxfinfo.parse_mxf2raw(fixture_text("f55_mxf2raw.txt"))
        p = mxfinfo.parse_ffprobe(fixture_text("f55_ffprobe.txt"))
        p.video["width"], p.video["height"] = "3840", "2160"
        self.assertFalse(mxfinfo.decide_source(i, p).convert)
        p.video["width"], p.video["height"] = "1920", "1080"
        p.video["field_order"] = "tt"
        self.assertFalse(mxfinfo.decide_source(i, p).convert)


if __name__ == "__main__":
    unittest.main()
