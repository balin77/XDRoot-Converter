import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

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

    def test_check_target_inside_chosen_source_parent(self):
        """K4: Wurde der Elternordner des XDROOT gewaehlt, darf das Ziel auch nicht dort drin liegen."""
        parent = self.tmp / "card"
        src = parent / "XDROOT"
        self.make_card(src)
        for bad in (parent / "out", parent / "x" / "y", parent):
            with self.assertRaises(ValueError, msg=str(bad)):
                card.check_target(src, bad, chosen_source=parent)
        # ohne chosen_source war "card/out" bisher erlaubt (Bug)
        self.assertEqual(card.check_target(src, parent / "out"), parent / "out" / "XDROOT")
        self.assertEqual(card.check_target(src, self.tmp / "ziel", chosen_source=parent), self.tmp / "ziel" / "XDROOT")
        self.assertEqual(card.check_target(src, self.tmp / "ziel", chosen_source=src), self.tmp / "ziel" / "XDROOT")

    def test_copy_structure(self):
        src = self.tmp / "card" / "XDROOT"
        self.make_card(src)
        dst = self.tmp / "out" / "XDROOT"
        n = card.copy_structure(src, dst)
        self.assertTrue((dst / "Sub").is_dir())  # leere Ordner
        self.assertTrue((dst / "Clip" / "A BR01.BIM").is_file())
        self.assertFalse((dst / "Clip" / "A B.MXF").exists())
        # MEDIAPRO.XML und Clip/*M??.XML schreibt der Ablauf selbst (zustandsabhaengig)
        self.assertFalse((dst / "MEDIAPRO.XML").exists())
        self.assertFalse((dst / "Clip" / "A BM01.XML").exists())
        self.assertEqual(n, 3)
        s, d = (src / "CUEUP.XML").stat().st_mtime, (dst / "CUEUP.XML").stat().st_mtime
        self.assertAlmostEqual(s, d, delta=2)

    def test_copy_structure_skip_stems(self):
        src = self.tmp / "card" / "XDROOT"
        self.make_card(src)
        dst = self.tmp / "out" / "XDROOT"
        card.copy_structure(src, dst, skip_stems={"a b"}, skip_xml=False)
        self.assertFalse((dst / "Clip" / "A BM01.XML").exists())
        self.assertFalse((dst / "Clip" / "A BR01.BIM").exists())
        self.assertFalse((dst / "Thmbnl" / "A BT01.JPG").exists())
        self.assertTrue((dst / "Clip" / "A B2M01.XML").exists())  # anderer Clip "A B2"

    def test_copy_structure_skip_xml_false_copies_everything(self):
        src = self.tmp / "card" / "XDROOT"
        self.make_card(src)
        dst = self.tmp / "out" / "XDROOT"
        n = card.copy_structure(src, dst, skip_xml=False)
        self.assertEqual(n, 6)
        self.assertTrue((dst / "MEDIAPRO.XML").is_file())
        self.assertTrue((dst / "Clip" / "A BM01.XML").is_file())

    def test_copy_structure_collects_errors_per_file(self):
        from unittest import mock
        src = self.tmp / "card" / "XDROOT"
        self.make_card(src)
        dst = self.tmp / "out" / "XDROOT"
        real = card.copy_file_atomic

        def flaky(s, d):
            if Path(s).name == "CUEUP.XML":
                raise OSError("kaputt")
            return real(s, d)

        errors = []
        with mock.patch.object(card, "copy_file_atomic", flaky):
            n = card.copy_structure(src, dst, errors=errors)
        self.assertEqual([e[0] for e in errors], ["CUEUP.XML"])
        self.assertIn("kaputt", errors[0][1])
        self.assertEqual(n, 2)  # der Rest wurde trotzdem kopiert
        self.assertTrue((dst / "Clip" / "A BR01.BIM").is_file())
        with mock.patch.object(card, "copy_file_atomic", flaky), self.assertRaises(OSError):
            card.copy_structure(src, self.tmp / "out2" / "XDROOT")  # ohne errors-Liste: Fehler wird ausgeloest

    def test_copy_structure_cancel_is_not_swallowed(self):
        src = self.tmp / "card" / "XDROOT"
        self.make_card(src)

        class Stop(Exception):
            pass

        def chk():
            raise Stop()

        with self.assertRaises(Stop):
            card.copy_structure(src, self.tmp / "out" / "XDROOT", cancel_check=chk, errors=[])

    def test_clip_xml_stem(self):
        self.assertEqual(card.clip_xml_stem("PDW F5_0001M01.XML"), "PDW F5_0001")
        self.assertEqual(card.clip_xml_stem("a bm02.xml"), "a b")
        self.assertIsNone(card.clip_xml_stem("CUEUP.XML"))

    def test_write_bytes_atomic_and_stat(self):
        a = self.tmp / "a.xml"
        a.write_bytes(b"alt")
        import os
        os.utime(a, (1_000_000_000, 1_000_000_000))
        b = self.tmp / "b.xml"
        card.write_bytes_atomic(b, b"neu", stat_from=a)
        self.assertEqual(b.read_bytes(), b"neu")
        self.assertAlmostEqual(b.stat().st_mtime, 1_000_000_000, delta=2)
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.name.endswith(card.TMP_SUFFIX)], [])

    def test_write_bytes_atomic_failure_keeps_old_and_no_temp(self):
        from unittest import mock
        b = self.tmp / "b.xml"
        b.write_bytes(b"alt")
        with mock.patch("os.replace", side_effect=OSError("x")), self.assertRaises(OSError):
            card.write_bytes_atomic(b, b"neu")
        self.assertEqual(b.read_bytes(), b"alt")
        self.assertEqual([p.name for p in self.tmp.iterdir() if p.name.endswith(card.TMP_SUFFIX)], [])

    def test_remove_stray_temp_files(self):
        (self.tmp / "Clip").mkdir()
        (self.tmp / "Clip" / ("x.XML" + card.TMP_SUFFIX)).write_text("t")
        (self.tmp / "Clip" / "x.XML").write_text("k")
        self.assertEqual(card.remove_stray_temp_files(self.tmp), 1)
        self.assertTrue((self.tmp / "Clip" / "x.XML").exists())

    def test_list_clip_mxf(self):
        src = self.tmp / "card" / "XDROOT"
        self.make_card(src)
        self.assertEqual([p.name for p in card.list_clip_mxf(src)], ["A B.MXF"])


