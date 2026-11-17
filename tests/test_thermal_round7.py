"""Runde 6 der Spezifikation ("Eingreifen bei Hitze nur noch auf Wunsch") und "Fortschritt beim Verpacken" (17.11.2026).

Alles mit Attrappen: kein ffmpeg/raw2bmx, keine echte Lüfterschaltung, keine echten Medien.  Dateien für den
Verpackungs-Fortschritt entstehen nur unter ``_test_out``."""

import io
import os
import signal
import tempfile
import threading
import time
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

from tests.test_thermal_round6 import Ctl, PauseOnly, Rig
from xdroot_converter import cli, converter, gui, thermal, tools

M = thermal.Messung
OUT_ROOT = Path(__file__).resolve().parent.parent / "_test_out"


def off_cfg(**kw):
    return thermal.ThermalConfig.from_limits(throttle=False, **kw)


# --------------------------------------------------------------------------- Regler: Schalter aus
class ThrottleOffControllerTests(unittest.TestCase):
    def test_never_throttles_pauses_or_aborts_at_110_degrees(self):
        r = Rig(cfg=off_cfg())
        r.run(110, 1800, dt=5.0)  # 30 min bei 110 °C (360 Messungen)
        r.run(110, 3600, dt=60.0)  # und weitere Stunde mit grossen Abständen (länger als die Höchstdauer einer Pause)
        self.assertEqual(r.ctl.calls, [], "kein set_throttle")
        self.assertEqual(r.ctl.events, [], "keine Pause")
        self.assertFalse(r.c.paused)
        self.assertEqual(r.c.aborted, "")
        self.assertEqual(r.c.failed, "")
        self.assertEqual(r.c.stats.pauses, 0)
        self.assertEqual(r.c.stats.throttle_steps, 0)
        self.assertEqual(r.c.stats.max_cpu_c, 110.0)
        self.assertTrue(all(st in ("fan", "ok") for _t, st in r.statuses))
        self.assertFalse(any("gedrosselt" in t or "Pause" in t for t, _st in r.statuses))

    def test_fan_full_effect_check_and_restore_stay(self):
        r = Rig(cfg=off_cfg())
        r.run(90, 20)
        self.assertTrue(r.c.fan_full)
        self.assertEqual(r.prov.fulls, 1)
        r.run(60, 600)  # kühler: Lüfter bleibt bis zum Laufende voll
        self.assertTrue(r.c.fan_full)
        self.assertEqual(r.prov.restores, 0)
        r.c.finish()
        self.assertEqual(r.prov.restores, 1)
        self.assertFalse(r.c.fan_full)

    def test_works_with_pause_only_controller_and_without_any_controller(self):
        for ctl in (PauseOnly(), None):
            r = Rig(ctl=ctl if ctl is not None else Ctl(), cfg=off_cfg())
            if ctl is None:
                r.c.pause_ctl = None
            r.run(115, 300)
            self.assertFalse(r.c.paused)
            if ctl is not None:
                self.assertEqual(ctl.events, [])

    def test_start_line_names_the_state(self):
        r = Rig(cfg=off_cfg())
        line = next(tx for lv, tx in r.notes if tx.startswith("Temperaturüberwachung aktiv"))
        self.assertIn("Lüfter voll ab 85 °C (bleibt bis zum Laufende)", line)
        self.assertIn("kein Drosseln, keine Pause (die CPU regelt sich selbst)", line)
        self.assertNotIn("gedrosselt", line)
        r2 = Rig(cfg=thermal.ThermalConfig.from_limits(throttle=True))
        line2 = next(tx for lv, tx in r2.notes if tx.startswith("Temperaturüberwachung aktiv"))
        self.assertIn("ab 97 °C wird die Last gedrosselt, Pause als letzte Stufe", line2)

    def test_constant_guard_unchanged(self):
        # die Konstanz-Wache gilt unverändert (Quelle b, toter Festwert unter der Lüfter-Schwelle -> Ausfall)
        r = Rig(cfg=off_cfg())
        r.c.provider.fan = True
        for _ in range(thermal.CONSTANT_GUARD_COUNT + 2):
            r.now[0] += 5
            r.c.tick(M(60.0, (3000,), source="feature"))
        self.assertTrue(r.c.failed)
        self.assertEqual(r.ctl.calls[-1:], [1.0] if r.ctl.calls else [])  # nur das unbedingte Aufheben, nie eine Stufe

    def test_failure_of_measurement_still_handled(self):
        r = Rig(cfg=off_cfg())
        r.run(90, 20)
        for _ in range(3):
            r.fail(RuntimeError("weg"))
        self.assertTrue(r.c.failed)
        self.assertEqual(r.prov.restores, 1)  # Lüfter am Ausfall zurückgestellt

    def test_switch_on_behaves_like_round5(self):
        r = Rig(cfg=thermal.ThermalConfig.from_limits(throttle=True))
        r.run(99, 30)
        self.assertTrue(r.ctl.calls, "Stufe runter (set_throttle)")
        self.assertLess(r.ctl.calls[0], 1.0)
        r.run(99, 3600)  # dauerhaft heiss: am Ende der Stufen wird pausiert
        self.assertIn("pause", r.ctl.events)
        self.assertGreater(r.c.stats.pauses, 0)

    def test_dataclass_default_keeps_round5_behavior_for_direct_calls(self):
        self.assertTrue(thermal.ThermalConfig().throttle)
        self.assertTrue(thermal.ThermalConfig.from_limits().throttle)


