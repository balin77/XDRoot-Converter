"""Tests der Abschlussprüfung mit automatischer Wiederholung (Runde 4).

Der Ablauf läuft mit den Attrappen aus test_run (ffmpeg/raw2bmx/mxf2raw ersetzt).  ``verify.verify_card`` wird durch eine
Attrappe ersetzt, die den Zustand des Ziels liest und gesteuert Abweichungen meldet; die echte ``verify_card`` wird
gesondert (strukturierte Befunde, Clip-Auswahl) geprüft.
"""

import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xdroot_converter import card, cli, converter, gui, mxfinfo, tools, verify

try:
    from test_run import CODEC_I, RunTestBase, Rep, make_card, mediapro_xml
except ImportError:  # pragma: no cover
    from tests.test_run import CODEC_I, RunTestBase, Rep, make_card, mediapro_xml


class FakeVerify:
    """Ersatz für verify.verify_card: liest das Ziel und meldet Abweichungen nach Vorgabe."""

    def __init__(self):
        self.calls: list = []
        self.dev: dict = {}  # Clipname -> wie oft die Einzelprüfung noch eine Abweichung meldet
        self.card_wide: list = []  # [(Text, kind, path)] zusätzlich bei kartenweiter Prüfung
        self.cancel_at = 0  # beim n-ten Aufruf (1-basiert) wird abgebrochen
        self.lock_seen: list = []
        self.compare_thumbs = False  # Thmbnl-Dateien byte-genau gegen die Quelle vergleichen (Begleitdateien)
        self.error: Exception = None  # wird beim Aufruf geworfen

    def __call__(self, target, source=None, deep=False, log=lambda s: None, cancel=None, threads=None, clips=None,
                 card_wide=True, progress=None, stage=None):
        self.calls.append({"clips": None if clips is None else set(clips), "card_wide": card_wide, "deep": deep,
                           "threads": threads, "cancel": cancel, "source": source})
        tgt = Path(target)
        self.lock_seen.append((tgt.parent / converter.LOCK_NAME).exists())
        if self.error is not None:
            raise self.error
        if self.cancel_at and len(self.calls) == self.cancel_at:
            cancel.cancel()
            cancel.check()
        rep = verify.VerifyReport()
        mp = card.parse_mediapro((tgt / "MEDIAPRO.XML").read_bytes())
        wanted = None if clips is None else {c.casefold() for c in clips}
        for m in mp.materials:
            nm = m.rel_path.split("/")[-1]
            if (card_wide or (wanted is not None and nm.casefold() in wanted)) and not (tgt / m.rel_path).is_file():
                rep.add(f"{m.rel_path}: in MEDIAPRO.XML gelistet, aber Datei fehlt im Ziel", clip=m.rel_path,
                        kind="missing_listed")
        names = sorted(p.name for p in (tgt / "Clip").glob("*.MXF"))
        if wanted is not None:
            names = [n for n in names if n.casefold() in wanted]
        for i, n in enumerate(names):
            if progress:
                progress(i + 1, len(names), f"Clip/{n}")
            if cancel:
                cancel.check()
            rep.clips_checked += 1
            if self.dev.get(n, 0) > 0:
                self.dev[n] -= 1
                rep.add(f"Clip/{n}: Decodierfehler: ['error while decoding MB']", clip=f"Clip/{n}")
            else:
                rep.clips_ok += 1
        if card_wide:
            for msg, kind, path in self.card_wide:
                rep.add(msg, kind=kind, path=path)
            if self.compare_thumbs:
                src = Path(source)
                for p in sorted((src / "Thmbnl").glob("*.JPG")):
                    t = tgt / "Thmbnl" / p.name
                    if not t.is_file() or t.read_bytes() != p.read_bytes():
                        rep.add(f"Thmbnl/{p.name}: Datei unterscheidet sich von der Quelle", kind="companion",
                                path=f"Thmbnl/{p.name}")
        return rep


class AutoBase(RunTestBase):
    def setUp(self):
        super().setUp()
        self.fv = FakeVerify()
        p = mock.patch.object(verify, "verify_card", self.fv)
        p.start()
        self.addCleanup(p.stop)
        # gründliche Prüfung der NEUEN Datei bei der Wiederholung (echtes ffmpeg/Tonvergleich) ersetzt; steuerbar
        self.deep_probs: list = []  # je Aufruf der Prüfungs-Attrappe: Liste von Befunden (leer = in Ordnung)
        self.deep_calls: list = []
        outer = self

        def deep_new(conv, plan, mxf, frames):
            outer.deep_calls.append(plan.name)
            return list(outer.deep_probs.pop(0)) if outer.deep_probs else []

        p2 = mock.patch.object(converter.Converter, "_deep_check_new", deep_new)
        p2.start()
        self.addCleanup(p2.stop)

    def run_auto(self, **kw):
        kw.setdefault("auto_verify", True)
        return self.run_conv(**kw)

    def dst_bytes(self, name):
        return self.out(f"Clip/{name}").read_bytes()


