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
        results = [_res(f"C{i}", converter.ST_FAILED) for i in range(13)]
        kind, _t, text = gui.completion_message(_summary(results=results))
        self.assertEqual(kind, "warning")
        self.assertIn("C9", text)
        self.assertNotIn("C10", text)
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


class ProgressTextTests(unittest.TestCase):
    def test_eta_none(self):
        _c, t = gui.progress_texts("A", "Analysiere Clip 1/4", 0.1, None)
        self.assertIn("Restzeit wird berechnet", t)

    def test_eta(self):
        _c, t = gui.progress_texts("A", "", 0.5, 75)
        self.assertIn("1:15", t)
        self.assertIn("50.0 %", t)


if __name__ == "__main__":
    unittest.main()