# --------------------------------------------------------------------------- Konfiguration / Grenzen
class ConfigTests(unittest.TestCase):
    def test_switch_off_does_not_evaluate_or_check_pause_threshold(self):
        # Pausen-Schwelle unsinnig / unter der Lüfter-Schwelle: bei Schalter aus kein Fehler
        c = thermal.ThermalConfig.from_limits(99, 90, throttle=False)
        self.assertEqual(c.fan_on_c, 99.0)
        thermal.ThermalConfig.from_limits(85, 5, throttle=False)
        thermal.ThermalConfig.from_limits(85, float("nan"), throttle=False)
        for bad in ((99, 90), (85, 5)):
            with self.assertRaises(ValueError):
                thermal.ThermalConfig.from_limits(*bad, throttle=True)

    def test_fan_threshold_still_checked_with_switch_off(self):
        with self.assertRaises(ValueError):
            thermal.ThermalConfig.from_limits(-5, 97, throttle=False)

    def test_check_limits_without_pause(self):
        thermal.check_limits(99, None)  # nur die Lüfter-Schwelle
        with self.assertRaises(ValueError):
            thermal.check_limits(40, None)
        with self.assertRaises(ValueError):
            thermal.check_limits(99, 90)


# --------------------------------------------------------------------------- Converter: Durchreichen, Zusammenfassung
class OptionsPassThroughTests(unittest.TestCase):
    def _cfg_for(self, **kw):
        seen = {}

        class FakeSession:
            def __init__(self, cfg, *a, **k):
                seen["cfg"] = cfg

            def start(self):
                pass

        conv = converter.Converter(converter.Options(source="s", target="t", thermal="auto", **kw))
        with mock.patch.object(thermal, "ThermalSession", FakeSession):
            conv._thermal_start()
        return seen["cfg"]

    def test_default_is_off(self):
        self.assertFalse(converter.Options(source="s", target="t").thermal_throttle)
        self.assertFalse(self._cfg_for().throttle)

    def test_switch_is_passed_to_config(self):
        self.assertTrue(self._cfg_for(thermal_throttle=True, temp_pause=95).throttle)
        cfg = self._cfg_for(thermal_throttle=False, temp_fan=99, temp_pause=90)  # ungeprüft
        self.assertFalse(cfg.throttle)
        self.assertEqual(cfg.fan_on_c, 99.0)