class Basics(AutoBase):
    def test_runs_after_conversion_and_reports(self):
        s, rep, _ = self.run_auto()
        self.assertEqual(len(self.fv.calls), 1)
        c = self.fv.calls[0]
        self.assertEqual((c["clips"], c["card_wide"], c["deep"]), (None, True, True))
        self.assertTrue(c["source"].endswith("XDROOT"))
        self.assertTrue(s.verify.verified_ok)
        self.assertTrue(s.verified_ok)
        self.assertEqual((s.verify.checked, s.verify.ok, s.verify.rounds), (2, 2, 0))
        txt = s.text()
        self.assertIn("Abschlussprüfung (gründlich", txt)
        self.assertIn("2 Clips geprüft, 2 ohne Abweichung", txt)
        self.assertIn("Fertig – geprüft", txt)
        self.assertIn("Umwandlung", txt.split("Dauer:")[-1])
        self.assertGreater(s.convert_seconds, 0)

    def test_library_default_is_off(self):
        self.assertFalse(converter.Options(source="a", target="b").auto_verify)
        s, _, _ = self.run_conv()
        self.assertEqual(self.fv.calls, [])
        self.assertIsNone(s.verify)
        lines = s.text().splitlines()
        self.assertNotIn("Abschlussprüfung", "\n".join(lines[:-1]))  # bisheriger Text unverändert, nur die Schlusszeile
        self.assertEqual(lines[-1], "Ergebnis: Fertig (nicht geprüft: Abschlussprüfung ausgeschaltet)")

    def test_progress_is_own_phase(self):
        s, rep, _ = self.run_auto()
        texts = [p[4] for p in rep.progress_calls if p[4].startswith("Abschlussprüfung")]
        self.assertIn("Abschlussprüfung: Clip 1/2", texts)
        self.assertIn("Abschlussprüfung: Clip 2/2", texts)

    def test_quick_mode_not_deep(self):
        self.run_auto(auto_verify_deep=False)
        self.assertFalse(self.fv.calls[0]["deep"])

    def test_threads_and_cancel_token_passed(self):
        s, rep, conv = self.run_auto(threads=2)
        self.assertEqual(self.fv.calls[0]["threads"], 2)
        self.assertIs(self.fv.calls[0]["cancel"], conv.cancel)  # ffmpeg-Prozesse der Prüfung hängen am CancelToken

    def test_lock_held_during_verification_and_released(self):
        self.run_auto()
        self.assertTrue(all(self.fv.lock_seen))
        self.assertFalse((self.tgt / converter.LOCK_NAME).exists())

    def test_max_frames_has_no_verification(self):
        s, rep, _ = self.run_auto(max_frames=3)
        self.assertEqual(self.fv.calls, [])
        self.assertTrue(s.verify.skipped)
        self.assertFalse(s.verify_problem)
        self.assertIn("entfällt", s.text())

    def test_resume_run_without_work_still_verifies(self):
        self.run_conv()  # ohne Prüfung
        before = list(self.fx.transcodes)
        s, _, _ = self.run_auto()
        self.assertEqual(s.count(converter.ST_SKIPPED_DONE), 2)
        self.assertEqual(self.fx.transcodes, before)
        self.assertEqual(len(self.fv.calls), 1)
        self.assertTrue(s.verified_ok)

    def test_verify_exception_is_no_conversion_error(self):
        self.fv.error = RuntimeError("kaputt")
        s, _, _ = self.run_auto()
        self.assertEqual(s.failed, 0)
        self.assertEqual(s.fatal, "")
        self.assertFalse(s.verify.verified_ok)
        self.assertTrue(s.verify_problem)
        self.assertIn("nicht möglich", s.text())


class Retries(AutoBase):
    def _fail_first_wrap_of(self, stem):
        """raw2bmx scheitert beim ersten Versuch für ``stem``, danach gelingt es."""
        self.fx.fail_wrap.add(stem)
        orig = converter.Converter._wrap

        def wrap(conv, plan, *a, **k):
            r = orig(conv, plan, *a, **k)
            if r:
                self.fx.fail_wrap.discard(plan.stem)
            return r

        p = mock.patch.object(converter.Converter, "_wrap", wrap)
        p.start()
        self.addCleanup(p.stop)

    def test_failed_clip_is_retried_and_succeeds(self):
        self._fail_first_wrap_of("A")
        s, rep, _ = self.run_auto()
        self.assertEqual(self.fx.transcodes, ["A.MXF", "B.MXF", "A.MXF"])
        self.assertEqual((s.failed, s.count(converter.ST_CONVERTED)), (0, 2))
        self.assertEqual(s.verify.rounds, 1)
        self.assertIn("A.MXF", s.verify.retried)
        self.assertIn("Fehler in der Umwandlung", s.verify.retried["A.MXF"][0])
        self.assertTrue(s.verified_ok)
        self.assertEqual(self.dst_bytes("A.MXF"), b"DST|A|L")
        self.assert_consistent()
        self.assertEqual(len([r for r in s.results if r.name == "A.MXF"]), 1)
        self.assertTrue(s.results[0].retry_reasons)
        self.assertIn("wiederholt: A.MXF", s.text())

    def test_deviation_forces_reconversion_of_existing_clip(self):
        self.run_conv()
        self.assertEqual(self.fx.transcodes, ["A.MXF", "B.MXF"])
        self.fv.dev = {"A.MXF": 1}
        s, rep, _ = self.run_auto()
        # der vorhandene, intakte Clip wird erst als fertig übersprungen, dann von der Prüfung erzwungen neu erzeugt
        self.assertEqual(self.fx.transcodes, ["A.MXF", "B.MXF", "A.MXF"])
        self.assertEqual(s.verify.rounds, 1)
        self.assertTrue(s.verified_ok)
        self.assertEqual(s.count(converter.ST_CONVERTED), 1)
        self.assertEqual(s.count(converter.ST_SKIPPED_DONE), 1)
        # Wiederholungsrunde prüft nur den betroffenen Clip, danach einmal kartenweit
        self.assertEqual([(c["clips"], c["card_wide"]) for c in self.fv.calls],
                         [(None, True), ({"A.MXF"}, False), (set(), True)])

    def test_retry_failure_keeps_existing_target(self):
        self.run_conv()
        old = self.dst_bytes("A.MXF")
        self.fv.dev = {"A.MXF": 99}
        self.fx.fail_wrap = {"A"}  # jede Neuerzeugung scheitert
        s, _, _ = self.run_auto(retries=2)
        self.assertEqual(self.dst_bytes("A.MXF"), old)  # alter Ziel-Clip bleibt erhalten
        self.assertEqual(s.verify.rounds, 2)
        self.assertEqual(self.fx.transcodes.count("A.MXF"), 1 + 2)
        self.assertIn("A.MXF", s.verify.bad_clips)
        self.assertFalse(s.verified_ok)
        self.assertTrue(s.verify_problem)
        self.assertEqual(s.count(converter.ST_FAILED), 1)
        self.assertEqual(self.src.joinpath("Clip", "A.MXF").read_bytes()[:5], b"SRC|A")  # Quelle unberührt

    def test_no_endless_loop_when_deviation_persists(self):
        self.fv.dev = {"A.MXF": 10 ** 6}
        for retries, rounds in ((0, 0), (1, 1), (2, 2), (5, 5), (9, 5)):
            self.fx.transcodes.clear()
            s, _, _ = self.run_auto(retries=retries, target=self.tmp / f"o{retries}")
            self.assertEqual(s.verify.rounds, rounds, retries)
            self.assertEqual(self.fx.transcodes.count("A.MXF"), 1 + rounds, retries)
            self.assertEqual(self.fx.transcodes.count("B.MXF"), 1, retries)
            self.assertTrue(s.verify_problem)
            self.assertEqual(s.verify.bad_clips, ["A.MXF"])
            self.assertIn("endgültig fehlerhaft: 1", s.text())
            self.assertIn("Fertig mit Fehlern", s.text())

    def test_copied_clips_are_not_retried(self):
        (self.src / "Clip" / "C.MXF").write_bytes(b"OTHER|C" + b"." * 30)
        (self.src / "Clip" / "CM01.XML").write_bytes(b"<x/>")
        self.fv.dev = {"C.MXF": 10 ** 6}
        s, _, _ = self.run_auto()
        self.assertEqual(self.fx.transcodes, ["A.MXF", "B.MXF"])
        self.assertEqual(s.verify.rounds, 0)
        self.assertEqual(s.count(converter.ST_COPIED), 1)
        self.assertTrue(any("C.MXF" in d for d in s.verify.deviations))
        self.assertTrue(s.verify_problem)

    def test_foreign_target_is_not_retried(self):
        self.run_conv()
        self.out("Clip/A.MXF").write_bytes(b"DST|A|L|FREMD")
        before = list(self.fx.transcodes)
        s, _, _ = self.run_auto()
        self.assertEqual(self.fx.transcodes, before)
        self.assertEqual(s.verify.rounds, 0)
        self.assertEqual(s.failed, 1)
        self.assertEqual(self.dst_bytes("A.MXF"), b"DST|A|L|FREMD")  # fremde Datei nie angefasst

    def test_source_finding_is_not_retried(self):
        class SrcVerify(FakeVerify):
            def __call__(s2, *a, **k):
                rep = super().__call__(*a, **k)
                if k.get("card_wide", True) and k.get("clips") is None:
                    rep.add("Clip/A.MXF: Quell-Clip nicht gefunden", clip="Clip/A.MXF", kind="source")
                return rep

        with mock.patch.object(verify, "verify_card", SrcVerify()):
            s, _, _ = self.run_auto()
        self.assertEqual(s.verify.rounds, 0)
        self.assertEqual(self.fx.transcodes, ["A.MXF", "B.MXF"])
        self.assertTrue(s.verify_problem)


