"""Tests der tkinter-freien GUI-Logik (laufen ohne tkinter und ohne Medien)."""

import unittest
from pathlib import Path
import tempfile

from xdroot_converter import converter, gui


def _summary(**kw):
    s = converter.RunSummary()
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _res(name, status):
    return converter.ClipResult(name=name, status=status)


class BitrateTests(unittest.TestCase):
    def test_gueltig(self):
        self.assertEqual(gui.parse_bitrate("50"), 50.0)
        self.assertEqual(gui.parse_bitrate(" 42,5 "), 42.5)
        self.assertEqual(gui.parse_bitrate("10"), 10.0)
        self.assertEqual(gui.parse_bitrate("100"), 100.0)

    def test_ungueltig(self):
        for bad in ("abc", "", "9.9", "100.1", "nan", "-5", "0"):
            with self.assertRaises(ValueError, msg=bad):
                gui.parse_bitrate(bad)

    def test_meldung_deutsch(self):
        with self.assertRaises(ValueError) as cm:
            gui.parse_bitrate("abc")
        self.assertIn("Mbit/s", str(cm.exception))


class OverviewKeyTests(unittest.TestCase):
    def test_schluessel(self):
        k = gui.overview_key("D:/karte ", False, "50")
        self.assertEqual(k, gui.overview_key("D:/karte", False, "50,0"))
        self.assertNotEqual(k, gui.overview_key("D:/karte", True, "50"))
        self.assertNotEqual(k, gui.overview_key("D:/karte", False, "40"))
        self.assertNotEqual(k, gui.overview_key("E:/karte", False, "50"))

    def test_ungueltige_bitrate_ist_schluessel(self):
        self.assertEqual(gui.overview_key("a", False, "x")[2], None)

    def test_hinweis_haken(self):
        t = "3 Clip(s) liegen auf der Karte, stehen aber nicht in MEDIAPRO.XML."
        self.assertIn("Einstellungen", gui.overview_display_text(t, False))
        self.assertEqual(gui.overview_display_text(t, True), t)
        self.assertEqual(gui.overview_display_text("Kamera: X", False), "Kamera: X")