class SummaryTests(unittest.TestCase):
    def test_max_temperature_without_throttle_and_without_pause(self):
        s = converter.RunSummary(max_cpu_c=98.0, thermal_throttle=False)
        t = s.text()
        self.assertIn("Höchste CPU-Temperatur 98 °C (Messwert des Geräts, kann über der Anzeige des Herstellers liegen)", t)
        self.assertNotIn("keine Pause nötig", t)

    def test_with_switch_on_text_as_before(self):
        s = converter.RunSummary(max_cpu_c=90.0, thermal_throttle=True)
        self.assertIn("Höchste gemessene CPU-Temperatur: 90 °C, keine Pause nötig", s.text())

    def test_gui_completion_window_names_max_temperature(self):
        s = converter.RunSummary(max_cpu_c=98.0, thermal_throttle=False)
        _kind, _title, msg = gui.completion_message(s)
        self.assertIn("Höchste CPU-Temperatur 98 °C", msg)
        self.assertIn("kann über der Anzeige des Herstellers liegen", msg)

    def test_run_transfers_max_temperature_and_flag(self):
        conv = converter.Converter(converter.Options(source="s", target="t", thermal="auto", thermal_throttle=False))
        sm = converter.RunSummary()
        conv._thermal_summary = sm

        class Sess:
            stats = thermal.ThermalStats(max_cpu_c=101.0)

            def stop(self):
                pass

        conv._thermal = Sess()
        conv._thermal_stop()
        self.assertEqual(sm.max_cpu_c, 101.0)
        self.assertFalse(sm.thermal_throttle)
        self.assertIn("Höchste CPU-Temperatur 101 °C", sm.text())


# --------------------------------------------------------------------------- CLI
class CliTests(unittest.TestCase):
    def parse(self, *a):
        return cli.build_parser().parse_args(["q", "z", *a])

    def _main(self, argv):
        captured = {}

        class FakeConv:
            def __init__(self, opts, rep, cancel):
                captured["opts"] = opts

            def run(self):
                return converter.RunSummary()

        saved = {n: signal.getsignal(getattr(signal, n)) for n in ("SIGINT", "SIGTERM") if hasattr(signal, n)}
        for n, h in saved.items():
            self.addCleanup(signal.signal, getattr(signal, n), h)
        out = io.StringIO()
        with mock.patch.object(converter, "Converter", FakeConv), mock.patch("sys.stdout", out):
            rc = cli.main(argv)
        return rc, captured["opts"], out.getvalue()

    def test_parsing(self):
        self.assertFalse(self.parse().thermal_throttle)
        self.assertTrue(self.parse("--thermal-throttle").thermal_throttle)
        a = self.parse("--thermal-throttle", "--temp-pause", "92")
        self.assertEqual((a.thermal_throttle, a.temp_pause), (True, 92.0))
        self.assertEqual(self.parse().temp_pause, 97.0)  # Standard bleibt

    def test_default_run_has_switch_off_and_no_hint(self):
        rc, o, out = self._main(["q", "z"])
        self.assertEqual((rc, o.thermal, o.thermal_throttle), (0, "auto", False))
        self.assertNotIn("Hinweis", out)

    def test_temp_pause_without_throttle_gives_hint_not_error(self):
        rc, o, out = self._main(["q", "z", "--temp-pause", "95"])
        self.assertEqual((rc, o.thermal_throttle, o.temp_pause), (0, False, 95.0))
        self.assertIn("Hinweis: --temp-pause wirkt nur zusammen mit --thermal-throttle", out)

    def test_hint_also_for_explicit_default_value(self):
        _rc, _o, out = self._main(["q", "z", "--temp-pause", "97"])
        self.assertIn("--thermal-throttle", out)

    def test_throttle_with_temp_pause_no_hint(self):
        rc, o, out = self._main(["q", "z", "--thermal-throttle", "--temp-pause", "95"])
        self.assertEqual((rc, o.thermal_throttle, o.temp_pause), (0, True, 95.0))
        self.assertNotIn("Hinweis", out)

    def test_thermal_off_switches_everything_off(self):
        rc, o, _out = self._main(["q", "z", "--thermal", "off", "--thermal-throttle"])
        self.assertEqual((o.thermal, o.thermal_throttle), ("off", False))

    def test_pause_limit_not_checked_without_throttle(self):
        rc, o, _out = self._main(["q", "z", "--temp-pause", "59"])  # ausserhalb 60-120, aber unbeachtlich
        self.assertEqual(rc, 0)
        rc, o, _out = self._main(["q", "z", "--temp-fan", "99", "--temp-pause", "90"])  # Verhältnis ungeprüft
        self.assertEqual((rc, o.temp_fan), (0, 99.0))

    def test_limits_still_checked_with_throttle_and_fan_always(self):
        for argv in (["--thermal-throttle", "--temp-pause", "59"], ["--thermal-throttle", "--temp-fan", "99", "--temp-pause", "90"],
                     ["--temp-fan", "40"]):
            with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as cm:
                cli.main(["q", "z", *argv])
            self.assertEqual(cm.exception.code, 2)

    def test_help_mentions_switch(self):
        h = cli.build_parser().format_help()
        self.assertIn("--thermal-throttle", h)
        self.assertIn("nur mit --thermal-throttle", h)