class OnlyFilter(AutoBase):
    def test_only_missing_listed_clips_are_no_error_and_no_retry(self):
        s, rep, _ = self.run_auto(only=["A"])
        self.assertEqual(self.fx.transcodes, ["A.MXF"])
        self.assertEqual(s.verify.rounds, 0)
        self.assertFalse(s.verify_problem)
        self.assertTrue(s.verified_ok)
        self.assertEqual(s.verify.deviations, [])
        # B6: genau EINE Zeile zu den nicht ausgewählten Clips
        self.assertEqual(s.verify.notes,
                         ["Nicht ausgewählt (kein Fehler, nicht wiederholt): 1 Clips aus MEDIAPRO.XML"])
        self.assertIn("Hinweis: Nicht ausgewählt", s.text())
        self.assertEqual(len(self.fv.calls), 1)

    def test_only_selected_clip_missing_is_retried(self):
        self.fx.fail_wrap = {"A"}
        s, _, _ = self.run_auto(only=["A"], retries=1)
        self.assertEqual(s.verify.rounds, 1)
        self.assertEqual(self.fx.transcodes, ["A.MXF", "A.MXF"])
        self.assertTrue(s.verify_problem)

    def test_unselected_clip_existing_in_target_with_deviation_is_a_deviation_but_not_retried(self):
        # G2: ein beschädigter, im Ziel vorhandener, aber nicht ausgewählter Clip ist eine ABWEICHUNG (kein "geprüft")
        self.run_conv()
        before = list(self.fx.transcodes)
        self.fv.dev = {"B.MXF": 5}
        s, _, _ = self.run_auto(only=["A"])
        self.assertEqual(s.verify.rounds, 0)
        self.assertNotIn("B.MXF", self.fx.transcodes[len(before):])  # nicht neu umgewandelt
        self.assertEqual(len(s.verify.deviations), 1)
        self.assertIn("nicht ausgewählt – nicht neu umgewandelt; ohne --only bzw. mit --only B erneut starten",
                      s.verify.deviations[0])
        self.assertEqual(s.verify.bad_clips, ["B.MXF"])
        self.assertFalse(s.verified_ok)
        self.assertTrue(s.verify_problem)
        self.assertEqual(s.verify.ok, s.verify.checked - 1)  # K1: zählt nicht als "ohne Abweichung"
        self.assertEqual(s.text().splitlines()[-1], "Ergebnis: Fertig mit Fehlern (betroffen: B.MXF)")
        self.assertEqual(self._exit(s), 1)

    def _exit(self, summary):
        return 1 if (summary.failed or summary.fatal or summary.verify_problem) else 0

    def test_unlisted_skip_is_no_error(self):
        make_card(self.tmp / "card3", names=("A",), unlisted=("U",))
        s, _, _ = self.run_auto(source=self.tmp / "card3", unlisted="skip", target=self.tmp / "o3")
        self.assertEqual(self.fx.transcodes, ["A.MXF"])
        self.assertTrue(s.verified_ok)