class ExistingTarget(unittest.TestCase):
    """G2: ein vorhandenes Ziel muss zur selben Karte gehoeren."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src = self.tmp / "card" / "XDROOT"
        (self.src / "Clip").mkdir(parents=True)
        (self.src / "MEDIAPRO.XML").write_bytes(fixture_bytes("f55_MEDIAPRO.XML"))
        (self.src / "Clip" / "PDW-F55_6753.MXF").write_bytes(b"x")
        self.mp = card.parse_mediapro(fixture_bytes("f55_MEDIAPRO.XML"))
        self.out = self.tmp / "out" / "XDROOT"

    def _make_target(self, mp_bytes=None, mxf=("PDW-F55_6753.MXF",)):
        (self.out / "Clip").mkdir(parents=True)
        if mp_bytes is not None:
            (self.out / "MEDIAPRO.XML").write_bytes(mp_bytes)
        for n in mxf:
            (self.out / "Clip" / n).write_bytes(b"y")

    def test_media_id_parsed(self):
        self.assertTrue(self.mp.media_id)

    def test_missing_or_empty_target_ok(self):
        card.check_existing_target(self.src, self.out, self.mp)
        (self.out / "Clip").mkdir(parents=True)
        card.check_existing_target(self.src, self.out, self.mp)

    def test_same_card_with_changed_video_type_ok(self):
        src = fixture_bytes("f55_MEDIAPRO.XML")
        self._make_target(src.replace(b"AVC100CBG_1920_1080_H422IP@L42", b"AVC50_1920_1080_H422P@L42"))
        card.check_existing_target(self.src, self.out, self.mp)

    def test_other_system_id(self):
        mp = fixture_bytes("f55_MEDIAPRO.XML")
        sid = self.mp.system_id.encode()
        self.assertTrue(sid and sid in mp)
        self._make_target(mp.replace(sid, b"FFFFFFFFFFFFFFFF"))
        with self.assertRaisesRegex(ValueError, "andere Karte"):
            card.check_existing_target(self.src, self.out, self.mp)

    def test_other_umid(self):
        mp = fixture_bytes("f55_MEDIAPRO.XML")
        umid = self.mp.materials[0].umid.encode()
        self._make_target(mp.replace(umid, b"0" * len(umid)))
        with self.assertRaisesRegex(ValueError, "andere Karte"):
            card.check_existing_target(self.src, self.out, self.mp)

    def test_other_media_id(self):
        mp = fixture_bytes("f55_MEDIAPRO.XML")
        self.assertTrue(self.mp.media_id.encode() in mp)
        self._make_target(mp.replace(self.mp.media_id.encode(), b"A" * len(self.mp.media_id)))
        with self.assertRaisesRegex(ValueError, "andere Karte"):
            card.check_existing_target(self.src, self.out, self.mp)

    def test_unreadable_mediapro(self):
        self._make_target(b"<kein xml")
        with self.assertRaisesRegex(ValueError, "nicht lesbar"):
            card.check_existing_target(self.src, self.out, self.mp)

    def test_mxf_not_from_source(self):
        self._make_target(fixture_bytes("f55_MEDIAPRO.XML"), mxf=("PDW-F55_6753.MXF", "FREMD.MXF"))
        with self.assertRaisesRegex(ValueError, "FREMD.MXF"):
            card.check_existing_target(self.src, self.out, self.mp)

    def test_xdroot_without_mediapro_but_with_mxf(self):
        self._make_target(None)
        with self.assertRaisesRegex(ValueError, "ohne MEDIAPRO.XML"):
            card.check_existing_target(self.src, self.out, self.mp)

    def test_xdroot_without_mediapro_with_foreign_file(self):
        (self.out / "Clip").mkdir(parents=True)
        (self.out / "Urlaub.doc").write_text("privat")
        with self.assertRaisesRegex(ValueError, "Urlaub.doc"):
            card.check_existing_target(self.src, self.out, self.mp)

    def test_xdroot_without_mediapro_only_own_partial_copy_ok(self):
        (self.out / "Clip").mkdir(parents=True)
        (self.src / "CUEUP.XML").write_text("c")
        (self.out / "CUEUP.XML").write_text("c")
        (self.out / ("x.XML" + card.TMP_SUFFIX)).write_text("t")
        card.check_existing_target(self.src, self.out, self.mp)


class WindowsRobustness(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_replace_retry_retries_permission_error(self):
        a, b = self.tmp / "a", self.tmp / "b"
        a.write_bytes(b"x")
        real, n = os.replace, [0]

        def flaky(s, d):
            n[0] += 1
            if n[0] < 4:
                raise PermissionError("Zugriff verweigert")
            return real(s, d)

        with mock.patch("os.replace", flaky), mock.patch.object(card.time, "sleep") as sl:
            card.replace_retry(a, b)
        self.assertEqual((n[0], sl.call_count), (4, 3))
        self.assertEqual(b.read_bytes(), b"x")

    def test_replace_retry_gives_up(self):
        a, b = self.tmp / "a", self.tmp / "b"
        a.write_bytes(b"x")
        with mock.patch("os.replace", side_effect=PermissionError("dauerhaft")), \
                mock.patch.object(card.time, "sleep") as sl:
            with self.assertRaises(PermissionError):
                card.replace_retry(a, b, tries=5, delay=0.1)
        self.assertEqual(sl.call_count, 4)

    def test_replace_retry_does_not_swallow_other_errors(self):
        with mock.patch("os.replace", side_effect=FileNotFoundError("weg")), mock.patch.object(card.time, "sleep") as sl:
            with self.assertRaises(FileNotFoundError):
                card.replace_retry(self.tmp / "a", self.tmp / "b")
        self.assertEqual(sl.call_count, 0)

    def test_atomic_writers_use_retry(self):
        calls = []
        real = card.replace_retry
        with mock.patch.object(card, "replace_retry", lambda a, b, *x, **k: (calls.append(1), real(a, b))[1]):
            card.write_bytes_atomic(self.tmp / "w.xml", b"1")
            (self.tmp / "s").write_bytes(b"2")
            card.copy_file_atomic(self.tmp / "s", self.tmp / "d")
        self.assertEqual(len(calls), 2)

    def test_copy_of_read_only_source_is_writable(self):
        s = self.tmp / "ro"
        s.write_bytes(b"abc")
        os.utime(s, (1_000_000_000, 1_000_000_000))
        os.chmod(s, 0o444)
        self.addCleanup(os.chmod, s, 0o644)
        d = self.tmp / "copy"
        card.copy_file_atomic(s, d)
        self.assertTrue(os.stat(d).st_mode & 0o200)
        self.assertEqual(int(os.stat(d).st_mtime), 1_000_000_000)  # Zeitstempel bleiben
        d2 = self.tmp / "written"
        card.write_bytes_atomic(d2, b"x", stat_from=s)
        self.assertTrue(os.stat(d2).st_mode & 0o200)
        self.assertEqual(int(os.stat(d2).st_mtime), 1_000_000_000)

    def test_copy_structure_read_only_dir_stays_usable(self):
        src, dst = self.tmp / "src", self.tmp / "dst"
        (src / "Sub").mkdir(parents=True)
        (src / "Sub" / "f.txt").write_text("1")
        os.chmod(src / "Sub", 0o555)
        self.addCleanup(os.chmod, src / "Sub", 0o755)
        card.copy_structure(src, dst)
        self.assertTrue(os.stat(dst / "Sub").st_mode & 0o200)
        (dst / "Sub" / "neu.txt").write_text("ok")  # Ziel-Ordner bleibt beschreibbar (Folgelauf)

    def test_make_writable_missing_file_is_silent(self):
        card.make_writable(self.tmp / "gibt-es-nicht")


class ForeignTargetHint(unittest.TestCase):
    def test_deleted_source_clip_message_has_hint(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        src, out = tmp / "src", tmp / "out"
        for r in (src, out):
            (r / "Clip").mkdir(parents=True)
        mp = card.parse_mediapro(fixture_bytes("f55_MEDIAPRO.XML"))
        (out / "MEDIAPRO.XML").write_bytes(fixture_bytes("f55_MEDIAPRO.XML"))
        (out / "Clip" / "ZZ.MXF").write_bytes(b"x")  # in der Quelle (geloescht) nicht mehr vorhanden
        with self.assertRaisesRegex(ValueError, "geloescht.*neuen, leeren Zielordner"):
            card.check_existing_target(src, out, mp)


if __name__ == "__main__":
    unittest.main()
