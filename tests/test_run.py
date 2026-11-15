"""Regressionstests fuer den Ablauf (converter.Converter.run) mit Fake-Karte.

Externe Programme (ffmpeg, raw2bmx, mxf2raw, ffprobe) sowie mxfinfo/verify/mxfheader werden per
unittest.mock ersetzt; es werden keine echten Medien gebraucht.  Die "MXF-Dateien" sind kleine
Textdateien:  ``SRC|NAME`` (XAVC-I-Quelle), ``DST|NAME|L`` (fertiger XAVC-L-Clip),
``DST|NAME|L|FREMD`` (XAVC-L mit anderer UMID), ``DST|NAME|TRUNC`` (abgeschnitten),
``OTHER|NAME`` (anderer Codec, wird nur kopiert).
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

from xdroot_converter import card, converter, mxfheader, mxfinfo, tools, verify

REPO = Path(__file__).resolve().parent.parent
CODEC_I = "AVC100CBG_1920_1080_H422IP@L42"
CODEC_L = "AVC50_1920_1080_H422P@L42"
FRAMES = 10


def uid(name: str, salt: str = "") -> str:
    return hashlib.sha256((name + salt).encode()).hexdigest().upper()


def mediapro_xml(names, system_id="SYS1", media_id="MEDIA1", salt="") -> bytes:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>\r\n',
        '<MediaProfile xmlns="http://xmlns.sony.net/pro/metadata/mediaprofile" version="2.10">\r\n',
        f'<Properties><System systemId="{system_id}" systemKind="PMW-F55"/>'
        f'<Attached mediaId="{media_id}" mediaKind="ProfessionalDisc"/></Properties>\r\n<Contents>\r\n',
    ]
    for n in names:
        lines.append(
            f'<Material uri="./Clip/{n}.MXF" type="MXF" offset="0" dur="{FRAMES}" fps="50p" ch="2"\r\n'
            f'  videoType="{CODEC_I}" audioType="LPCM24" umid="{uid(n, salt)}" status="none">\r\n'
            f'<RelevantInfo uri="./Clip/{n}M01.XML" type="XML"/></Material>\r\n')
    lines.append("</Contents></MediaProfile>\r\n")
    return "".join(lines).encode()


def clip_xml(name, codec=CODEC_I, salt="") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\r\n<NonRealTimeMeta xmlns="urn:schemas-professionalDisc:nonRealTimeMeta:ver.2.00">\r\n'
        f'<TargetMaterial umidRef="{uid(name, salt)}"/>\r\n<Duration value="{FRAMES}"/>\r\n'
        f'<VideoFormat><VideoFrame videoCodec="{codec}" captureFps="50.00p"/></VideoFormat>\r\n'
        '<AudioFormat numOfChannel="2"/>\r\n</NonRealTimeMeta>\r\n').encode()


def make_card(root: Path, names=("A", "B"), unlisted=(), salt="", system_id="SYS1", media_id="MEDIA1", no_xml=()) -> Path:
    xr = root / "XDROOT"
    for d in ("Clip", "Take", "Sub", "General", "Thmbnl"):
        (xr / d).mkdir(parents=True)
    (xr / "MEDIAPRO.XML").write_bytes(mediapro_xml(names, system_id, media_id, salt))
    (xr / "CUEUP.XML").write_bytes(b"<CueUp/>")
    for n in list(names) + list(unlisted):
        (xr / "Clip" / f"{n}.MXF").write_bytes(f"SRC|{n}".encode() + b"." * 50)
        if n not in no_xml:
            (xr / "Clip" / f"{n}M01.XML").write_bytes(clip_xml(n, salt=salt))
        (xr / "Thmbnl" / f"{n}T01.JPG").write_bytes(b"jpg")
    return xr


def _mxf_info(path, cancel=None):
    data = Path(path).read_bytes()
    parts = data.decode("latin1").split("|")
    if len(parts) < 2 or parts[0] not in ("SRC", "DST", "OTHER"):
        raise RuntimeError("MXF nicht lesbar")
    name = parts[1].split(".")[0]
    info = mxfinfo.MxfInfo(path=str(path))
    info.mp_uid = uid(name)
    info.fp_uid = uid(name, "fp")
    info.duration = FRAMES
    info.edit_rate = Fraction(50)
    info.start_tc = "00:00:00:00"
    if parts[0] == "DST" and len(parts) > 3 and parts[3].startswith("FREMD"):
        info.mp_uid = uid(name, "fremd")
    if parts[0] == "DST" and len(parts) > 2 and parts[2] == "TRUNC":
        raise RuntimeError("abgeschnitten")
    et = {"SRC": "AVCI_100_1080p", "DST": "AVC_High_422", "OTHER": "MPEG2_Long"}[parts[0]]
    info.tracks = [mxfinfo.MxfTrack(index=0, kind="Picture", essence_type=et)] + [
        mxfinfo.MxfTrack(index=i, kind="Sound", essence_type="PCM") for i in (1, 2)]
    return info


class Fakes:
    """Ersetzt alle externen Abhaengigkeiten des Ablaufs."""

    def __init__(self):
        self.transcodes: list = []
        self.wraps: list = []
        self.wrap_args: list = []
        self.fail_wrap: set = set()
        self.checks: list = []  # (Dateiname, mit_source)
        self.header_problems: list = []
        self.adopt_msgs: list = []
        self.adopts: list = []
        self.after_adopt_problems: list = []
        self.seq_offs = [0, 0]
        self.probe_level = "42"
        self.crash_in_finish: set = set()  # Clip-Namen: os._exit direkt nach dem Ersetzen der MXF (harter Abbruch)
        self.patches: list = []
        self.free = 10 ** 12

    def __enter__(self):
        fx = self

        def probe(path, cancel=None, extra=None):
            return mxfinfo.ProbeResult(streams=[
                {"codec_type": "video", "codec_name": "h264", "profile": "High 4:2:2 Intra", "width": "1920",
                 "height": "1080", "r_frame_rate": "50/1", "level": fx.probe_level},
                {"codec_type": "audio"}, {"codec_type": "audio"}])

        def decide(info, pr):
            if info.video is not None and info.video.essence_type.startswith("AVCI"):
                return mxfinfo.SourceDecision(True, "XAVC-Intra 1080 progressiv", Fraction(50))
            return mxfinfo.SourceDecision(False, "Codec mpeg2")

        def transcode(conv, plan, fps, frames, v_path, wavs):
            fx.transcodes.append(plan.name)
            v_path.write_bytes(b"v")
            for w in wavs:
                w.write_bytes(b"w")
            conv._emit_progress(plan, 0.9, "t", transcoded=frames)

        def wrap(conv, plan, fps, frames, v_path, wavs, out, anc, xml):
            fx.wraps.append(plan.name)
            if plan.stem in fx.fail_wrap:
                return "raw2bmx kaputt"
            out.write_bytes(f"DST|{plan.stem}|L".encode())
            return ""

        def check_clip(path, exp, cancel=None, source=None, source_info=None):
            fx.checks.append((Path(path).name, source is not None))
            try:
                info = _mxf_info(path)
            except RuntimeError as e:
                return [f"MXF nicht lesbar: {e}"]
            probs = []
            if info.video.essence_type != "AVC_High_422":
                probs.append("essence_type falsch")
            if info.mp_uid != exp.mp_uid:
                probs.append("Material-Package-UMID falsch")
            if source is not None and fx.after_adopt_problems:
                probs += fx.after_adopt_problems
                fx.after_adopt_problems = []  # einmalig
            return probs

        def check_header(path, source, cancel=None, info=None, src_info=None):
            return list(fx.header_problems)

        def adopt(target, source):
            fx.adopts.append(Path(target).name)
            fx.header_problems = []
            return list(fx.adopt_msgs)

        def finish(conv, plan, dst_dir, _orig=converter.Converter._finish_clip_files):
            if plan.stem in fx.crash_in_finish:
                os._exit(9)  # wie kill -9: MXF ist ersetzt, XML/MEDIAPRO noch nicht
            return _orig(conv, plan, dst_dir)

        self._ps = [
            mock.patch.object(tools, "check_tools", lambda: {}),
            mock.patch.object(converter, "tool_versions", lambda: {"ffmpeg": "ffmpeg 1", "raw2bmx": "raw2bmx 1"}),
            mock.patch.object(mxfinfo, "read_mxf_info", _mxf_info),
            mock.patch.object(mxfinfo, "run_ffprobe", probe),
            mock.patch.object(mxfinfo, "decide_source", decide),
            mock.patch.object(converter, "read_seq_offsets", lambda p: list(fx.seq_offs)),
            mock.patch.object(converter.Converter, "_transcode", transcode),
            mock.patch.object(converter.Converter, "_wrap", wrap),
            mock.patch.object(converter.Converter, "_extract_anc", lambda *a, **k: None),
            mock.patch.object(converter.Converter, "_finish_clip_files", finish),
            mock.patch.object(verify, "check_clip", check_clip),
            mock.patch.object(verify, "check_header", check_header),
            mock.patch.object(mxfheader, "adopt_from_source", adopt),
            mock.patch.object(converter.shutil, "disk_usage",
                              lambda p: shutil._ntuple_diskusage(10 ** 13, 10 ** 13 - fx.free, fx.free)),
        ]
        for p in self._ps:
            p.start()
        return self

    def __exit__(self, *exc):
        for p in reversed(self._ps):
            p.stop()


class Rep(converter.Reporter):
    def __init__(self):
        self.lines: list = []
        self.progress_calls: list = []

    def log(self, level, msg):
        self.lines.append((level, msg))

    def progress(self, clip_name, clip_frac, total_frac, eta_s, text=""):
        self.progress_calls.append((clip_name, clip_frac, total_frac, eta_s, text))

    def text(self, level=None):
        return "\n".join(m for l, m in self.lines if level is None or l == level)


class RunTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.fx = Fakes().__enter__()
        self.addCleanup(self.fx.__exit__)
        self.src = make_card(self.tmp / "card")
        self.tgt = self.tmp / "out"

    def run_conv(self, source=None, target=None, **kw):
        opts = converter.Options(source=str(source or self.src), target=str(target or self.tgt), **kw)
        rep = Rep()
        conv = converter.Converter(opts, rep)
        summary = conv.run()
        return summary, rep, conv

    def out(self, name) -> Path:
        return self.tgt / "XDROOT" / name

    def codecs(self):
        """(MEDIAPRO-videoTypes, M01.XML-videoCodecs) im Ziel."""
        mp = card.parse_mediapro(self.out("MEDIAPRO.XML").read_bytes())
        xmls = {}
        for p in sorted((self.tgt / "XDROOT" / "Clip").glob("*M01.XML")):
            xmls[p.name] = card.parse_clip_xml(p.read_bytes()).video_codec
        return {m.rel_path: m.video_type for m in mp.materials}, xmls

    def assert_consistent(self, names=("A", "B")):
        mpc, xmls = self.codecs()
        for n in names:
            exists = self.out(f"Clip/{n}.MXF").exists()
            is_l = exists and self.out(f"Clip/{n}.MXF").read_bytes().startswith(b"DST|") and b"|L" in self.out(f"Clip/{n}.MXF").read_bytes()
            want = CODEC_L if is_l else CODEC_I
            self.assertEqual(mpc[f"Clip/{n}.MXF"], want, f"MEDIAPRO {n}")
            self.assertEqual(xmls.get(f"{n}M01.XML"), want, f"M01.XML {n}")


class G1_FollowUpRuns(RunTestBase):
    def test_full_run_consistent(self):
        s, rep, _ = self.run_conv()
        self.assertEqual((s.count(converter.ST_CONVERTED), s.failed, s.fatal), (2, 0, ""))
        self.assert_consistent()
        self.assertEqual(self.codecs()[0]["Clip/A.MXF"], CODEC_L)

    def test_followup_with_only_keeps_finished_clips(self):
        """Vorher: MEDIAPRO.XML wurde aus der Quelle neu geschrieben -> A stand wieder auf XAVC-I."""
        self.run_conv()
        s, rep, _ = self.run_conv(only=["B"])
        self.assertEqual(s.count(converter.ST_SKIPPED_DONE), 1)
        self.assert_consistent()
        self.assertEqual(self.codecs()[0]["Clip/A.MXF"], CODEC_L)

    def test_followup_after_new_run_does_not_reset_xml(self):
        """Vorher: copy_structure kopierte die Quell-M01.XML ueber die angepasste."""
        self.run_conv()
        with mock.patch.object(converter.Converter, "_process_clip", side_effect=RuntimeError("stoppt")):
            s, _, _ = self.run_conv()
        self.assertEqual(s.failed, 2)
        self.assert_consistent()

    def test_fatal_before_writing_leaves_target_untouched(self):
        """Vorher: Fatale Fehler (z. B. zu wenig Speicher) schrieben MEDIAPRO.XML aus der Quelle -> alles XAVC-I."""
        self.run_conv()
        (self.src / "Clip" / "C.MXF").write_bytes(b"SRC|C" + b"." * 50)  # offener Clip im Folgelauf
        before = {p: p.read_bytes() for p in self.tgt.rglob("*") if p.is_file()}
        self.fx.free = 1000
        s, _, _ = self.run_conv()
        self.assertIn("Zu wenig freier Speicher", s.fatal)
        after = {p: p.read_bytes() for p in self.tgt.rglob("*") if p.is_file() and p.name != converter.LOCK_NAME}
        # nur das Log darf gewachsen sein
        for p, b in before.items():
            if p.name == converter.LOG_NAME:
                continue
            self.assertEqual(after.get(p), b, p.name)
        self.assert_consistent()

    def test_cancel_keeps_finished_and_next_run_completes(self):
        cancel = tools.CancelToken()
        orig = converter.Converter._finish_clip_files

        def finish_then_cancel(conv, plan, dst_dir):
            orig(conv, plan, dst_dir)
            cancel.cancel()

        opts = converter.Options(source=str(self.src), target=str(self.tgt))
        with mock.patch.object(converter.Converter, "_finish_clip_files", finish_then_cancel):
            s = converter.Converter(opts, Rep(), cancel).run()
        self.assertTrue(s.cancelled)
        mpc, xmls = self.codecs()
        self.assertEqual(mpc["Clip/A.MXF"], CODEC_L)  # A fertig und eingetragen
        self.assertEqual(mpc["Clip/B.MXF"], CODEC_I)
        self.assert_consistent()
        s2, _, _ = self.run_conv()
        self.assertEqual(s2.count(converter.ST_SKIPPED_DONE), 1)
        self.assert_consistent()

    def _child(self, crash_for, only=None):
        code = (
            "import sys; from tests import test_run as t; "
            f"t.child_main({str(self.src)!r}, {str(self.tgt)!r}, {sorted(crash_for)!r}, {only!r})"
        )
        return subprocess.run([sys.executable, "-c", code], cwd=str(REPO), capture_output=True, text=True, timeout=120)

    def test_hard_kill_after_mxf_replace_is_healed(self):
        """kill -9 direkt nach dem Ersetzen der MXF: Zwischenzustand 'MXF XAVC-L, XML alt' fuer EINEN Clip;
        der naechste Lauf heilt ihn - auch wenn er den Clip gar nicht auswaehlt (--only)."""
        r = self._child({"B"})
        self.assertEqual(r.returncode, 9, r.stderr)
        # A war fertig und eingetragen, B: MXF ersetzt, XML/MEDIAPRO noch alt
        mpc, xmls = self.codecs()
        self.assertEqual((mpc["Clip/A.MXF"], xmls["AM01.XML"]), (CODEC_L, CODEC_L))
        self.assertTrue(self.out("Clip/B.MXF").read_bytes().startswith(b"DST|B"))
        self.assertEqual((mpc["Clip/B.MXF"], xmls["BM01.XML"]), (CODEC_I, CODEC_I))
        self.assertTrue((self.tgt / converter.LOCK_NAME).exists())  # verwaiste Sperre
        s, _, _ = self.run_conv(only=["A"])  # B wird nicht ausgewaehlt und trotzdem geheilt
        self.assertEqual(s.fatal, "")
        self.assert_consistent()
        self.assertFalse((self.tgt / converter.LOCK_NAME).exists())

    def test_hard_kill_before_any_clip_then_only_run(self):
        r = self._child({"A"})
        self.assertEqual(r.returncode, 9, r.stderr)
        # MEDIAPRO.XML existiert schon zu Beginn (aus Ist-Zustand), nicht erst am Ende
        self.assertTrue(self.out("MEDIAPRO.XML").is_file())
        s, _, _ = self.run_conv(only=["B"])
        self.assertEqual(s.fatal, "")
        self.assert_consistent()

    def test_xml_writes_are_atomic(self):
        self.run_conv()
        real = os.replace
        seen = []

        def spy(a, b):
            seen.append((Path(a).name, Path(b).name))
            return real(a, b)

        with mock.patch("os.replace", spy):
            self.run_conv(only=["A"], bitrate_mbps=40)  # anderer Lauf: schreibt Einstellungen/MEDIAPRO nicht neu
        # kein Temp-Rest
        self.assertEqual([p for p in self.tgt.rglob("*") if p.name.endswith(card.TMP_SUFFIX)], [])


def child_main(src, tgt, crash_for, only):
    """Eigener Prozess fuer harte Abbrueche (os._exit)."""
    with Fakes() as fx:
        fx.crash_in_finish = set(crash_for)
        opts = converter.Options(source=src, target=tgt, only=only or [])
        converter.Converter(opts, Rep()).run()


class G2_ForeignTarget(RunTestBase):
    def test_other_card_in_target_aborts_before_any_change(self):
        self.run_conv()
        other = make_card(self.tmp / "card2", salt="X", system_id="SYS2", media_id="MEDIA2")
        before = {p: p.read_bytes() for p in self.tgt.rglob("*") if p.is_file()}
        s, rep, _ = self.run_conv(source=other)
        self.assertIn("andere Karte", s.fatal)
        self.assertEqual(self.fx.transcodes, ["A.MXF", "B.MXF"])  # nur vom ersten Lauf
        after = {p: p.read_bytes() for p in self.tgt.rglob("*") if p.is_file()}
        self.assertEqual(before, after)  # weder Dateien geaendert noch Log/Sperre zurueckgeblieben

    def test_same_names_other_umids_in_mediapro_is_foreign(self):
        self.run_conv()
        other = make_card(self.tmp / "card2", salt="X")  # gleiche systemId/mediaId, andere UMIDs
        s, _, _ = self.run_conv(source=other)
        self.assertIn("andere Karte", s.fatal)
        self.assertTrue(self.out("Clip/A.MXF").read_bytes().startswith(b"DST|A|L"))

    def test_xdroot_without_mediapro_but_mxf_aborts(self):
        (self.tgt / "XDROOT" / "Clip").mkdir(parents=True)
        (self.tgt / "XDROOT" / "Clip" / "A.MXF").write_bytes(b"privat")
        s, _, _ = self.run_conv()
        self.assertIn("ohne MEDIAPRO.XML", s.fatal)
        self.assertEqual((self.tgt / "XDROOT" / "Clip" / "A.MXF").read_bytes(), b"privat")

    def test_mxf_with_foreign_umid_is_never_deleted(self):
        """Vorher: dst.unlink() bei jeder Abweichung."""
        self.run_conv()
        foreign = b"DST|A|L|FREMD"
        self.out("Clip/A.MXF").write_bytes(foreign)
        s, rep, _ = self.run_conv()
        res = {r.name: r for r in s.results}
        self.assertEqual(res["A.MXF"].status, converter.ST_FAILED)
        self.assertIn("andere Datei", " ".join(res["A.MXF"].notes))
        self.assertEqual(self.out("Clip/A.MXF").read_bytes(), foreign)
        self.assertEqual(res["B.MXF"].status, converter.ST_SKIPPED_DONE)
        self.assertGreater(s.failed, 0)

    def test_differing_target_is_replaced_not_deleted_first(self):
        """Eine abweichende (hier: nicht lesbare/abgeschnittene) Ziel-MXF bleibt, bis die neue fertig ist."""
        self.run_conv()
        self.out("Clip/A.MXF").write_bytes(b"DST|A|TRUNC")
        self.fx.fail_wrap.add("A")  # Neuerzeugung scheitert -> alte Datei bleibt
        s, _, _ = self.run_conv()
        self.assertEqual({r.name: r.status for r in s.results}["A.MXF"], converter.ST_FAILED)
        self.assertEqual(self.out("Clip/A.MXF").read_bytes(), b"DST|A|TRUNC")
        self.fx.fail_wrap.clear()
        s, _, _ = self.run_conv()
        self.assertEqual({r.name: r.status for r in s.results}["A.MXF"], converter.ST_CONVERTED)
        self.assertEqual(self.out("Clip/A.MXF").read_bytes(), b"DST|A|L")
        self.assert_consistent()

    def test_copied_clip_with_foreign_umid_not_overwritten(self):
        (self.src / "Clip" / "Z.MXF").write_bytes(b"OTHER|Z" + b"." * 5)
        (self.tgt / "XDROOT" / "Clip").mkdir(parents=True)
        self.run_conv()
        self.out("Clip/Z.MXF").write_bytes(b"OTHER|Zfremd")
        os.utime(self.out("Clip/Z.MXF"), (1, 1))

        def other_uid(path, cancel=None, _orig=_mxf_info):
            i = _orig(path)
            if Path(path).name == "Z.MXF" and "fremd" in Path(path).read_text():
                i.mp_uid = "FFFF"
            return i

        with mock.patch.object(mxfinfo, "read_mxf_info", other_uid):
            s, _, _ = self.run_conv()
        self.assertEqual({r.name: r.status for r in s.results}["Z.MXF"], converter.ST_FAILED)
        self.assertEqual(self.out("Clip/Z.MXF").read_bytes(), b"OTHER|Zfremd")


class G3_ResumeHeaderRecheck(RunTestBase):
    def test_adopt_header_then_full_check(self):
        """Belegter Fehler: Nach dem Nachziehen der Kopfdaten lief nur check_header (16-Bit-Quelle -> falscher Ton)."""
        self.run_conv()
        self.fx.checks.clear()
        self.fx.adopts.clear()
        self.fx.header_problems = ["Identification abweichend"]
        self.fx.after_adopt_problems = ["Audiospur 1: unerwartetes Format"]
        s, rep, _ = self.run_conv(only=["A"])
        self.assertEqual(self.fx.adopts[0], "A.MXF")
        # nach dem Angleichen wurde check_clip ERNEUT (mit Quelle = volle Pruefung) aufgerufen
        self.assertEqual([c for c in self.fx.checks if c[0] == "A.MXF"], [("A.MXF", False), ("A.MXF", True)])
        # Ergebnis der vollen Pruefung fiel durch -> Clip wurde neu erzeugt
        self.assertEqual({r.name: r.status for r in s.results}["A.MXF"], converter.ST_CONVERTED)
        self.assertEqual(self.fx.transcodes.count("A.MXF"), 2)

    def test_adopt_header_ok_skips_transcode(self):
        self.run_conv()
        self.fx.header_problems = ["Identification abweichend"]
        s, rep, _ = self.run_conv(only=["A"])
        res = {r.name: r for r in s.results}["A.MXF"]
        self.assertEqual(res.status, converter.ST_SKIPPED_DONE)
        self.assertIn("Kopfdaten nachgezogen", res.notes)
        self.assertEqual(self.fx.transcodes.count("A.MXF"), 1)


class A2_SeqOff(unittest.TestCase):
    """--seq-off wird (nur wenn != 0) an raw2bmx uebergeben."""

    def _args(self, seq_off):
        seen = []

        def fake_run(args, **kw):
            seen.append(list(args))
            return tools.ProcResult(0, "", "")

        info = mxfinfo.MxfInfo(mp_uid="AA", fp_uid="BB", start_tc="00:00:00:00", ident_modified="2025-01-01 10:00:00.000")
        info.tracks = [mxfinfo.MxfTrack(index=0, kind="Picture", descriptor={}),
                       mxfinfo.MxfTrack(index=1, kind="Sound", descriptor={"locked": "true", "audio_ref_level": "0"})]
        plan = converter.ClipPlan(mxf=Path("A.MXF"), stem="A", material=None, info=info)
        plan.seq_off = seq_off
        conv = converter.Converter(converter.Options(source="s", target="t", sony_header=False))
        with mock.patch.object(tools, "run_capture", fake_run), mock.patch.object(tools, "find_tool", lambda n: n):
            err = conv._wrap(plan, Fraction(30000, 1001), 30, Path("v"), [Path("w")], Path("o"), None, None)
        self.assertEqual(err, "")
        return seen[0]

    def test_seq_off_passed(self):
        a = self._args(3)
        i = a.index("--seq-off")
        self.assertEqual(a[i + 1], "3")
        self.assertLess(i, a.index("--avc_high_422"))  # Clip-Option vor den Eingaengen
        self.assertEqual(a[a.index("-f") + 1], "30000/1001")

    def test_no_seq_off_when_zero_or_unknown(self):
        self.assertNotIn("--seq-off", self._args(0))
        self.assertNotIn("--seq-off", self._args(None))


class A2_ScanSeqOff(RunTestBase):
    def test_tracks_with_different_offsets_are_copied_with_warning(self):
        self.fx.seq_offs = [0, 2]
        s, rep, _ = self.run_conv()
        res = {r.name: r for r in s.results}
        self.assertEqual(res["A.MXF"].status, converter.ST_COPIED)
        self.assertIn("SequenceOffset", rep.text("warn"))
        self.assertEqual(self.fx.transcodes, [])

    def test_offset_is_planned(self):
        self.fx.seq_offs = [3, 3]
        src_root = card.find_xdroot(str(self.src))
        mp = card.parse_mediapro((src_root / "MEDIAPRO.XML").read_bytes())
        conv = converter.Converter(converter.Options(source=str(self.src), target=str(self.tgt)), Rep())
        plans = conv.scan(src_root, mp, card.list_clip_mxf(src_root))
        self.assertEqual([p.seq_off for p in plans], [3, 3])


class LockTests(RunTestBase):
    def test_second_run_is_refused_and_writes_nothing(self):
        self.tgt.mkdir()
        lock = converter.acquire_lock(self.tgt)
        try:
            s, rep, _ = self.run_conv()
            self.assertIn("bereits von einem anderen Lauf benutzt", s.fatal)
            self.assertEqual(self.fx.transcodes, [])
            self.assertTrue(lock.exists())  # fremde Sperre bleibt
            self.assertFalse((self.tgt / "XDROOT").exists())
            self.assertFalse((self.tgt / converter.LOG_NAME).exists())
        finally:
            converter.release_lock(lock)

    def test_lock_removed_after_run_and_after_failure(self):
        self.run_conv()
        self.assertFalse((self.tgt / converter.LOCK_NAME).exists())
        with mock.patch.object(converter.Converter, "scan", side_effect=RuntimeError("boom")):
            s, _, _ = self.run_conv()
        self.assertIn("boom", s.fatal)
        self.assertFalse((self.tgt / converter.LOCK_NAME).exists())

    def test_orphan_lock_of_dead_process_is_taken_over(self):
        self.tgt.mkdir()
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        (self.tgt / converter.LOCK_NAME).write_text(
            json.dumps({"pid": p.pid, "start": 1.0, "host": __import__("socket").gethostname()}))
        s, _, _ = self.run_conv()
        self.assertEqual(s.fatal, "")
        self.assertFalse((self.tgt / converter.LOCK_NAME).exists())

    def test_lock_of_living_process_is_kept(self):
        self.tgt.mkdir()
        lock = converter.acquire_lock(self.tgt)
        with self.assertRaises(converter.TargetBusy):
            converter.acquire_lock(self.tgt)
        converter.release_lock(lock)
        self.assertFalse(lock.exists())
        converter.release_lock(lock)  # doppelt ist harmlos

    def test_lock_from_before_boot_is_stale(self):
        self.tgt.mkdir()
        (self.tgt / converter.LOCK_NAME).write_text(
            json.dumps({"pid": os.getpid(), "start": 1.0, "host": __import__("socket").gethostname()}))
        with mock.patch.object(converter, "_boot_time", lambda: 10 ** 9):
            lock = converter.acquire_lock(self.tgt)
        self.assertTrue(lock.exists())
        converter.release_lock(lock)

    def test_lock_of_other_host_is_respected(self):
        self.tgt.mkdir()
        (self.tgt / converter.LOCK_NAME).write_text(json.dumps({"pid": 999999, "start": 1.0, "host": "anderer-rechner-xyz"}))
        with self.assertRaises(converter.TargetBusy):
            converter.acquire_lock(self.tgt)


class K_Settings(RunTestBase):
    def test_resume_with_other_settings_warns_and_does_not_rebuild(self):
        self.run_conv(bitrate_mbps=50)
        s, rep, _ = self.run_conv(bitrate_mbps=40, preset="slow")
        self.assertEqual(s.count(converter.ST_SKIPPED_DONE), 2)
        self.assertEqual(self.fx.transcodes, ["A.MXF", "B.MXF"])  # kein Neuerzeugen
        w = rep.text("warn")
        self.assertIn("werden nicht neu erzeugt", w)
        self.assertIn("Bitrate (Mbit/s): damals 50, jetzt 40", w)
        self.assertIn("Preset: damals medium, jetzt slow", w)

    def test_same_settings_no_warning(self):
        self.run_conv()
        s, rep, _ = self.run_conv()
        self.assertNotIn("nicht neu erzeugt", rep.text("warn"))

    def test_settings_file_written(self):
        self.run_conv(keep_anc=False)
        data = json.loads((self.tgt / converter.SETTINGS_NAME).read_text(encoding="utf-8"))
        self.assertIs(data["clips"]["Clip/A.MXF"]["keep_anc"], False)

    def test_diff_settings_ignores_unknown(self):
        self.assertEqual(converter.diff_settings({"ffmpeg": "?"}, {"ffmpeg": "x"}), [])
        self.assertEqual(converter.diff_settings({"preset": "a"}, {"preset": "b"}), ["Preset: damals a, jetzt b"])


class K_TestMarker(RunTestBase):
    def test_full_run_removes_marker(self):
        s, _, _ = self.run_conv(max_frames=5)
        marker = self.tgt / verify.TEST_MARKER
        self.assertTrue(marker.exists())
        # Testclips haben "falsche" Dauer -> im Fake: Dauer-Abweichung simulieren
        for n in ("A", "B"):
            self.out(f"Clip/{n}.MXF").write_bytes(f"DST|{n}|TRUNC".encode())
        s, rep, _ = self.run_conv()
        self.assertEqual(s.failed, 0)
        self.assertFalse(marker.exists())
        self.assertIn("Testmarker", rep.text("info"))

    def test_max_frames_run_into_complete_target_aborts(self):
        self.run_conv()
        before = self.out("Clip/A.MXF").read_bytes()
        s, _, _ = self.run_conv(max_frames=5)
        self.assertIn("kein Testlauf", s.fatal)
        self.assertEqual(self.out("Clip/A.MXF").read_bytes(), before)
        self.assertFalse((self.tgt / verify.TEST_MARKER).exists())

    def test_max_frames_run_into_test_target_is_fine(self):
        self.run_conv(max_frames=5)
        s, _, _ = self.run_conv(max_frames=5)
        self.assertEqual(s.fatal, "")

    def test_marker_stays_when_clip_failed(self):
        self.run_conv(max_frames=5)
        self.fx.fail_wrap.add("A")
        self.out("Clip/A.MXF").unlink()
        s, _, _ = self.run_conv()
        self.assertEqual(s.failed, 1)
        self.assertTrue((self.tgt / verify.TEST_MARKER).exists())


class K_DiskEstimate(RunTestBase):
    def _need(self, rep):
        import re
        m = re.search(r"Speicherbedarf geschaetzt (\S+ \w+)", rep.text("info"))
        return m.group(1)

    def test_finished_clips_are_not_counted_again(self):
        _, rep1, _ = self.run_conv()
        _, rep2, _ = self.run_conv()
        # beim ersten Lauf Platz fuer 2 Clips + Temp, beim zweiten nur Reserve (alles fertig)
        self.assertTrue(self._need(rep2).endswith("MB"))
        n1 = float(self._need(rep1).split()[0])
        n2 = float(self._need(rep2).split()[0])
        self.assertLessEqual(n2, n1)
        self.assertAlmostEqual(n2, 200.0, delta=0.5)  # nur die Reserve, nichts mehr zu tun


class K_Summary(RunTestBase):
    def test_incomplete_card_is_reported_loudly(self):
        self.fx.fail_wrap.add("B")
        s, rep, _ = self.run_conv()
        self.assertEqual(s.failed, 1)
        t = s.text()
        self.assertIn("ACHTUNG: Zielkarte unvollstaendig - 1 Clip(s) fehlen: B.MXF", t)
        self.assertIn("ACHTUNG: Zielkarte unvollstaendig", rep.text("info"))  # steht auch im Log
        self.assertEqual(s.results[1].dst_size, 0)

    def test_cancelled_clip_shows_existing_target_size(self):
        self.run_conv()
        self.out("Clip/A.MXF").write_bytes(b"DST|A|TRUNC")
        cancel = tools.CancelToken()
        cancel.cancel()
        s = converter.Converter(converter.Options(source=str(self.src), target=str(self.tgt)), Rep(), cancel).run()
        self.assertTrue(s.cancelled)

    def test_cancel_during_clip_keeps_dst_size(self):
        self.run_conv()
        self.out("Clip/A.MXF").write_bytes(b"DST|A|TRUNC")  # vorhanden, wird neu erzeugt
        cancel = tools.CancelToken()
        orig = converter.Converter._transcode

        def tr(conv, *a, **k):
            cancel.cancel()
            cancel.check()

        with mock.patch.object(converter.Converter, "_transcode", tr):
            s = converter.Converter(converter.Options(source=str(self.src), target=str(self.tgt)), Rep(), cancel).run()
        r = {x.name: x for x in s.results}["A.MXF"]
        self.assertEqual(r.status, converter.ST_CANCELLED)
        self.assertEqual(r.dst_size, len(b"DST|A|TRUNC"))
        self.assertIn(f"-> {converter.fmt_size(r.dst_size)}", s.text())


class K7_CopyErrors(RunTestBase):
    def test_copy_error_does_not_abort_run(self):
        real = card.copy_file_atomic

        def flaky(s, d):
            if Path(s).name == "CUEUP.XML":
                raise OSError("Zugriff verweigert")
            return real(s, d)

        with mock.patch.object(card, "copy_file_atomic", flaky):
            s, rep, _ = self.run_conv()
        self.assertEqual(s.fatal, "")
        self.assertEqual(s.count(converter.ST_CONVERTED), 2)  # Lauf ging weiter
        self.assertEqual([e[0] for e in s.copy_errors], ["CUEUP.XML"])
        self.assertGreaterEqual(s.failed, 1)  # -> Exit-Code != 0
        self.assertIn("FEHLER beim Kopieren von CUEUP.XML", s.text())
        self.assertIn("Zugriff verweigert", rep.text("error"))


class K9_NoCodecString(RunTestBase):
    def test_clip_without_xml_and_mediapro_is_converted(self):
        make_card(self.tmp / "c9", unlisted=("U",), no_xml=("U",))
        self.fx.probe_level = "41"
        s, rep, conv = self.run_conv(source=self.tmp / "c9")
        res = {r.name: r for r in s.results}
        self.assertEqual(res["U.MXF"].status, converter.ST_CONVERTED)
        self.assertEqual(self.out("Clip/U.MXF").read_bytes(), b"DST|U|L")
        self.assertFalse(self.out("Clip/UM01.XML").exists())  # kein XML zu patchen
        self.assertIn("kein Codec-String", rep.text("warn"))
        self.assertIn("4.1", rep.text("warn"))
        self.assert_consistent()

    def test_level_from_probe_is_passed_to_expect(self):
        make_card(self.tmp / "c9", unlisted=("U",), no_xml=("U",))
        self.fx.probe_level = "41"
        seen = []
        orig = converter.make_expect
        with mock.patch.object(converter, "make_expect", lambda **kw: seen.append(kw.get("level")) or orig(**kw)):
            self.run_conv(source=self.tmp / "c9")
        self.assertIn("4.1", seen)

    def test_unknown_codec_string_is_not_guessed(self):
        xr = make_card(self.tmp / "c9")
        p = xr / "Clip" / "AM01.XML"
        p.write_bytes(p.read_bytes().replace(CODEC_I.encode(), b"WEIRD_CODEC"))
        mp = xr / "MEDIAPRO.XML"
        mp.write_bytes(mp.read_bytes().replace(CODEC_I.encode() + b'" audioType="LPCM24" umid="' + uid("A").encode(),
                                               b'WEIRD_CODEC" audioType="LPCM24" umid="' + uid("A").encode()))
        s, rep, _ = self.run_conv(source=self.tmp / "c9")
        res = {r.name: r for r in s.results}
        self.assertEqual(res["A.MXF"].status, converter.ST_COPIED)
        self.assertIn("unbekanntes Codec-Muster", rep.text("warn"))
        self.assertEqual(self.out("Clip/AM01.XML").read_bytes(), p.read_bytes())  # XML unveraendert


class K10_Progress(RunTestBase):
    def test_analysis_progress_text(self):
        s, rep, _ = self.run_conv()
        texts = [c[4] for c in rep.progress_calls]
        self.assertIn("Analysiere Clip 1/2", texts)
        self.assertIn("Analysiere Clip 2/2", texts)

    def test_eta_ignores_skipped_clips(self):
        conv = converter.Converter(converter.Options(source="s", target="t"), Rep())
        conv._frames_total = 1000
        conv._frames_pending = 1000
        conv._frames_done = 500  # 500 Frames wurden uebersprungen -> zaehlen NICHT in die Geschwindigkeit
        plan = converter.ClipPlan(mxf=Path("A.MXF"), stem="A", material=None, frames=100)
        plan.decision = mxfinfo.SourceDecision(True, "x", Fraction(50))
        plan.new_codec = CODEC_L
        conv._clip_t0 = converter.time.time() - 10
        conv._emit_progress(plan, 0.45, "x", transcoded=0)
        self.assertIsNone(conv.rep.progress_calls[-1][3])  # nichts transkodiert -> keine Restzeit
        conv._emit_progress(plan, 0.45, "x", transcoded=50)  # 50 Frames in 10 s = 5 fps
        eta = conv.rep.progress_calls[-1][3]
        self.assertAlmostEqual(eta, (1000 - 50) / 5.0, delta=2)

    def test_no_eta_while_copying(self):
        conv = converter.Converter(converter.Options(source="s", target="t"), Rep())
        conv._frames_total = 100
        conv._work_frames_done, conv._work_secs_done = 100, 50.0
        plan = converter.ClipPlan(mxf=Path("A.MXF"), stem="A", material=None)
        conv._emit_progress(plan, 0.5, "Kopiere")
        self.assertIsNone(conv.rep.progress_calls[-1][3])

    def test_copy_progress_by_bytes(self):
        (self.src / "Clip" / "Z.MXF").write_bytes(b"OTHER|Z" + b"." * 100)
        s, rep, _ = self.run_conv()
        self.assertTrue(any(c[0] == "Z.MXF" and c[4].startswith("Kopiere") for c in rep.progress_calls))

    def test_phase_texts(self):
        s, rep, _ = self.run_conv()
        texts = " | ".join(c[4] for c in rep.progress_calls)
        for t in ("Verpacke MXF", "Pruefe Ergebnis", "Gleiche MXF-Kopfdaten"):
            self.assertIn(t, texts)


class K11_CopiedClips(RunTestBase):
    def test_unchanged_clip_is_not_copied_again(self):
        (self.src / "Clip" / "Z.MXF").write_bytes(b"OTHER|Z" + b"." * 100)
        self.run_conv()
        with mock.patch.object(converter.Converter, "_copy_cancelable", side_effect=AssertionError("kopiert erneut")):
            s, rep, _ = self.run_conv()
        r = {x.name: x for x in s.results}["Z.MXF"]
        self.assertEqual(r.status, converter.ST_COPIED)
        self.assertEqual(r.dst_size, r.src_size)
        self.assertEqual(r.src_size, (self.out("Clip/Z.MXF")).stat().st_size)

    def test_changed_source_clip_is_copied_again(self):
        z = self.src / "Clip" / "Z.MXF"
        z.write_bytes(b"OTHER|Z" + b"." * 100)
        self.run_conv()
        z.write_bytes(b"OTHER|Z" + b"." * 300)
        s, _, _ = self.run_conv()
        self.assertEqual(self.out("Clip/Z.MXF").stat().st_size, 307)


class LogDetail(RunTestBase):
    def test_header_changes_are_detail_and_one_info_line(self):
        self.fx.adopt_msgs = [f"Aenderung {i}" for i in range(5)] + ["WARNUNG: komisch"]
        s, rep, _ = self.run_conv()
        info = rep.text("info")
        self.assertEqual(info.count("MXF-Kopfdaten an Quelle angeglichen (5 Aenderungen)"), 2)  # je Clip eine Zeile
        self.assertNotIn("Aenderung 3", info)
        self.assertIn("Aenderung 3", rep.text("detail"))
        self.assertEqual(rep.text("warn").count("komisch"), 2)
        log = (self.tgt / converter.LOG_NAME).read_text(encoding="utf-8")
        self.assertIn("[detail]", log)
        self.assertIn("Aenderung 3", log)


class Overview(RunTestBase):
    def test_overview_still_works(self):
        ov = converter.overview(converter.Options(source=str(self.src), target=str(self.tgt)))
        self.assertEqual((ov.clips, ov.to_convert), (2, 2))


if __name__ == "__main__":
    unittest.main()