class RetryQuality(AutoBase):
    """M1-M3, K1-K4, B5, B6: Qualität der Wiederholung und Ergebnis-Einordnung."""

    def _with_anc(self):
        orig = mxfinfo.read_mxf_info

        def with_anc(path, cancel=None):
            i = orig(path)
            if i.video.essence_type.startswith("AVCI"):
                i.tracks.append(mxfinfo.MxfTrack(index=9, kind="Data", essence_type="ANC_Data"))
            return i

        def extract(conv, plan, tmpd):
            f = tmpd / "anc.klv"
            f.write_bytes(b"anc")
            return f

        for p in (mock.patch.object(mxfinfo, "read_mxf_info", with_anc),
                  mock.patch.object(converter.Converter, "_extract_anc", extract)):
            p.start()
            self.addCleanup(p.stop)

    def test_k1_counts_come_from_final_state(self):
        self.fx.fail_wrap = {"A"}
        orig = converter.Converter._wrap

        def wrap(conv, plan, *a, **k):
            r = orig(conv, plan, *a, **k)
            self.fx.fail_wrap.discard(plan.stem)
            return r

        with mock.patch.object(converter.Converter, "_wrap", wrap):
            s, _, _ = self.run_auto()
        # A entsteht erst in der Wiederholung und zählt mit
        self.assertEqual((s.verify.checked, s.verify.ok, s.verify.rounds), (2, 2, 1))

    def test_m2_deep_check_failure_of_new_file_keeps_old_clip(self):
        self.run_conv()
        old = self.dst_bytes("A.MXF")
        self.fv.dev = {"A.MXF": 1}
        self.deep_probs = [["Tonspur 3: Inhalt weicht von der Quelle ab (vollständiger Vergleich)"]] * 5
        s, _, _ = self.run_auto(retries=1)
        self.assertEqual(self.deep_calls, ["A.MXF"])               # vor dem Ersetzen geprüft
        self.assertEqual(self.dst_bytes("A.MXF"), old)              # alter Clip bleibt
        r = [r for r in s.results if r.name == "A.MXF"][0]
        self.assertEqual(r.status, converter.ST_FAILED)
        self.assertIn("Tonspur 3", " ".join(r.notes))
        # M3: der alte Clip liegt im Ziel - nicht "unbrauchbar/veraltet", sondern "liegt im Ziel, hat aber eine Abweichung"
        self.assertTrue(r.target_ok and r.retry_failed)
        inc = s.incomplete_text()
        self.assertIn("liegen im Ziel, haben aber eine Abweichung", inc)
        self.assertNotIn("unbrauchbar", inc)
        self.assertFalse(s.verified_ok)

    def test_m2_quick_mode_has_no_deep_check_of_new_file(self):
        self.run_conv()
        self.fv.dev = {"A.MXF": 1}
        s, _, _ = self.run_auto(auto_verify_deep=False)
        self.assertEqual(self.deep_calls, [])
        self.assertTrue(s.verified_ok)

    def test_m2_fallback_without_anc_is_rejected_when_old_clip_has_anc(self):
        self._with_anc()
        self.run_conv()
        self.assertIn(b"|ANC", self.out("Clip/A.MXF").read_bytes())
        old = self.dst_bytes("A.MXF")
        self.fx.fail_wrap_anc = {"A"}                               # nur ein Rückfall ohne ANC gelänge
        self.fv.dev = {"A.MXF": 1}
        s, rep, _ = self.run_auto(retries=1)
        self.assertEqual(self.dst_bytes("A.MXF"), old)
        r = [r for r in s.results if r.name == "A.MXF"][0]
        self.assertEqual((r.status, r.target_ok), (converter.ST_FAILED, True))

    def test_m2_fallback_without_anc_is_fine_when_old_clip_has_none(self):
        self._with_anc()
        self.fx.fail_wrap_anc = {"A"}
        self.run_conv()                                             # A ohne ANC erzeugt
        self.fv.dev = {"A.MXF": 1}
        s, _, _ = self.run_auto(retries=1)
        r = [r for r in s.results if r.name == "A.MXF"][0]
        self.assertEqual(r.status, converter.ST_CONVERTED)

    def test_m2_source_unreadable_findings_are_source_kind_and_not_retried(self):
        class SrcVerify(FakeVerify):
            def __call__(s2, *a, **k):
                rep = super().__call__(*a, **k)
                if k.get("clips") is None:
                    rep.add("Clip/A.MXF: Tonspuren-Inhalt nicht pruefbar (Quelle): x", clip="Clip/A.MXF", kind="source")
                return rep

        with mock.patch.object(verify, "verify_card", SrcVerify()):
            s, _, _ = self.run_auto()
        self.assertEqual(s.verify.rounds, 0)
        self.assertEqual(self.fx.transcodes, ["A.MXF", "B.MXF"])
        self.assertTrue(any("Quelle nicht lesbar – Kartenleser/Karte prüfen und erneut starten" in d
                            for d in s.verify.deviations), s.verify.deviations)

    def test_m1_leftover_test_marker_blocks_verification_and_retries(self):
        self.run_conv(max_frames=3)
        self.assertTrue((self.tgt / verify.TEST_MARKER).exists())
        before = list(self.fx.transcodes)
        self.fv.calls.clear()
        s, _, _ = self.run_auto(only=["A"])                          # B bleibt aus dem Testlauf im Ziel -> Marke bleibt
        self.assertTrue((self.tgt / verify.TEST_MARKER).exists())
        self.assertEqual(self.fv.calls, [])
        self.assertEqual(s.verify.rounds, 0)
        self.assertEqual(self.fx.transcodes.count("B.MXF"), 1)       # keine Wiederholungen
        self.assertLessEqual(self.fx.transcodes.count("A.MXF"), 2)
        self.assertEqual(s.verify.deviations, [
            "Im Ziel liegt eine Testausgabe (TESTAUSGABE.txt) – Karte nicht geprüft; "
            "Zielordner leeren oder neuen Zielordner wählen"])
        self.assertTrue(s.verify_problem)
        self.assertFalse(s.verified_ok)
        self.assertTrue(s.text().splitlines()[-1].startswith("Ergebnis: Fertig mit Fehlern"))

    def test_k2_foreign_clip_is_no_retry_candidate_even_with_a_finding(self):
        self.run_conv()
        self.out("Clip/A.MXF").write_bytes(b"DST|A|L|FREMD")
        before = list(self.fx.transcodes)
        self.fv.dev = {"A.MXF": 99}
        s, _, _ = self.run_auto()
        self.assertEqual(self.fx.transcodes, before)
        self.assertEqual(s.verify.rounds, 0)
        self.assertEqual(self.dst_bytes("A.MXF"), b"DST|A|L|FREMD")

    def test_k3_recopied_companion_is_no_copy_error_any_more(self):
        self.fv.compare_thumbs = True
        orig = card.copy_structure

        def copy_structure(src, out, skip_stems=(), cancel_check=None, errors=None):
            n = orig(src, out, skip_stems=skip_stems, cancel_check=cancel_check, errors=errors)
            (out / "Thmbnl" / "AT01.JPG").unlink()
            errors.append(("Thmbnl/AT01.JPG", "gesperrt"))
            return n

        with mock.patch.object(card, "copy_structure", copy_structure):
            s, _, _ = self.run_auto()
        self.assertEqual((self.out("Thmbnl/AT01.JPG").read_bytes()), b"jpg")
        self.assertEqual(s.copy_errors, [])
        self.assertEqual(s.failed, 0)
        self.assertTrue(s.verified_ok)

    def test_k4_backslash_path_finds_the_plan(self):
        class BackslashVerify(FakeVerify):
            done = False

            def __call__(s2, *a, **k):
                rep = super().__call__(*a, **k)
                if k.get("clips") is None and not s2.done:
                    s2.done = True
                    rep.add("Clip\\A.MXF: in MEDIAPRO.XML gelistet, aber Datei fehlt im Ziel", clip="Clip\\A.MXF",
                            kind="missing_listed")
                return rep

        with mock.patch.object(verify, "verify_card", BackslashVerify()):
            s, _, _ = self.run_auto()
        self.assertEqual(self.fx.transcodes, ["A.MXF", "B.MXF", "A.MXF"])
        self.assertEqual(s.verify.rounds, 1)

    def test_b5_retry_progress_text_and_companion_phase(self):
        self.run_conv()
        self.fv.dev = {"A.MXF": 1}
        s, rep, _ = self.run_auto()
        texts = [p[4] for p in rep.progress_calls]
        self.assertTrue(any(t.startswith("Wiederholung Runde 1/2: Clip 1/1 – ") for t in texts), texts[-12:])
        self.assertIn("Abschlussprüfung (kartenweit): Begleitdateien ...", texts)

    def test_b6_one_line_for_unselected_clips(self):
        class UnlistedVerify(FakeVerify):
            def __call__(s2, *a, **k):
                rep = super().__call__(*a, **k)
                if k.get("card_wide", True) and k.get("clips") is None:
                    rep.deliberate = ["Clip/U.MXF", "Clip/V.MXF"]
                    rep.deliberate_note = "2 Quell-Clip(s) fehlen im Ziel, laut Einstellungsdatei bewusst nicht ausgewaehlt"
                    rep.notes.append(rep.deliberate_note)
                    rep.notes.append("GOP-Laenge N=[25] bei 2 Clips")
                return rep

        with mock.patch.object(verify, "verify_card", UnlistedVerify()):
            s, rep, _ = self.run_auto(only=["A"])
        self.assertEqual(s.verify.notes, ["Nicht ausgewählt (kein Fehler, nicht wiederholt): "
                                          "1 Clips aus MEDIAPRO.XML, 2 nicht gelistete Clips"])
        self.assertNotIn("laut Einstellungsdatei", s.text())
        self.assertNotIn("GOP-Laenge", s.text())                     # nur Detail im Protokoll
        self.assertIn("GOP-Laenge", rep.text("detail"))
        self.assertTrue(s.verified_ok)