# --------------------------------------------------------------------------- GUI
class GuiParseTests(unittest.TestCase):
    def test_parse_temp_limits_without_pause(self):
        self.assertEqual(gui.parse_temp_limits("85", None), (85.0, None))
        with self.assertRaises(ValueError):
            gui.parse_temp_limits("40", None)
        self.assertEqual(gui.parse_temp_limits("85", "97"), (85.0, 97.0))
        with self.assertRaises(ValueError):
            gui.parse_temp_limits("99", "90")

    def test_hint_text(self):
        self.assertIn("Die CPU schützt sich selbst", gui.HINT_THERMAL)
        self.assertIn("unter Volllast abstürzt", gui.HINT_THERMAL)
        self.assertIn("10 °C über der Anzeige in Lenovo Vantage", gui.HINT_THERMAL)


class GuiFieldStateTests(unittest.TestCase):
    def setUp(self):
        try:
            self.app = gui.App()
        except Exception as e:  # noqa: BLE001  (kein Display)
            self.skipTest(f"keine GUI möglich: {e}")
        self.addCleanup(self.app.destroy)

    def state(self, w):
        return str(w.cget("state"))

    def test_defaults_and_field_states(self):
        a = self.app
        self.assertTrue(a.var_thermal_on.get())
        self.assertFalse(a.var_throttle_on.get())
        fan, pause, ck = a._temp_fields[0], a._pause_fields[0], a._throttle_fields[0]
        a._update_thermal_fields(True)
        self.assertEqual((self.state(fan), self.state(ck), self.state(pause)), ("normal", "normal", "disabled"))
        a.var_throttle_on.set(True)
        a._update_thermal_fields(True)
        self.assertEqual((self.state(fan), self.state(ck), self.state(pause)), ("normal", "normal", "normal"))
        a.var_thermal_on.set(False)  # Überwachung aus: alles aus, auch "Pause ab"
        a._update_thermal_fields(True)
        self.assertEqual((self.state(fan), self.state(ck), self.state(pause)), ("disabled", "disabled", "disabled"))
        a.var_thermal_on.set(True)
        a._update_thermal_fields(False)  # Lauf aktiv: Eingaben gesperrt
        self.assertEqual((self.state(fan), self.state(ck), self.state(pause)), ("disabled", "disabled", "disabled"))

    def test_hint_is_not_cut_off_at_min_height(self):
        a = self.app
        a.update()
        a._toggle_settings()
        a.update()
        minw, minh = a.minsize()
        a.geometry(f"{minw}x{minh}")
        for _ in range(3):
            a.update()
            time.sleep(0.1)
        lbl = a.lbl_th_hint
        self.assertGreaterEqual(lbl.winfo_height(), lbl.winfo_reqheight(), "Hinweis nicht abgeschnitten")
        h = a.winfo_height()
        bottom = lambda w: w.winfo_rooty() - a.winfo_rooty() + w.winfo_height()  # noqa: E731
        self.assertLessEqual(bottom(a.frm_settings), h)
        self.assertLessEqual(bottom(a.lbl_thermal), h)
        self.assertGreaterEqual(a.txt.winfo_height(), 78)


