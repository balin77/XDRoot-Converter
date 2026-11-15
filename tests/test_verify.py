import copy
import json
import os
import shutil
import struct
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

from tests.helpers import fixture_bytes, fixture_text
from xdroot_converter import card, converter, mxfheader, mxfinfo, tools, verify


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


# --------------------------------------------------------------------------- Struktur (abgeschnittene Dateien)

_PK = bytes.fromhex("060e2b34020501010d01020101")
_EC = bytes.fromhex("060e2b34040101010d01030102060300")


def _pack(kind, status, this, prev, footer, hbc, ibc=0):
    v = struct.pack(">HHI", 1, 3, 1) + struct.pack(">QQQQQ", this, prev, footer, hbc, ibc)
    v += struct.pack(">IQI", 0, 0, 0) + bytes(16) + struct.pack(">II", 1, 16) + _EC
    return _PK + bytes([kind, status, 0]) + b"\x83" + len(v).to_bytes(3, "big") + v


def _mxf(meta=2000, body=300000, status=4, footer_ptr=True, rip=True, footer_status=4):
    """Header-Pack + Metadaten(Dummy) + Body(Dummy) + Footer-Pack + RIP."""
    hlen = len(_pack(2, 4, 0, 0, 0, meta))
    foff = hlen + meta + body
    out = _pack(2, status, 0, 0, foff if footer_ptr else 0, meta) + bytes(meta) + bytes(body)
    out += _pack(4, footer_status, foff, 0, foff, 0)
    if rip:
        rb = struct.pack(">IQ", 0, 0) + struct.pack(">IQ", 0, foff)
        out += bytes.fromhex("060e2b34020501010d01020101110100") + bytes([0x83]) + (len(rb) + 4).to_bytes(3, "big") + rb
        out += struct.pack(">I", 16 + 4 + len(rb) + 4)
    return out


class StructureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _check(self, data: bytes, **kw):
        p = self.tmp / "t.mxf"
        p.write_bytes(data)
        return verify.check_structure(p, **kw)

    def test_good_file(self):
        self.assertEqual(self._check(_mxf()), [])

    def test_halved_and_cut_files(self):
        good = _mxf()
        for n in (len(good) // 2, len(good) - 1, len(good) - 30, 100, 0):
            self.assertTrue(self._check(good[:n]), n)

    def test_trailing_garbage(self):
        self.assertTrue(self._check(_mxf() + b"\x00" * 64))

    def test_zero_filled_tail(self):
        good = _mxf()
        self.assertTrue(self._check(good[:len(good) // 2] + bytes(len(good) - len(good) // 2)))

    def test_open_header_and_missing_footer(self):
        self.assertIn("Header-Partition nicht geschlossen", "\n".join(self._check(_mxf(status=2))))
        self.assertIn("FooterPartition", "\n".join(self._check(_mxf(footer_ptr=False))))

    def test_footer_not_closed(self):
        self.assertIn("Footer-Partition nicht geschlossen", "\n".join(self._check(_mxf(footer_status=2))))

    def test_missing_rip(self):
        self.assertIn("RIP", "\n".join(self._check(_mxf(rip=False))))

    def test_rip_entry_wrong(self):
        data = bytearray(_mxf())
        # letzten RIP-Eintrag (Offset des Footers) um 1 verschieben
        i = len(data) - 4 - 8
        data[i:i + 8] = struct.pack(">Q", struct.unpack(">Q", data[i:i + 8])[0] + 1)
        self.assertIn("RIP", "\n".join(self._check(bytes(data))))

    def test_minimum_size(self):
        data = _mxf(body=5000)
        self.assertEqual(self._check(data, frames=1, n_audio=1, fps=Fraction(50)), [])
        msg = self._check(data, frames=100, n_audio=8, fps=Fraction(50))
        self.assertIn("zu klein", "\n".join(msg))
        # 29,97 fps: 1601 Samples/Frame (abgerundet)
        self.assertTrue(self._check(data, frames=1000, n_audio=2, fps=Fraction(30000, 1001)))


# --------------------------------------------------------------------------- H.264-Eigenschaften


class H264Props(unittest.TestCase):
    def _probe(self):
        return mxfinfo.parse_ffprobe(fixture_text("fx6_ffprobe.txt"))

    def test_reference_clean(self):
        self.assertEqual(verify.check_h264_props(self._probe(), 4, "4.2"), [])
        self.assertEqual(verify.check_h264_props(self._probe()), [])

    def test_each_property(self):
        cases = (("profile", "High 4:2:2 Intra", "H.264-Profil"), ("pix_fmt", "yuv420p", "pix_fmt"),
                 ("width", "1280", "Aufloesung"), ("color_range", "pc", "color_range"),
                 ("color_space", "bt601", "color_space"), ("color_transfer", "smpte170m", "color_transfer"),
                 ("color_primaries", "bt2020", "color_primaries"), ("level", "41", "Level"),
                 ("codec_name", "mpeg2video", "H.264-Profil"))
        for k, val, txt in cases:
            p = self._probe()
            p.video[k] = val
            self.assertIn(txt, "\n".join(verify.check_h264_props(p, 4, "4.2")), k)

    def test_audio(self):
        p = self._probe()
        self.assertIn("Audiospuren", "\n".join(verify.check_h264_props(p, 8)))
        p.audio[1]["codec_name"] = "pcm_s16le"
        self.assertIn("Audiostream 2", "\n".join(verify.check_h264_props(p, 4)))
        p = self._probe()
        p.audio[0]["sample_rate"] = "44100"
        self.assertIn("Audiostream 1", "\n".join(verify.check_h264_props(p, 4)))


# --------------------------------------------------------------------------- check_clip (Programme gemockt)


class CheckClipMocked(unittest.TestCase):
    def setUp(self):
        self.info = _info("fx6_mxf2raw.txt")
        self.probe = mxfinfo.parse_ffprobe(fixture_text("fx6_ffprobe.txt"))
        self.exp = verify.ClipExpect(
            frames=962, mp_uid=self.info.mp_uid, fp_uid=self.info.fp_uid, start_tc=self.info.start_tc,
            n_audio=4, fps=Fraction(50), expect_anc=True, expect_xml=True, level="4.2")

    def _run(self, structure=(), probe=None, **kw):
        with mock.patch.object(verify, "check_structure", return_value=list(structure)) as cs, \
                mock.patch.object(mxfinfo, "read_mxf_info", return_value=self.info), \
                mock.patch.object(mxfinfo, "run_ffprobe", return_value=probe or self.probe):
            out = verify.check_clip(Path("x.mxf"), self.exp, **kw)
        return out, cs

    def test_clean_and_structure_called_with_expectation(self):
        out, cs = self._run()
        self.assertEqual(out, [])
        cs.assert_called_once()
        self.assertEqual(cs.call_args.kwargs, {"frames": 962, "n_audio": 4, "fps": Fraction(50)})

    def test_structure_problem_stops_early(self):
        out, _ = self._run(structure=["Datei abgeschnitten"])
        self.assertEqual(out, ["Struktur: Datei abgeschnitten"])

    def test_h264_property_and_level_checked_on_resume(self):
        p = copy.deepcopy(self.probe)
        p.video["pix_fmt"] = "yuv420p"
        self.assertIn("pix_fmt", "\n".join(self._run(probe=p)[0]))
        self.exp.level = "4.1"
        self.assertIn("Level", "\n".join(self._run()[0]))
        self.exp.level = None  # nicht pruefen
        self.assertEqual(self._run()[0], [])

    def test_ffprobe_failure_reported(self):
        with mock.patch.object(verify, "check_structure", return_value=[]), \
                mock.patch.object(mxfinfo, "read_mxf_info", return_value=self.info), \
                mock.patch.object(mxfinfo, "run_ffprobe", side_effect=RuntimeError("kaputt")):
            out = verify.check_clip(Path("x.mxf"), self.exp)
        self.assertIn("ffprobe-Pruefung: kaputt", out)


# --------------------------------------------------------------------------- Decode am Clip-Ende


class TailDecode(unittest.TestCase):
    def _run(self, frames_line, stderr="", rc=0, frames=100):
        res = tools.ProcResult(rc, frames_line, stderr)

        def fake(args, cancel=None, on_stdout_line=None, **kw):
            fake.args = list(args)
            for l in res.stdout.splitlines():
                on_stdout_line(l)
            return res

        with mock.patch.object(tools, "find_tool", return_value="ffmpeg"), mock.patch.object(tools, "run_capture", fake):
            return verify.tail_decode_check("x.mxf", frames, Fraction(50)), fake.args

    def test_ok_and_seek_position(self):
        out, args = self._run("frame=10\nframe=25\nprogress=end")
        self.assertEqual(out, [])
        # 100 Frames @ 50 fps, 25 Test-Frames: halbes Frame vor Frame 75 = 1,49 s
        self.assertEqual(args[args.index("-ss") + 1], "1.490000")

    def test_short_clip_decodes_all(self):
        out, args = self._run("frame=10", frames=10)
        self.assertEqual(out, [])
        self.assertEqual(args[args.index("-ss") + 1], "0.000000")

    def test_no_frames_is_problem(self):
        out, _ = self._run("frame=0")
        self.assertIn("Clip-Ende", "\n".join(out))

    def test_errors_are_problem(self):
        out, _ = self._run("frame=25", stderr="[h264] error while decoding MB")
        self.assertIn("Decodierfehler", "\n".join(out))


# --------------------------------------------------------------------------- Ton-Inhalt


class AudioContent(unittest.TestCase):
    A, B, C = b"\x01" * 480, b"\x02" * 480, b"\x03" * 480

    def _run(self, src, tgt, **kw):
        """src/tgt: {Startframe: [Inhalt je Tonspur]}; gemockt wird nur das Extrahieren."""
        calls = []

        def fake(path, start, dur, directory, tag, cancel):
            calls.append((str(path), start, dur))
            data = (src if str(path) == "S" else tgt)[start]
            if isinstance(data, Exception):
                raise data
            return list(data)

        kw.setdefault("fps", Fraction(50))
        kw.setdefault("duration", 300)
        with mock.patch.object(verify, "_extract_audio", side_effect=fake):
            out = verify.check_audio_content("T", "S", **kw)
        self.calls = calls
        return out

    def test_identical_is_clean_and_reads_one_second(self):
        a = [self.A, self.B, self.C]
        self.assertEqual(self._run({0: a}, {0: a}), [])
        self.assertEqual(self.calls, [("S", 0, 50), ("T", 0, 50)])           # 1 s bei 50 fps, nur ein Fenster

    def test_swapped_tracks_detected_and_named(self):
        out = self._run({0: [self.A, self.B, self.C]}, {0: [self.B, self.A, self.C]})
        self.assertEqual(len(out), 2)
        self.assertIn("Tonspur 1: Inhalt weicht von der Quelle ab (Spuren vertauscht?)", out[0])
        self.assertIn("Quell-Tonspur 2", out[0])
        self.assertIn("Quell-Tonspur 1", out[1])

    def test_unknown_difference_without_match(self):
        out = self._run({0: [self.A, self.B]}, {0: [self.A, b"\x09" * 480]})
        self.assertEqual(len(out), 1)
        self.assertIn("Tonspur 2: Inhalt weicht von der Quelle ab (Spuren vertauscht?)", out[0])
        self.assertNotIn("entspricht", out[0])

    def test_shorter_target_compares_common_length(self):
        self.assertEqual(self._run({0: [self.A, self.B]}, {0: [self.A[:100], self.B[:100]]}), [])
        out = self._run({0: [self.A, self.B]}, {0: [self.A[:100], self.A[:100]]})
        self.assertEqual(len(out), 1)
        self.assertIn("Tonspur 2", out[0])

    def test_track_count_and_empty_data(self):
        self.assertIn("1 extrahiert statt 2", self._run({0: [self.A, self.B]}, {0: [self.A]})[0])
        out = self._run({0: [self.A]}, {0: [b""]})
        self.assertIn("keine Audiodaten", out[0])

    def test_silent_first_window_checks_later_windows(self):
        z = b"\x00" * 480
        src = {0: [z, z], 100: [self.A, self.B], 200: [self.A, self.B]}
        self.assertEqual(self._run(src, {0: [z, z], 100: [self.A, self.B], 200: [self.A, self.B]}), [])
        self.assertEqual([c[1] for c in self.calls if c[0] == "S"], [0, 100])    # 1/3 reicht: Spuren verschieden
        out = self._run(src, {0: [z, z], 100: [self.B, self.A], 200: [self.B, self.A]})
        self.assertEqual(len(out), 2)
        self.assertIn("(Fenster bei 1/3)", out[0])

    def test_later_window_beyond_short_target_is_skipped(self):
        z = b"\x00" * 480
        src = {0: [z, z], 100: [self.A, self.B], 200: [self.A, self.B]}
        tgt = {0: [z, z], 100: RuntimeError("Start position beyond"), 200: RuntimeError("beyond")}
        self.assertEqual(self._run(src, tgt), [])

    def test_extraction_failure_is_a_problem_and_cancel_propagates(self):
        out = self._run({0: RuntimeError("kaputt")}, {0: []})
        self.assertEqual(len(out), 1)
        self.assertIn("Tonspuren-Inhalt nicht pruefbar: kaputt", out[0])
        with mock.patch.object(verify, "_extract_audio", side_effect=tools.Cancelled()):
            with self.assertRaises(tools.Cancelled):
                verify.check_audio_content("T", "S", fps=Fraction(50), duration=100)

    def test_fps_and_duration_read_from_source_when_missing(self):
        info = mxfinfo.MxfInfo(edit_rate=Fraction(25), duration=100)
        with mock.patch.object(mxfinfo, "read_mxf_info", return_value=info) as rd:
            self._run({0: [self.A]}, {0: [self.A]}, fps=None, duration=None)
        rd.assert_called_once()
        self.assertEqual(self.calls[0], ("S", 0, 25))

    def test_extract_audio_runs_mxf2raw_limited_and_orders_tracks(self):
        seen = {}

        def fake_run(args, cancel=None, **kw):
            seen["args"] = [str(a) for a in args]
            prefix = seen["args"][seen["args"].index("--ess-out") + 1]
            for n in (10, 2, 0):                                     # numerisch sortieren, nicht alphabetisch
                Path(f"{prefix}_a{n}.raw").write_bytes(bytes([n]) * 6)
            Path(f"{prefix}_v0.raw").write_bytes(b"video")           # fremde Datei wird ignoriert
            return tools.ProcResult(returncode=0, stdout="", stderr="")

        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(tools, "run_capture", side_effect=fake_run), \
                mock.patch.object(tools, "find_tool", return_value="mxf2raw"):
            out = verify._extract_audio("quelle.mxf", 7, 50, td, "s", None)
            self.assertEqual(out, [b"\x00" * 6, b"\x02" * 6, b"\x0a" * 6])
            self.assertEqual(sorted(os.listdir(td)), ["s_v0.raw"])    # Audio-Rohdateien sind wieder entfernt
        a = seen["args"]
        for opt in ("--disable-video", "--disable-data"):
            self.assertIn(opt, a)
        self.assertEqual(a[a.index("--start") + 1], "7")
        self.assertEqual(a[a.index("--dur") + 1], "50")
        self.assertEqual(a[-1], "quelle.mxf")                         # Input-Optionen stehen vor dem Dateinamen
        self.assertLess(a.index("--start"), len(a) - 1)

    def test_temp_dir_is_removed(self):
        made = []
        real = tempfile.TemporaryDirectory

        def spy(*a, **k):
            d = real(*a, **k)
            made.append(d.name)
            return d

        with mock.patch.object(tempfile, "TemporaryDirectory", spy):
            self._run({0: [self.A]}, {0: [self.A]})
        self.assertEqual(len(made), 1)
        self.assertFalse(os.path.exists(made[0]))

    def test_check_clip_calls_it_only_with_source(self):
        info = _info("fx6_mxf2raw.txt")
        probe = mxfinfo.parse_ffprobe(fixture_text("fx6_ffprobe.txt"))
        exp = verify.ClipExpect(frames=962, mp_uid=info.mp_uid, fp_uid=info.fp_uid, start_tc=info.start_tc,
                                n_audio=4, fps=Fraction(50), expect_anc=True, expect_xml=True, level="4.2")
        with mock.patch.object(verify, "check_structure", return_value=[]), \
                mock.patch.object(mxfinfo, "read_mxf_info", return_value=info), \
                mock.patch.object(mxfinfo, "run_ffprobe", return_value=probe), \
                mock.patch.object(verify, "check_header", return_value=[]), \
                mock.patch.object(verify, "check_audio_content", return_value=["Tonspur 1: Inhalt weicht ab"]) as ac:
            self.assertEqual(verify.check_clip(Path("x.mxf"), exp), [])
            ac.assert_not_called()
            out = verify.check_clip(Path("x.mxf"), exp, source=Path("s.mxf"), source_info=info)
        self.assertEqual(out, ["Tonspur 1: Inhalt weicht ab"])
        ac.assert_called_once()
        self.assertEqual(ac.call_args.args, (Path("x.mxf"), Path("s.mxf")))


# --------------------------------------------------------------------------- Kopfdaten (Praefix, direkte Pruefung)


class CheckHeaderPrefix(unittest.TestCase):
    def test_no_double_prefix(self):
        with mock.patch.object(mxfheader, "compare_with_source", return_value=["Abweichung: Identification X"]), \
                mock.patch.object(verify, "check_header_direct", return_value=[]):
            out = verify.check_header(Path("a"), Path("b"), info=_info("f55_mxf2raw.txt"), src_info=_info("f55_mxf2raw.txt"))
        self.assertEqual(out, ["Identification X"])


class CheckHeaderDirect(unittest.TestCase):
    """Direkte Pruefung ueber read_header; baut die kleinen Testdateien mit den Helfern aus test_mxfheader."""

    def setUp(self):
        from tests import test_mxfheader as th
        self.th = th
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def _file(self, name, tracks, ident):
        p = self.tmp / name
        p.write_bytes(self.th.build_file(self.th.make_card(tracks, ident=ident).build(6000)))
        return p

    def test_identical_clean(self):
        a = self._file("a.mxf", self.th.SRC_TRACKS, self.th.SONY_IDENT)
        b = self._file("b.mxf", self.th.SRC_TRACKS, self.th.SONY_IDENT)
        self.assertEqual(verify.check_header_direct(a, b), [])

    def test_track_ids_and_identification_differ(self):
        src = self._file("s.mxf", self.th.SRC_TRACKS, self.th.SONY_IDENT)
        tgt = self._file("t.mxf", self.th.TGT_TRACKS, self.th.BMX_IDENT)
        text = "\n".join(verify.check_header_direct(tgt, src))
        self.assertIn("Track-IDs sound", text)
        self.assertIn("Track-IDs picture", text)
        self.assertIn("Identification CompanyName", text)
        self.assertIn("Identification ProductName", text)

    def test_dangling_references(self):
        src = self._file("s.mxf", self.th.SRC_TRACKS, self.th.SONY_IDENT)
        tgt = self._file("t.mxf", self.th.SRC_TRACKS, self.th.SONY_IDENT)

        def edit(hm):
            st = mxfheader.resolve(hm)
            clip = st.material.tracks[1].clips[0]
            hm.put(clip, "SourceClip.SourceTrackID", struct.pack(">I", 99))
            hm.put(st.file_descriptors()[0], "FileDescriptor.LinkedTrackID", struct.pack(">I", 77))
            return []

        mxfheader.patch_header(tgt, edit)
        text = "\n".join(verify.check_header_direct(tgt, src))
        self.assertIn("nicht vorhandenen Track 99", text)
        self.assertIn("LinkedTrackID 77", text)

    def test_extra_source_timecode_track_is_only_a_note(self):
        extra = self.th.SRC_TRACKS + [("timecode", 6, 0, None)]
        src = self._file("s.mxf", extra, self.th.SONY_IDENT)
        tgt = self._file("t.mxf", self.th.SRC_TRACKS, self.th.SONY_IDENT)
        notes: list = []
        self.assertEqual(verify.check_header_direct(tgt, src, notes=notes), [])
        self.assertEqual(len(notes), 2)                              # Material Package und File Package
        self.assertIn("zusaetzliche Timecode-Tracks", notes[0])
        self.assertEqual(verify.check_header_direct(tgt, src), [])    # notes ist optional

    def test_target_timecode_id_must_exist_in_source(self):
        src = self._file("s.mxf", self.th.SRC_TRACKS, self.th.SONY_IDENT)
        tc9 = [("timecode", 9, 0, None)] + self.th.SRC_TRACKS[1:]
        tgt = self._file("t.mxf", tc9, self.th.SONY_IDENT)
        text = "\n".join(verify.check_header_direct(tgt, src))
        self.assertIn("Track-IDs timecode [9] statt [1]", text)
        # Bild/Ton/Daten bleiben strikt gleich, auch wenn die Quelle mehr hat
        more = self.th.SRC_TRACKS + [("sound", 7, 0x16080302, None)]
        src2 = self._file("s2.mxf", more, self.th.SONY_IDENT)
        tgt2 = self._file("t2.mxf", self.th.SRC_TRACKS, self.th.SONY_IDENT)
        self.assertIn("Track-IDs sound", "\n".join(verify.check_header_direct(tgt2, src2)))

    def test_first_identification_is_compared(self):
        th = self.th
        s = th.make_card(th.SRC_TRACKS, ident=th.SONY_IDENT)
        ident2 = s.add("Identification", [("Identification.ThisGenerationUID", b"\x55" * 16),
                                          ("Identification.ModificationDate", bytes.fromhex("07ea0b0f11000000")),
                                          ("Identification.CompanyName", th.u16s("Adobe")),
                                          ("Identification.ProductName", th.u16s("Premiere"))])
        out = []
        for n, items, u, k in s.sets:
            if n == "Preface":
                first = dict(items)["Preface.Identifications"][8:24]
                items = [(r, th.batch([first, ident2]) if r == "Preface.Identifications" else v) for r, v in items]
            out.append((n, items, u, k))
        s.sets = out
        src = self.tmp / "s.mxf"
        src.write_bytes(th.build_file(s.build(6000)))
        tgt = self._file("t.mxf", th.SRC_TRACKS, th.SONY_IDENT)      # traegt die Werte der ersten Identification
        self.assertEqual(verify.check_header_direct(tgt, src), [])

    def test_missing_anc_in_target_not_reported_here(self):
        src = self._file("s.mxf", self.th.SRC_TRACKS, self.th.SONY_IDENT)
        tgt = self._file("t.mxf", self.th.SRC_TRACKS[:-1], self.th.SONY_IDENT)
        text = "\n".join(verify.check_header_direct(tgt, src))
        self.assertNotIn("data", text)


# --------------------------------------------------------------------------- Karte pruefen (Programme gemockt)

_MP_EMPTY = (b'<?xml version="1.0"?><MediaProfile xmlns="http://xmlns.sony.net/pro/metadata/mediaprofile">'
             b'<Contents/></MediaProfile>')


class VerifyCardMocked(unittest.TestCase):
    """Eine Ziel-Karte mit einem (Dummy-)XAVC-L-Clip; mxf2raw/ffprobe/ffmpeg gemockt."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "XDROOT"
        (self.root / "Clip").mkdir(parents=True)
        (self.root / "MEDIAPRO.XML").write_bytes(_MP_EMPTY)
        (self.root / "Clip" / "FX6ENG10562.MXF").write_bytes(b"x")
        self.xml = fixture_bytes("fx6_M01.XML")
        (self.root / "Clip" / "FX6ENG10562M01.XML").write_bytes(self.xml)
        self.info = _info("fx6_mxf2raw.txt")
        self.probe = mxfinfo.parse_ffprobe(fixture_text("fx6_ffprobe.txt"))

    def _verify(self, **kw):
        with mock.patch.object(mxfinfo, "read_mxf_info", return_value=self.info), \
                mock.patch.object(mxfinfo, "run_ffprobe", return_value=self.probe), \
                mock.patch.object(verify, "check_structure", return_value=[]), \
                mock.patch.object(verify, "measure_gop", return_value=[24, 24]), \
                mock.patch.object(verify, "tail_decode_check", return_value=kw.pop("tail", [])) as tail:
            rep = verify.verify_card(str(self.root), **kw)
        self.tail = tail
        return rep

    def test_clean(self):
        rep = self._verify()
        self.assertEqual(rep.problems, [])
        self.tail.assert_called_once()

    def test_deep_skips_tail_check(self):
        with mock.patch.object(verify, "deep_decode_check", return_value=(962, [])):
            rep = self._verify(deep=True)
        self.assertEqual(rep.problems, [])
        self.tail.assert_not_called()

    def test_tail_problem_is_deviation(self):
        rep = self._verify(tail=["Clip-Ende: nur 0 von 25 erwarteten Frames dekodierbar"])
        self.assertEqual(len(rep.problems), 1)
        self.assertIn("Clip-Ende", rep.problems[0])

    def test_k8_xavci_m01_next_to_xavcl_mxf_unlisted(self):
        # Clip steht nicht in MEDIAPRO.XML, M01.XML nennt aber XAVC-I
        bad = self.xml.replace(b"AVC50_1920_1080_H422P@L42", b"AVC100CBG_1920_1080_H422IP@L42")
        self.assertNotEqual(bad, self.xml)
        (self.root / "Clip" / "FX6ENG10562M01.XML").write_bytes(bad)
        rep = self._verify()
        self.assertTrue(any("M01.XML videoCodec" in p and "passt nicht" in p for p in rep.problems), rep.problems)

    def test_structure_problem_reported(self):
        with mock.patch.object(mxfinfo, "read_mxf_info", return_value=self.info), \
                mock.patch.object(mxfinfo, "run_ffprobe", return_value=self.probe), \
                mock.patch.object(verify, "check_structure", return_value=["Datei abgeschnitten"]), \
                mock.patch.object(verify, "measure_gop", return_value=[24]), \
                mock.patch.object(verify, "tail_decode_check", return_value=[]):
            rep = verify.verify_card(str(self.root))
        self.assertTrue(any("Struktur: Datei abgeschnitten" in p for p in rep.problems))

    def test_anc_missing_and_manifest_mismatch(self):
        src_root = self.tmp / "SRC" / "XDROOT"
        (src_root / "Clip").mkdir(parents=True)
        (src_root / "MEDIAPRO.XML").write_bytes(_MP_EMPTY)
        (src_root / "Clip" / "FX6ENG10562.MXF").write_bytes(b"y")
        (src_root / "Clip" / "FX6ENG10562M01.XML").write_bytes(self.xml)
        src_info = copy.deepcopy(self.info)

        def run(tgt_info):
            self.info = tgt_info
            with mock.patch.object(mxfinfo, "read_mxf_info", side_effect=lambda p, cancel=None: tgt_info if "SRC" not in p else src_info), \
                    mock.patch.object(mxfinfo, "run_ffprobe", return_value=self.probe), \
                    mock.patch.object(verify, "check_structure", return_value=[]), \
                    mock.patch.object(verify, "measure_gop", return_value=[24]), \
                    mock.patch.object(verify, "tail_decode_check", return_value=[]), \
                    mock.patch.object(verify, "check_audio_content", return_value=[]) as audio, \
                    mock.patch.object(verify, "check_header", return_value=[]):
                rep = verify.verify_card(str(self.root), source=str(src_root))
            self.audio = audio
            return rep

        no_anc = copy.deepcopy(self.info)
        no_anc.tracks = [t for t in no_anc.tracks if t.essence_type != "ANC_Data"]
        probs = "\n".join(run(no_anc).problems)
        self.assertIn("ANC-Spur: Ziel nein, Quelle ja (gewollt bei --no-anc)", probs)
        self.assertIn("nicht, dass ANC bewusst weggelassen", probs)      # keine Angabe in der Einstellungsdatei
        self.audio.assert_called_once()                                  # Ton-Inhalt je umgewandeltem Clip
        # Einstellungsdatei im Zielordner: ANC bewusst weggelassen -> nur Hinweis
        settings = self.tmp / "konvertierung_einstellungen.json"
        for entry in ({"keep_anc": False}, {"keep_anc": True, "anc_failed": True}):
            settings.write_text(json.dumps({"version": 1, "clips": {"Clip/FX6ENG10562.MXF": dict(entry, neues_feld=7)}}),
                                encoding="utf-8")
            rep = run(no_anc)
            self.assertEqual(rep.problems, [], entry)
            self.assertTrue(any("ANC-Spur fehlt im Ziel" in n for n in rep.notes), rep.notes)
        # keep_anc true ohne Fehlerfeld, anderer Clip, kaputte Datei: weiterhin Abweichung
        for content in (json.dumps({"clips": {"Clip/FX6ENG10562.MXF": {"keep_anc": True}}}),
                        json.dumps({"clips": {"Clip/ANDERER.MXF": {"keep_anc": False}}}),
                        "{kaputt", json.dumps([1, 2]), json.dumps({"clips": {"Clip/FX6ENG10562.MXF": 5}})):
            settings.write_text(content, encoding="utf-8")
            self.assertTrue(any("ANC-Spur: Ziel nein" in p for p in run(no_anc).problems), content)
        settings.unlink()
        # ANC im Ziel vorhanden, in der Quelle nicht: nie durch die Einstellungsdatei entschuldigt
        settings.write_text(json.dumps({"clips": {"Clip/FX6ENG10562.MXF": {"keep_anc": False}}}), encoding="utf-8")
        src_without = copy.deepcopy(src_info)
        src_without.tracks = [t for t in src_without.tracks if t.essence_type != "ANC_Data"]
        saved, src_info = src_info, src_without
        self.assertTrue(any("ANC-Spur: Ziel ja, Quelle nein" in p for p in run(_info("fx6_mxf2raw.txt")).problems))
        src_info = saved
        settings.unlink()
        fewer = copy.deepcopy(src_info)
        fewer.anc_tracks[0].anc_elements = fewer.anc_tracks[0].anc_elements[:-1]
        probs = "\n".join(run(fewer).problems)
        self.assertIn("ANC-Manifest", probs)


# --------------------------------------------------------------------------- Nicht-MXF-Dateien fehlen im Ziel


class CompareFilesMissing(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.src, self.tgt = self.tmp / "s", self.tmp / "t"
        for d in (self.src, self.tgt):
            for sub in ("Clip", "Take", "Thmbnl"):
                (d / sub).mkdir(parents=True)
        files = ["DISCMETA.XML", "CUEUP.XML", "Take/A_0001U01.SMI", "Thmbnl/A_0001T01.JPG", "Thmbnl/A_0002T01.JPG",
                 "Clip/A_0001R01.BIM", "Clip/A_0002R01.BIM", "Clip/A_0001.MXF", "Clip/A_0002.MXF"]
        for rel in files:
            (self.src / rel).write_bytes(b"data " + rel.encode())
            if not rel.startswith("Clip/A_0002") or rel.endswith(".BIM"):
                (self.tgt / rel).write_bytes(b"data " + rel.encode())

    def _run(self):
        rep = verify.VerifyReport()
        verify._compare_files(self.src, self.tgt, card.MediaPro(), card.MediaPro(), rep)
        return rep

    def test_all_present_is_clean(self):
        (self.tgt / "Clip" / "A_0002.MXF").write_bytes(b"x")
        self.assertEqual(self._run().problems, [])

    def test_missing_files_are_deviations(self):
        (self.tgt / "Clip" / "A_0002.MXF").write_bytes(b"x")
        for rel in ("DISCMETA.XML", "CUEUP.XML", "Take/A_0001U01.SMI", "Thmbnl/A_0001T01.JPG", "Clip/A_0002R01.BIM"):
            (self.tgt / rel).unlink()
        probs = "\n".join(self._run().problems)
        for rel in ("DISCMETA.XML", "CUEUP.XML", "Take/A_0001U01.SMI", "Thmbnl/A_0001T01.JPG", "Clip/A_0002R01.BIM"):
            self.assertIn(rel, probs)

    def test_skipped_clip_companions_only_a_note(self):
        # Clip A_0002 fehlt im Ziel (--only / --unlisted skip): MXF, BIM und Thumbnail fehlen bewusst
        (self.tgt / "Clip" / "A_0002R01.BIM").unlink()
        (self.tgt / "Thmbnl" / "A_0002T01.JPG").unlink()
        rep = self._run()
        self.assertEqual(rep.problems, [])
        self.assertTrue(any("bewusst" in n and "1 Quell-Clip" in n for n in rep.notes), rep.notes)

    def test_skipped_clip_does_not_excuse_other_files(self):
        (self.tgt / "Clip" / "A_0002R01.BIM").unlink()
        (self.tgt / "Take" / "A_0001U01.SMI").unlink()
        (self.tgt / "Thmbnl" / "A_0001T01.JPG").unlink()
        probs = self._run().problems
        self.assertEqual(len(probs), 2)  # A_0001 ist vorhanden -> seine fehlenden Dateien sind Abweichungen


if __name__ == "__main__":
    unittest.main()