class Cancel(AutoBase):
    def test_cancel_during_verification(self):
        self.fv.cancel_at = 1
        opts = converter.Options(source=str(self.src), target=str(self.tgt), auto_verify=True)
        conv = converter.Converter(opts, Rep(), tools.CancelToken())
        s = conv.run()
        self.assertTrue(s.cancelled)
        self.assertTrue(s.verify.cancelled)
        self.assertFalse(s.verify.complete)
        self.assertFalse(s.verified_ok)
        self.assertFalse(s.verify_problem)
        self.assertEqual(s.count(converter.ST_CONVERTED), 2)  # Umwandlung war fertig
        self.assertIn("abgebrochen, Prüfung unvollständig", s.text())
        self.assertFalse((self.tgt / converter.LOCK_NAME).exists())
        kind, title, text = gui.completion_message(s)
        self.assertEqual((kind, title), ("warning", "Abgebrochen"))
        self.assertIn("Prüfung ist unvollständig", text)
        self.assertNotIn("Zielkarte ist unvollständig", text)
        self.assertEqual(gui.finish_status(s), "Abgebrochen, Prüfung unvollständig.")

    def test_cancel_during_retry_round(self):
        self.fv.dev = {"A.MXF": 1}
        cancel = tools.CancelToken()
        orig = converter.Converter._finish_clip_files
        seen = []

        def finish(conv, plan, dst_dir):
            orig(conv, plan, dst_dir)
            seen.append(plan.name)
            if seen.count("A.MXF") == 2:  # Wiederholung von A beendet -> Abbruch
                cancel.cancel()

        opts = converter.Options(source=str(self.src), target=str(self.tgt), auto_verify=True)
        with mock.patch.object(converter.Converter, "_finish_clip_files", finish):
            s = converter.Converter(opts, Rep(), cancel).run()
        self.assertTrue(s.cancelled)
        self.assertTrue(s.verify.cancelled)
        self.assertFalse(s.verified_ok)