# --------------------------------------------------------------------------- Fortschritt beim Verpacken
class WrapFractionTests(unittest.TestCase):
    def test_fraction(self):
        f = converter.Converter.wrap_fraction
        self.assertEqual(f(0, 1000), 0.0)
        self.assertEqual(f(500, 1000), 0.5)
        self.assertEqual(f(5000, 1000), 0.99)  # auf 99 % begrenzt
        self.assertEqual(f(1000, 1000), 0.99)
        self.assertEqual(f(500, 0), 0.0)
        self.assertEqual(f(-3, 100), 0.0)


class Rep(converter.Reporter):
    def __init__(self):
        self.progs = []

    def progress(self, clip_name, clip_frac, total_frac, eta_s, text=""):
        self.progs.append((clip_frac, total_frac, text))


class WrapWatchTests(unittest.TestCase):
    def setUp(self):
        OUT_ROOT.mkdir(exist_ok=True)
        self._td = tempfile.TemporaryDirectory(dir=str(OUT_ROOT))
        self.addCleanup(self._td.cleanup)
        self.d = Path(self._td.name)
        self.rep = Rep()
        self.conv = converter.Converter(converter.Options(source="s", target="t"), self.rep)
        self.conv.WRAP_POLL_S = 0.02
        self.conv._frames_total = 100
        self.plan = converter.ClipPlan(mxf=Path("A.MXF"), stem="A", material=None)
        self.plan.frames = 10

    def _inputs(self):
        v, w = self.d / "v.h264", self.d / "a0.wav"
        v.write_bytes(b"x" * 700)
        w.write_bytes(b"y" * 300)
        return [v, w, self.d / "fehlt.bin"]  # eine fehlende Eingabe zählt 0

    def _wait_text(self, text, timeout=3.0):
        end = time.time() + timeout
        while time.time() < end:
            if any(t == text for _f, _g, t in self.rep.progs):
                return True
            time.sleep(0.01)
        return False

    def test_reports_size_ratio_capped_and_monotonic(self):
        out = self.d / "out.mxf"
        stop = threading.Event()
        th = threading.Thread(target=self.conv._wrap_watch, args=(self.plan, 10, out, self._inputs(), stop))
        th.start()
        try:
            time.sleep(0.1)
            self.assertEqual(self.rep.progs, [], "Datei existiert noch nicht: nichts melden, kein Fehler")
            out.write_bytes(b"m" * 630)
            self.assertTrue(self._wait_text("Verpacke MXF (raw2bmx) 63 %"))
            out.write_bytes(b"m" * 400)  # schrumpft (z. B. Neustart): nie rückwärts
            time.sleep(0.1)
            out.write_bytes(b"m" * 5000)
            self.assertTrue(self._wait_text("Verpacke MXF (raw2bmx) 99 %"))
        finally:
            stop.set()
            th.join(5)
        self.assertFalse(th.is_alive())
        fr = [f for f, _g, _t in self.rep.progs]
        self.assertEqual(fr, sorted(fr))
        self.assertTrue(all(0.92 <= f < 0.95 + 1e-9 for f in fr), fr)
        self.assertNotIn("Verpacke MXF (raw2bmx) 100 %", [t for _f, _g, t in self.rep.progs])
        # Gesamtfortschritt bleibt innerhalb des Clip-Anteils (Gewichtung unverändert)
        self.assertTrue(all(g <= 0.1 * 0.95 + 1e-9 for _f, g, _t in self.rep.progs))

    def test_errors_are_ignored(self):
        out = self.d / "out.mxf"
        out.write_bytes(b"m" * 500)
        stop = threading.Event()
        self.rep.progress = mock.Mock(side_effect=RuntimeError("Anzeige kaputt"))
        th = threading.Thread(target=self.conv._wrap_watch, args=(self.plan, 10, out, self._inputs(), stop))
        th.start()
        time.sleep(0.2)
        stop.set()
        th.join(5)
        self.assertFalse(th.is_alive())
        self.assertTrue(self.rep.progress.called)  # versucht, Fehler verschluckt

    def test_stat_error_is_ignored(self):
        stop = threading.Event()
        th = threading.Thread(target=self.conv._wrap_watch, args=(self.plan, 10, self.d / "x.mxf", [self.d / "n"], stop))
        th.start()
        time.sleep(0.1)
        stop.set()
        th.join(5)
        self.assertFalse(th.is_alive())
        self.assertEqual(self.rep.progs, [])