class TargetTests(unittest.TestCase):
    def test_target_has_xdroot(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(gui.target_has_xdroot(d))
            (Path(d) / "XDROOT").mkdir()
            self.assertTrue(gui.target_has_xdroot(d))
        self.assertFalse(gui.target_has_xdroot(""))


class CompletionTests(unittest.TestCase):
    def test_erfolg(self):
        s = _summary(results=[_res("A", converter.ST_CONVERTED), _res("B", converter.ST_COPIED)],
                     log_path=Path("Z/log.txt"))
        kind, _t, text = gui.completion_message(s)
        self.assertEqual(kind, "info")
        self.assertIn("umgewandelt: 1", text)
        self.assertIn("kopiert: 1", text)
        self.assertIn("log.txt", text)
        self.assertIn("vor", text)  # Empfehlung zur Prüfung vor dem Löschen

    def test_fehler_nie_erfolg(self):
        results = [_res(f"Clip_mit_sehr_langem_Namen_fuer_den_Test_{i:02d}", converter.ST_FAILED) for i in range(13)]
        kind, _t, text = gui.completion_message(_summary(results=results))
        self.assertEqual(kind, "warning")
        self.assertIn("_09", text)
        self.assertNotIn("_10", text)
        self.assertIn("3 weitere", text)
        self.assertNotIn("Empfehlung", text)

    def test_abbruch(self):
        kind, _t, text = gui.completion_message(_summary(cancelled=True))
        self.assertEqual(kind, "warning")
        self.assertNotIn("Empfehlung", text)

    def test_fatal(self):
        kind, _t, text = gui.completion_message(_summary(fatal="Zielordner gehört zu einer anderen Karte"))
        self.assertEqual(kind, "error")
        self.assertIn("anderen Karte", text)

    def test_status(self):
        self.assertEqual(gui.finish_status(_summary()), "Fertig.")
        self.assertTrue(gui.finish_status(_summary(fatal="x")).startswith("Fehler"))
        self.assertEqual(gui.finish_status(_summary(cancelled=True)), "Abgebrochen.")


class CompletionDetailTests(unittest.TestCase):
    def test_copy_errors_getrennt(self):
        errs = [(f"General/F{i}.XML", "Zugriff verweigert") for i in range(12)]
        kind, _t, text = gui.completion_message(_summary(results=[_res("A", converter.ST_CONVERTED)],
                                                         copy_errors=errs))
        self.assertEqual(kind, "warning")
        self.assertIn("12 Datei(en) konnten nicht kopiert werden", text)
        self.assertIn("General/F9.XML", text)
        self.assertNotIn("General/F10.XML", text)
        self.assertIn("2 weitere", text)
        self.assertNotIn("Clip(s) sind fehlgeschlagen", text)
        self.assertNotIn("Empfehlung", text)

    def test_clipfehler_und_copy_errors(self):
        s = _summary(results=[_res("C1", converter.ST_FAILED)], copy_errors=[("a.xml", "x")])
        _k, _t, text = gui.completion_message(s)
        self.assertIn("1 Clip(s) sind fehlgeschlagen", text)
        self.assertIn("1 Datei(en) konnten nicht kopiert werden", text)

    def test_incomplete_text(self):
        r = _res("C1", converter.ST_FAILED)
        s = _summary(results=[r])
        self.assertTrue(s.incomplete_text())
        _k, _t, text = gui.completion_message(s)
        self.assertIn(s.incomplete_text(), text)
        _k, _t, ok = gui.completion_message(_summary(results=[_res("A", converter.ST_CONVERTED)]))
        self.assertNotIn("unvollstaendig", ok)

    def test_abbruch_hinweis(self):
        _k, _t, text = gui.completion_message(_summary(cancelled=True))
        self.assertIn("Die Zielkarte ist unvollständig. Ein erneuter Lauf mit demselben Zielordner setzt fort.", text)

    def test_fatal_ohne_zaehlzeile(self):
        s = _summary(fatal="Der Zielordner enthaelt bereits eine andere Karte oder fremde Daten: x",
                     log_path=Path("Z/log.txt"))
        _k, _t, text = gui.completion_message(s)
        self.assertNotIn("Fehler: 0", text)
        self.assertNotIn("umgewandelt", text)
        self.assertIn("leeren Ordner", text)
        self.assertIn("log.txt", text)

    def test_fatal_gesperrt(self):
        s = _summary(fatal="Der Zielordner wird bereits von einem anderen Lauf benutzt. Datei 'X.lock' loeschen.")
        _k, _t, text = gui.completion_message(s)
        self.assertIn("anderen Lauf", text)
        self.assertIn("Sperrdatei", text)


class FatalMitErgebnissenTests(unittest.TestCase):
    def test_fatal_mitten_im_lauf(self):
        s = _summary(fatal="Platte voll", results=[_res("A", converter.ST_CONVERTED),
                                                    _res("B", converter.ST_FAILED)],
                     log_path=Path("Z/log.txt"))
        kind, _t, text = gui.completion_message(s)
        self.assertEqual(kind, "error")
        self.assertIn("Platte voll", text)
        self.assertNotIn("konnte nicht durchgeführt werden", text)
        self.assertIn("umgewandelt: 1", text)
        self.assertIn("Fehler: 1", text)
        self.assertIn(s.incomplete_text(), text)
        self.assertIn("log.txt", text)


class FehlergrundTests(unittest.TestCase):
    def test_grund_hinter_namen(self):
        r = converter.ClipResult(name="C0001", status=converter.ST_FAILED,
                                 notes=["FFmpeg beendet mit Code 1", "zweite Notiz"])
        _k, _t, text = gui.completion_message(_summary(results=[r]))
        self.assertIn("• C0001 – FFmpeg beendet mit Code 1", text)
        self.assertNotIn("zweite Notiz", text)

    def test_grund_gekuerzt(self):
        r = converter.ClipResult(name="C0002", status=converter.ST_FAILED, notes=["x" * 300])
        _k, _t, text = gui.completion_message(_summary(results=[r]))
        line = [l for l in text.splitlines() if "C0002" in l and "•" in l][0]
        self.assertLess(len(line), 140)
        self.assertTrue(line.endswith("…"))

    def test_ohne_notiz(self):
        _k, _t, text = gui.completion_message(_summary(results=[_res("C0003", converter.ST_FAILED)]))
        self.assertIn("  • C0003\n", text + "\n")


class TargetFilesTests(unittest.TestCase):
    def test_xdroot_has_files(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(gui.target_xdroot_has_files(d))
            (Path(d) / "XDROOT").mkdir()
            self.assertFalse(gui.target_xdroot_has_files(d))
            (Path(d) / "XDROOT" / "MEDIAPRO.XML").write_text("x")
            self.assertTrue(gui.target_xdroot_has_files(d))

    def test_target_is_xdroot(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(gui.target_is_xdroot(d))
            x = Path(d) / "XDROOT"
            (x / "Clip").mkdir(parents=True)
            self.assertTrue(gui.target_is_xdroot(str(x)))
            self.assertFalse(gui.target_is_xdroot(d))
            (Path(d) / "MEDIAPRO.XML").write_text("x")
            self.assertTrue(gui.target_is_xdroot(d))


class ProgressTextTests(unittest.TestCase):
    def test_eta_none_nur_beim_transkodieren(self):
        _c, t = gui.progress_texts("A", "Transkodiere 10/100", 0.1, None)
        self.assertIn("Restzeit wird berechnet", t)
        for phase in ("Analysiere Clip 1/4", "Pruefe vorhandenes Ziel 1/2", "Kopiere 1.00 MB / 2.00 MB"):
            _c, t = gui.progress_texts("A", phase, 0.1, None)
            self.assertNotIn("Restzeit", t, phase)

    def test_eta(self):
        _c, t = gui.progress_texts("A", "", 0.5, 75)
        self.assertIn("1:15", t)
        self.assertIn("50.0 %", t)


class ProgressLayoutTests(unittest.TestCase):
    """Beschriftung und Fortschrittsbalken dürfen sich bei hoher DPI-Skalierung nicht überlappen (ohne Display übersprungen)."""

    def _check(self, scaling):
        try:
            app = gui.App()
        except Exception as e:  # noqa: BLE001  (kein Display)
            self.skipTest(f"keine GUI möglich: {e}")
        orig = app.tk.call("tk", "scaling")  # prozessweit: muss zurückgesetzt werden, sonst verfälscht es spätere GUI-Tests
        try:
            app.attributes("-alpha", 0.0)  # unsichtbar, aber mit echter Fenstergröße (Geometrie wie im Betrieb)
            if scaling:
                app.tk.call("tk", "scaling", scaling)
            app.var_clip_label.set("Clip C0001 – " + "lange Beschriftung " * 8)
            app.var_total_label.set("Gesamt 42 % – noch etwa 1 h 20 min")
            app.update_idletasks()
            app.update()
            for lbl, pb in ((app.lbl_clip, app.pb_clip), (app.lbl_total, app.pb_total)):
                self.assertGreaterEqual(pb.winfo_y(), lbl.winfo_y() + lbl.winfo_height(), f"scaling={scaling}")
                self.assertLessEqual(lbl.winfo_x() + lbl.winfo_width(), app.winfo_width() + 1)
            self.assertGreaterEqual(app.lbl_total.winfo_y(), app.pb_clip.winfo_y() + app.pb_clip.winfo_height())
        finally:
            try:
                app.tk.call("tk", "scaling", orig)
            except Exception:  # noqa: BLE001
                pass
            try:
                app.after_cancel(app._after_id)
            except Exception:  # noqa: BLE001
                pass
            app.destroy()

    def test_default(self):
        self._check(None)

    def test_scaling_2_0(self):
        self._check(2.0)

    def test_scaling_2_67(self):
        self._check(2.67)


if __name__ == "__main__":
    unittest.main()