class Companions(AutoBase):
    def setUp(self):
        super().setUp()
        self.fv.compare_thumbs = True

    def _corrupt_same_stat(self, path):
        """Gleiche Größe und Zeit wie die Quelle (der normale Kopierlauf hält die Datei für unverändert), anderer Inhalt."""
        import os
        src = self.src / "Thmbnl" / path.name
        path.write_bytes(b"x" * src.stat().st_size)
        st = src.stat()
        os.utime(path, (st.st_atime, st.st_mtime))

    def test_companion_recopied_once_and_compared_again(self):
        self.run_conv()
        thumb = self.out("Thmbnl/AT01.JPG")
        self._corrupt_same_stat(thumb)
        self.fv.calls.clear()
        s, _, _ = self.run_auto()
        self.assertEqual(thumb.read_bytes(), b"jpg")
        self.assertEqual(s.verify.rounds, 1)
        self.assertTrue(s.verified_ok)
        self.assertEqual(self.fx.transcodes, ["A.MXF", "B.MXF"])  # keine Neu-Umwandlung
        self.assertEqual([(c["clips"], c["card_wide"]) for c in self.fv.calls], [(None, True), (set(), True)])

    def test_companion_that_cannot_be_fixed_stays_a_deviation(self):
        self.run_conv()
        self._corrupt_same_stat(self.out("Thmbnl/AT01.JPG"))
        with mock.patch.object(converter.Converter, "_recopy_companion") as cp:
            s, _, _ = self.run_auto(retries=3)
        cp.assert_called_once()  # nur einmal versucht
        self.assertTrue(s.verify_problem)
        self.assertTrue(any("AT01.JPG" in d for d in s.verify.deviations))

    def test_xml_findings_are_not_recopied(self):
        self.fv.card_wide = [("Clip/AM01.XML: weicht ausser bei videoCodec von der Quelle ab", "xml", "Clip/AM01.XML")]
        before = self.src.joinpath("Clip", "AM01.XML").read_bytes()
        with mock.patch.object(converter.Converter, "_recopy_companion") as cp:
            s, _, _ = self.run_auto()
        cp.assert_not_called()
        self.assertEqual(s.verify.rounds, 0)
        self.assertTrue(s.verify_problem)
        self.assertEqual(self.src.joinpath("Clip", "AM01.XML").read_bytes(), before)