class WrapIntegrationTests(unittest.TestCase):
    """``_wrap`` mit einem Attrappen-``run_capture``: die Datei wächst, während der "Prozess" läuft."""

    def test_progress_during_fake_raw2bmx(self):
        from xdroot_converter import mxfinfo

        OUT_ROOT.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=str(OUT_ROOT)) as td:
            d = Path(td)
            v, w, out = d / "v.h264", d / "a0.wav", d / "out.mxf"
            v.write_bytes(b"x" * 800)
            w.write_bytes(b"y" * 200)

            def fake_run(args, **kw):
                self.assertEqual(Path(args[args.index("-o") + 1]), out)
                for n in (300, 600, 900):
                    out.write_bytes(b"m" * n)
                    time.sleep(0.15)
                return tools.ProcResult(0, "", "")

            info = mxfinfo.MxfInfo(mp_uid="AA", fp_uid="BB", start_tc="00:00:00:00", ident_modified="2025-01-01 10:00:00.000")
            info.tracks = [mxfinfo.MxfTrack(index=0, kind="Picture", descriptor={}),
                           mxfinfo.MxfTrack(index=1, kind="Sound", descriptor={"locked": "true", "audio_ref_level": "0"})]
            plan = converter.ClipPlan(mxf=Path("A.MXF"), stem="A", material=None, info=info)
            plan.frames = 10
            rep = Rep()
            conv = converter.Converter(converter.Options(source="s", target="t", sony_header=False), rep)
            conv.WRAP_POLL_S = 0.05
            conv._frames_total = 10
            with mock.patch.object(tools, "run_capture", fake_run), mock.patch.object(tools, "find_tool", lambda n: n):
                err = conv._wrap(plan, Fraction(50), 10, v, [w], out, None, None)
            self.assertEqual(err, "")
            texts = [t for _f, _g, t in rep.progs]
            self.assertTrue(any(t.startswith("Verpacke MXF (raw2bmx) ") and t.endswith(" %") for t in texts), texts)
            self.assertIn("Verpacke MXF (raw2bmx) 30 %", texts)
            self.assertTrue(all(f < 0.95 + 1e-9 for f, _g, _t in rep.progs))
            # nach dem Ende des Verpackens kommt nichts mehr nach
            n = len(rep.progs)
            time.sleep(0.2)
            self.assertEqual(len(rep.progs), n)
            self.assertFalse([t for t in threading.enumerate() if t.name == "wrap-progress"])


if __name__ == "__main__":
    unittest.main()