class RealVerifyStructured(unittest.TestCase):
    """Die echte verify_card liefert strukturierte Befunde; der Text bleibt unverändert."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.root = self.tmp / "XDROOT"
        (self.root / "Clip").mkdir(parents=True)
        (self.root / "MEDIAPRO.XML").write_bytes(mediapro_xml(["A", "B"]))

    def test_missing_listed_is_structured(self):
        rep = verify.verify_card(str(self.root))
        self.assertEqual(len(rep.findings), len(rep.problems), 2)
        f = rep.findings[0]
        self.assertEqual((f.kind, f.clip), ("missing_listed", "Clip/A.MXF"))
        self.assertEqual(rep.problems[0], "Clip/A.MXF: in MEDIAPRO.XML gelistet, aber Datei fehlt im Ziel")
        self.assertEqual(f.message, rep.problems[0])
        self.assertIn("ABWEICHUNG: Clip/A.MXF: in MEDIAPRO.XML gelistet, aber Datei fehlt im Ziel", rep.text())

    def test_clip_restriction_and_card_wide_switch(self):
        rep = verify.verify_card(str(self.root), clips={"B.MXF"}, card_wide=False)
        self.assertEqual([f.clip for f in rep.findings], ["Clip/B.MXF"])
        rep = verify.verify_card(str(self.root), clips=set(), card_wide=False)
        self.assertEqual(rep.findings, [])
        rep = verify.verify_card(str(self.root), clips=set(), card_wide=True)
        self.assertEqual(len(rep.findings), 2)

    def test_compare_files_kinds(self):
        src, tgt = self.tmp / "S" / "XDROOT", self.tmp / "T" / "XDROOT"
        for r in (src, tgt):
            (r / "Clip").mkdir(parents=True)
            (r / "Thmbnl").mkdir()
        (src / "Thmbnl" / "X.JPG").write_bytes(b"1")
        (tgt / "Thmbnl" / "X.JPG").write_bytes(b"2")
        (src / "Thmbnl" / "Y.JPG").write_bytes(b"1")
        (src / "Clip" / "AM01.XML").write_bytes(b"<a/>")
        (tgt / "Clip" / "AM01.XML").write_bytes(b"<b/>")
        mp = card.parse_mediapro(mediapro_xml([]))
        rep = verify.VerifyReport()
        verify._compare_files(src, tgt, mp, mp, rep)
        kinds = {(f.message.split(":")[0], f.kind, f.path) for f in rep.findings}
        self.assertEqual(kinds, {("Thmbnl/X.JPG", "companion", "Thmbnl/X.JPG"),
                                 ("Thmbnl/Y.JPG", "companion", "Thmbnl/Y.JPG"),
                                 ("Clip/AM01.XML", "xml", "Clip/AM01.XML")})

    def test_deep_decode_registers_process_with_cancel_token(self):
        seen = {}

        def fake(args, cancel=None, **kw):
            seen["cancel"] = cancel
            return tools.ProcResult(0, "", "")

        tok = tools.CancelToken()
        with mock.patch.object(tools, "run_capture", fake), mock.patch.object(tools, "find_tool", lambda n: n):
            verify.deep_decode_check("x.mxf", cancel=tok, threads=2)
        self.assertIs(seen["cancel"], tok)


class CliTests(unittest.TestCase):
    def _main(self, argv, summary=None):
        seen = {}

        class Stub:
            def __init__(self, opts, reporter=None, cancel=None):
                seen["opts"] = opts

            def run(self):
                return summary if summary is not None else converter.RunSummary()

        with mock.patch.object(converter, "Converter", Stub), mock.patch("signal.signal"), \
                mock.patch("sys.stdout", io.StringIO()), mock.patch("sys.stderr", io.StringIO()):
            rc = cli.main(argv)
        return rc, seen.get("opts")

    def test_defaults_and_flags(self):
        rc, o = self._main(["a", "b"])
        self.assertEqual((o.auto_verify, o.auto_verify_deep, o.retries), (True, True, 2))
        _, o = self._main(["a", "b", "--no-auto-verify"])
        self.assertFalse(o.auto_verify)
        _, o = self._main(["a", "b", "--auto-verify-quick", "--retries", "4"])
        self.assertEqual((o.auto_verify, o.auto_verify_deep, o.retries), (True, False, 4))
        _, o = self._main(["a", "b", "--retries", "0"])
        self.assertEqual(o.retries, 0)

    def test_b7_help_epilog_and_ineffective_combinations(self):
        help_text = cli.build_parser().format_help()
        self.assertIn("Exit-Codes: 0 fertig (mit Abschlussprüfung: geprüft ohne Abweichung), 1 Fehler oder Abweichung, "
                      "2 falscher Aufruf, 3 Rechte-Erhöhung abgelehnt, 130 abgebrochen (auch Temperatur-Abbruch)", help_text)
        self.assertIn("ohne\n                        Abschlussprüfung)", help_text)  # --max-frames

        def out_of(argv):
            buf = io.StringIO()
            with mock.patch.object(converter, "Converter", mock.MagicMock()), mock.patch("signal.signal"), \
                    mock.patch("sys.stdout", buf), mock.patch("sys.stderr", io.StringIO()), \
                    mock.patch.object(verify, "verify_card", return_value=verify.VerifyReport()):
                cli.main(argv)
            return buf.getvalue()

        self.assertIn("--retries hat zusammen mit --no-auto-verify keine Wirkung", out_of(["a", "b", "--no-auto-verify", "--retries", "3"]))
        self.assertIn("--auto-verify-quick hat zusammen mit --no-auto-verify keine Wirkung",
                      out_of(["a", "b", "--no-auto-verify", "--auto-verify-quick"]))
        self.assertIn("--retries hat zusammen mit --max-frames keine Wirkung", out_of(["a", "b", "--max-frames", "5", "--retries", "1"]))
        self.assertIn("--retries hat zusammen mit --verify keine Wirkung", out_of(["--verify", "x", "--retries", "1"]))
        self.assertNotIn("keine Wirkung", out_of(["a", "b"]))

    def test_retries_range(self):
        for bad in ("6", "-1", "x"):
            with self.assertRaises(SystemExit) as cm, mock.patch("sys.stderr", io.StringIO()):
                cli.build_parser().parse_args(["a", "b", "--retries", bad])
            self.assertEqual(cm.exception.code, 2)

    def test_exit_codes(self):
        ok = converter.RunSummary(verify=converter.VerifyOutcome(started=True, complete=True))
        self.assertEqual(self._main(["a", "b"], ok)[0], 0)
        dev = converter.RunSummary(verify=converter.VerifyOutcome(started=True, complete=True, deviations=["x"]))
        self.assertEqual(self._main(["a", "b"], dev)[0], 1)
        err = converter.RunSummary(verify=converter.VerifyOutcome(started=True, error="kaputt"))
        self.assertEqual(self._main(["a", "b"], err)[0], 1)
        skipped = converter.RunSummary(verify=converter.VerifyOutcome(skipped="Testlauf"))
        self.assertEqual(self._main(["a", "b", "--max-frames", "3"], skipped)[0], 0)
        failed = converter.RunSummary(results=[converter.ClipResult("A.MXF", converter.ST_FAILED)],
                                      verify=converter.VerifyOutcome(started=True, complete=True))
        self.assertEqual(self._main(["a", "b"], failed)[0], 1)
        canc = converter.RunSummary(cancelled=True, verify=converter.VerifyOutcome(started=True, cancelled=True))
        self.assertEqual(self._main(["a", "b"], canc)[0], 130)
        self.assertEqual(self._main(["a", "b", "--no-auto-verify"], converter.RunSummary())[0], 0)


class GuiTexts(unittest.TestCase):
    def _s(self, **vk):
        v = converter.VerifyOutcome(started=True, complete=True, checked=76, ok=76, seconds=480, **vk)
        return converter.RunSummary(results=[converter.ClipResult("A.MXF", converter.ST_CONVERTED)], verify=v,
                                    convert_seconds=3600)

    def test_geprueft(self):
        s = self._s()
        kind, title, text = gui.completion_message(s)
        self.assertEqual((kind, title), ("info", "Fertig – geprüft"))
        self.assertIn("76 Clips", text)
        self.assertIn("keine Abweichung", text)
        self.assertIn("komplett dekodiert", text)
        self.assertIn("Abschlussprüfung 8:00", text)
        self.assertEqual(gui.finish_status(s), "Fertig – geprüft.")

    def test_geprueft_schnell_ohne_dekodieren_hinweis(self):
        s = self._s()
        s.verify.deep = False
        kind, title, text = gui.completion_message(s)
        self.assertNotIn("jeder Clip komplett dekodiert", text)
        self.assertIn("schnelle Prüfung, Clips nicht komplett dekodiert", text)
        self.assertEqual(title, "Fertig – geprüft (schnelle Prüfung)")
        self.assertEqual(gui.finish_status(s), "Fertig – geprüft (schnell).")
        self.assertIn("Geprüft: 76 Clips, 76 ohne Abweichung", text)
        self.assertEqual(s.text().splitlines()[-1], "Ergebnis: Fertig – geprüft (schnelle Prüfung), ohne Abweichung")

    def test_wiederholt_und_in_ordnung(self):
        s = self._s(retried={"B.MXF": ["Runde 1: x"]}, rounds=1)
        kind, title, text = gui.completion_message(s)
        self.assertEqual(title, "Fertig – geprüft")
        self.assertIn("Wiederholt: 1 Clip(s) in 1 Runde(n), danach in Ordnung: 1", text)

    def test_abweichung(self):
        s = self._s(retried={"B.MXF": ["Runde 1: x"]}, rounds=2, bad_clips=["B.MXF"],
                    deviations=["Clip/B.MXF: Decodierfehler: ['x']"])
        kind, title, text = gui.completion_message(s)
        self.assertEqual((kind, title), ("warning", "Fertig mit Fehlern"))
        self.assertIn("Abweichung", text)
        self.assertIn("B.MXF", text)
        self.assertIn("Decodierfehler", text)
        self.assertNotIn("Empfehlung", text)
        self.assertNotIn("geprüft", title)
        self.assertIn("Abweichung(en) in der Prüfung", gui.finish_status(s))

    def test_ohne_pruefung_unveraendert(self):
        s = converter.RunSummary(results=[converter.ClipResult("A.MXF", converter.ST_CONVERTED)])
        self.assertEqual(gui.completion_message(s)[:2], ("info", "Fertig"))
        self.assertEqual(gui.finish_status(s), "Fertig.")

    def test_b1_retried_and_final_lists(self):
        retried = {f"C{i}.MXF": [f"Runde 1: {'x' * 150}"] for i in range(12)}
        bad = [f"C{i}.MXF" for i in range(12)]
        devs = [f"Clip/C{i}.MXF: Decodierfehler" for i in range(12)]
        failed = [converter.ClipResult("C0.MXF", converter.ST_FAILED, notes=["Fehler in der Umwandlung"])]
        s = self._s(retried=retried, rounds=2, bad_clips=bad, deviations=devs)
        s.results += failed
        text = gui.completion_message(s)[2]
        self.assertIn("Geprüft: 76 Clips, 76 ohne Abweichung", text)
        self.assertIn("… und 2 weitere, siehe Protokoll", text)               # wiederholte: höchstens 10 Zeilen
        self.assertIn("… und 2 weitere (siehe Protokoll)", text)              # endgültig fehlerhafte: gemeinsame Liste
        self.assertEqual(text.count("• C0.MXF"), 2)                           # einmal wiederholt, einmal endgültig (nicht doppelt)
        self.assertNotIn("Fehlgeschlagene Clips:", text)
        self.assertNotIn("x" * 120, text)                                      # Gründe gekürzt

    def test_b1_no_retries_heading(self):
        s = self._s(bad_clips=["B.MXF"], deviations=["Clip/B.MXF: Decodierfehler"], retries=0)
        self.assertIn("Fehlerhaft (keine Wiederholung eingestellt):", gui.completion_message(s)[2])

    def test_b2_verification_failed_itself(self):
        v = converter.VerifyOutcome(started=True, error="RuntimeError: kaputt")
        s = converter.RunSummary(results=[converter.ClipResult("A.MXF", converter.ST_CONVERTED)], verify=v)
        kind, title, text = gui.completion_message(s)
        self.assertIn("Die Prüfung konnte nicht durchgeführt werden: RuntimeError: kaputt – die Karte ist NICHT geprüft.", text)
        self.assertNotIn("•", text)
        self.assertNotIn("Geprüft:", text)
        self.assertEqual(s.text().splitlines()[-1], "Ergebnis: Umwandlung fertig, Prüfung nicht möglich – Karte nicht geprüft")
        self.assertIn("Die Prüfung konnte nicht durchgeführt werden", s.text())

    def test_b3_single_final_result_line(self):
        R = converter.ClipResult
        conv = [R("A.MXF", converter.ST_CONVERTED)]

        def last(**kw):
            return converter.RunSummary(**kw).text().splitlines()[-1]

        V = converter.VerifyOutcome
        self.assertEqual(last(results=conv, verify=V(started=True, complete=True)),
                         "Ergebnis: Fertig – geprüft, ohne Abweichung")
        self.assertEqual(last(results=conv, verify=V(started=True, complete=True, deep=False)),
                         "Ergebnis: Fertig – geprüft (schnelle Prüfung), ohne Abweichung")
        self.assertEqual(last(results=conv), "Ergebnis: Fertig (nicht geprüft: Abschlussprüfung ausgeschaltet)")
        self.assertEqual(last(results=conv, verify=V(skipped="entfällt")), "Ergebnis: Fertig (nicht geprüft: Testausgabe)")
        self.assertEqual(last(results=conv, test_run=True), "Ergebnis: Fertig (nicht geprüft: Testausgabe)")
        names = [R(f"C{i}.MXF", converter.ST_FAILED) for i in range(12)]
        line = last(results=names)
        self.assertTrue(line.startswith("Ergebnis: Fertig mit Fehlern (betroffen: C0.MXF, C1.MXF"), line)
        self.assertIn("und 2 weitere", line)
        self.assertEqual(last(results=conv, cancelled=True, verify=V(started=True, cancelled=True)),
                         "Ergebnis: Abgebrochen, Prüfung unvollständig")
        self.assertEqual(last(results=conv, cancelled=True), "Ergebnis: Abgebrochen – Zielkarte unvollständig")
        self.assertTrue(last(results=conv, fatal="Platte voll").startswith("Ergebnis: Fehler – Platte voll"))
        # Temperaturzeile steht VOR der Ergebniszeile
        lines = converter.RunSummary(results=conv, thermal_note="Temperatur nicht verfügbar: x").text().splitlines()
        self.assertEqual(lines[-2], "Temperatur nicht verfügbar: x")
        self.assertEqual(sum(1 for l in lines if l.startswith("Ergebnis:")), 1)

    def test_b7_status_when_heat_abort_during_verification(self):
        v = converter.VerifyOutcome(started=True, cancelled=True)
        s = converter.RunSummary(cancelled=True, abort_reason="CPU zu heiß", verify=v)
        self.assertEqual(gui.finish_status(s), "Abgebrochen: CPU kühlt nicht ab, Prüfung unvollständig.")

    def test_progress_text_phase(self):
        clip, total = gui.progress_texts("B.MXF", "Abschlussprüfung: Clip 12/76", 0.158, 120)
        self.assertIn("Clip 12/76", clip)
        self.assertEqual(total, "Abschlussprüfung 15.8 %, Restzeit ca. 2:00")

    def test_gui_defaults_are_on(self):
        # die Haken stehen standardmäßig an (Prüfung + gründlich); die Variablen werden in App angelegt
        src = Path(gui.__file__).read_text(encoding="utf-8")
        self.assertIn("self.var_auto_verify = tk.BooleanVar(value=True)", src)
        self.assertIn("self.var_verify_deep = tk.BooleanVar(value=True)", src)
        self.assertIn("Nach der Umwandlung prüfen und fehlerhafte Clips wiederholen", src)
        self.assertIn("gründlich (jeden Clip komplett dekodieren)", src)


if __name__ == "__main__":
    unittest.main()
