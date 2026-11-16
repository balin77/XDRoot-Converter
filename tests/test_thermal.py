"""Tests der Temperaturueberwachung und des Schonmodus (Runde 3): Regler mit Attrappen-Anbieter, Legion-Anbieter mit
Attrappe fuer den PowerShell-Hilfsprozess, Hilfsprozess-Protokoll mit einem kleinen Python-Kindprozess (kein
PowerShell, keine echte Hardware, kein ffmpeg), Pause/Abbruch am CancelToken, CLI-Argumente, --threads."""

import atexit
import io
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from fractions import Fraction
from pathlib import Path
from unittest import mock

from xdroot_converter import cli, converter, thermal, tools

try:  # discover -s tests (Modul heisst test_run) bzw. Aufruf aus dem Projektordner
    from test_run import CODEC_L, RunTestBase, Rep, _REAL_TRANSCODE
except ImportError:  # pragma: no cover
    from tests.test_run import CODEC_L, RunTestBase, Rep, _REAL_TRANSCODE

PY = sys.executable

# Zustandsdateien der Tests liegen in einem Wegwerf-Ordner (nie im echten %LOCALAPPDATA%)
_STATE_ROOT = tempfile.mkdtemp(prefix="xdroot-fanstate-")


thermal.THERMAL_TEST_LOG_DIR = Path(_STATE_ROOT)  # thermal_test.txt nie in den echten Programmordner schreiben
atexit.register(shutil.rmtree, _STATE_ROOT, True)  # (nicht tearDownModule: test_thermal_fixes benutzt ihn mit)


def new_store():
    return thermal.FanStateStore(tempfile.mkdtemp(dir=_STATE_ROOT))


def wait_for(cond, timeout=10.0, step=0.01):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


# --------------------------------------------------------------------------- Attrappen
class FakeProvider(thermal.ThermalProvider):
    """Attrappen-Anbieter: ``temps`` wird der Reihe nach geliefert (ein Eintrag, der eine Exception ist, wird
    geworfen); nach dem Ende bleibt der letzte Wert.  Alle Aufrufe landen im gemeinsamen ``log``."""

    name = "Attrappe"

    def __init__(self, temps, log=None, fan_ok=True, fail_fan_full=False, fail_restore=0, fail_open=None,
                 rpms=None, fan_max=None):
        self.rpms = rpms  # None = konstant (3000, 3100); sonst Liste von Tupeln (letzter Wert bleibt)
        self.fan_max = fan_max
        self.temps = list(temps)
        self.log = log if log is not None else []
        self.fan_ok = fan_ok
        self.fail_fan_full = fail_fan_full
        self.fail_restore = fail_restore
        self.fail_open = fail_open
        self.closed = False
        self.i = 0

    def open(self):
        if self.fail_open:
            raise self.fail_open
        self.log.append("open")

    def measure(self):
        v = self.temps[min(self.i, len(self.temps) - 1)]
        self.i += 1
        if isinstance(v, BaseException):
            raise v
        self.log.append(("measure", v))
        rpm = (3000, 3100) if self.rpms is None else self.rpms[min(self.i - 1, len(self.rpms) - 1)]
        return thermal.Messung(v, rpm, self.fan_max)

    def fan_control_info(self):
        return self.fan_ok, "Attrappe-Luefter"

    def fan_full(self):
        self.log.append("fan_full")
        if self.fail_fan_full:
            raise thermal.ProviderError("Schreiben fehlgeschlagen")

    def fan_restore(self):
        self.log.append("fan_restore")
        if self.fail_restore > 0:
            self.fail_restore -= 1
            raise thermal.ProviderError("Zurueckschreiben fehlgeschlagen")

    def close(self):
        self.closed = True
        self.log.append("close")


class FakePause:
    def __init__(self, log, result=True):
        self.log = log
        self.result = result

    def pause(self):
        self.log.append("pause")
        return self.result

    def resume(self):
        self.log.append("resume")
        return True


def ops(log):
    """Nur die Schalt-/Pausenaufrufe (ohne Messungen) in der Reihenfolge."""
    return [e for e in log if not isinstance(e, tuple) and e not in ("open", "close")]


class Notes:
    def __init__(self):
        self.items = []
        self.states = []

    def notify(self, level, text):
        self.items.append((level, text))

    def status(self, text, state):
        self.states.append((text, state))

    def text(self, level=None):
        return "\n".join(t for l, t in self.items if level is None or l == level)


def make_ctl(temps, cfg=None, pause_result=True, **pkw):
    log = []
    prov = FakeProvider(temps, log, **pkw)
    notes = Notes()
    clock = [0.0]
    ctl = thermal.ThermalController(prov, cfg or thermal.ThermalConfig(), FakePause(log, pause_result),
                                    notes.notify, notes.status, clock=lambda: clock[0])
    prov.open()
    ctl.setup()
    return ctl, prov, log, notes, clock


def run_ticks(ctl, n):
    for _ in range(n):
        ctl.tick()


# --------------------------------------------------------------------------- Konfiguration
class ConfigTests(unittest.TestCase):
    def test_defaults_match_spec(self):
        c = thermal.ThermalConfig()
        # Runde 5: Die Rückschalt-Schwelle der Lüfter (fan_off_c) entfällt, "Lüfter voll" bleibt bis zum Laufende
        self.assertEqual((c.fan_on_c, c.pause_c, c.resume_c), (85, 97, 85))
        self.assertFalse(hasattr(c, "fan_off_c"))
        c.validate()

    def test_from_limits_derives_hysteresis(self):
        c = thermal.ThermalConfig.from_limits(80, 95)
        self.assertEqual((c.fan_on_c, c.pause_c, c.resume_c), (80, 95, 83))

    def test_invalid_limits(self):
        # (5, 90) entfällt: abgelehnt wurde es nur, weil die frühere Rückschwelle 5-10 < 0 unplausibel war; der erlaubte
        # Bereich 50-110 °C wird von check_limits (CLI/GUI) geprüft, siehe test_thermal_fixes.
        for fan, pause in ((97, 97), (99, 90), (0, 90), (85, 130)):
            with self.assertRaises(ValueError, msg=(fan, pause)):
                thermal.ThermalConfig.from_limits(fan, pause)

    def test_plausible(self):
        for t in (1, 40.5, 125):
            self.assertTrue(thermal.plausible(t), t)
        for t in (0, -3, 125.1, float("nan"), None, "x"):
            self.assertFalse(thermal.plausible(t), t)

    def test_status_text(self):
        m = thermal.Messung(87, (4800, 5100))
        self.assertEqual(thermal.format_status(m, True), "CPU 87 °C · Lüfter 4800/5100 U/min · Lüfter voll")
        self.assertEqual(thermal.format_status(thermal.Messung(70), False), "CPU 70 °C")
        self.assertEqual(thermal.format_status(thermal.Messung(98), True, True),
                         "Pause wegen Temperatur (98 °C), weiter unter 85 °C")


# --------------------------------------------------------------------------- Regler
class ControllerTests(unittest.TestCase):
    def test_fan_hold_and_confirmation(self):
        # Runde 5: 2 Messungen in Folge >= 85 -> voll; danach BLEIBT es voll (kein Zurückschalten mehr bei Abkühlung),
        # zurückgestellt wird erst am Laufende (finish).  (Früher: 3x < 75 -> "fan_restore" mitten im Lauf.)
        ctl, prov, log, notes, _ = make_ctl([80, 86, 86, 80, 80, 74, 74, 74, 74, 60, 60])
        run_ticks(ctl, 3)
        self.assertEqual(ops(log), ["fan_full"])
        self.assertTrue(ctl.fan_full)
        run_ticks(ctl, 8)
        self.assertEqual(ops(log), ["fan_full"])  # auch nach langer Abkühlung kein Zurückschalten
        self.assertTrue(ctl.fan_full)
        ctl.finish()
        self.assertEqual(ops(log), ["fan_full", "fan_restore"])
        self.assertFalse(ctl.fan_full)

    def test_single_outlier_switches_nothing(self):
        ctl, prov, log, notes, _ = make_ctl([70, 99, 70, 99, 70, 70])
        run_ticks(ctl, 6)
        self.assertEqual(ops(log), [])
        self.assertEqual(ctl.stats.pauses, 0)
        self.assertEqual(ctl.stats.max_cpu_c, 99)

    def test_pause_and_resume_order(self):
        ctl, prov, log, notes, clock = make_ctl([98, 98, 90, 90, 90, 80, 80, 80])
        run_ticks(ctl, 1)
        self.assertEqual(ops(log), [])
        run_ticks(ctl, 1)
        self.assertEqual(ops(log), ["pause", "fan_full"])  # waehrend der Pause immer Luefter voll
        self.assertTrue(ctl.paused)
        clock[0] = 5.0
        run_ticks(ctl, 3)  # 90 ist nicht unter 85: bleibt pausiert
        self.assertTrue(ctl.paused)
        run_ticks(ctl, 2)
        self.assertTrue(ctl.paused)
        clock[0] = 20.0
        run_ticks(ctl, 1)  # dritte Messung unter 85
        self.assertFalse(ctl.paused)
        self.assertEqual(ops(log), ["pause", "fan_full", "resume"])
        self.assertEqual((ctl.stats.pauses, ctl.stats.pause_seconds), (1, 20.0))
        self.assertIn("Pause wegen Temperatur", notes.text("warn"))
        self.assertIn("Pause beendet", notes.text("info"))
        ctl.finish()
        self.assertEqual(ops(log), ["pause", "fan_full", "resume", "fan_restore"])  # Luefter erst nach dem Fortsetzen

    def test_pause_without_fan_control(self):
        ctl, prov, log, notes, _ = make_ctl([99, 99, 60, 60, 60], fan_ok=False)
        run_ticks(ctl, 5)
        self.assertEqual(ops(log), ["pause", "resume"])  # Pause wirkt auch ohne Luefterschaltung

    def test_pause_refused_when_cancelled(self):
        ctl, prov, log, notes, _ = make_ctl([99, 99, 99], pause_result=False)
        run_ticks(ctl, 3)
        self.assertFalse(ctl.paused)
        self.assertEqual(ctl.stats.pauses, 0)

    def test_status_states(self):
        ctl, prov, log, notes, _ = make_ctl([70, 90, 90, 99, 99])
        run_ticks(ctl, 5)
        self.assertEqual([s for _t, s in notes.states], ["ok", "ok", "fan", "fan", "pause"])
        self.assertIn("Pause wegen Temperatur (99 °C)", notes.states[-1][0])

    # ---- unplausible Werte
    def test_implausible_values_in_a_row_fail(self):
        ctl, prov, log, notes, _ = make_ctl([0, -5, 200, 90])
        run_ticks(ctl, 2)
        self.assertEqual(ctl.failed, "")
        run_ticks(ctl, 1)
        self.assertIn("unbrauchbare Werte", ctl.failed)
        self.assertEqual(ops(log), [])
        self.assertEqual(notes.states[-1][1], "unavailable")
        run_ticks(ctl, 1)  # nach dem Ausfall wird nichts mehr gemessen
        self.assertEqual(prov.i, 3)

    def test_single_implausible_value_is_ignored(self):
        ctl, prov, log, notes, _ = make_ctl([90, 0, 90, 130, 90])
        run_ticks(ctl, 5)
        self.assertEqual(ctl.failed, "")
        self.assertTrue(ctl.fan_full)  # 90, (0 ignoriert), 90: der Zaehler laeuft ueber die ungueltige Messung hinweg

    def test_implausible_while_paused_resumes_and_restores(self):
        ctl, prov, log, notes, _ = make_ctl([99, 99, 0, 0, 0])
        run_ticks(ctl, 5)
        self.assertTrue(ctl.failed)
        self.assertEqual(ops(log), ["pause", "fan_full", "resume", "fan_restore"])
        self.assertFalse(ctl.paused)

    # ---- Ausfall des Anbieters
    def test_provider_failure_midrun_restores_everything(self):
        ctl, prov, log, notes, _ = make_ctl([90, 90, thermal.ProviderError("weg")])
        run_ticks(ctl, 3)
        self.assertIn("Temperaturmessung ist ausgefallen", ctl.failed)
        self.assertEqual(ops(log), ["fan_full", "fan_restore"])
        self.assertIn("ausgefallen", notes.text("warn"))
        self.assertIn("läuft ohne Überwachung weiter", notes.text("warn"))
        ctl.finish()
        self.assertTrue(prov.closed)
        self.assertEqual(ops(log).count("fan_restore"), 1)  # kein zweites Zurueckschreiben

    def test_provider_failure_while_paused_resumes(self):
        ctl, prov, log, notes, _ = make_ctl([99, 99, RuntimeError("kaputt")])
        run_ticks(ctl, 3)
        self.assertEqual(ops(log), ["pause", "fan_full", "resume", "fan_restore"])

    def test_unexpected_exception_type_in_measure_is_contained(self):
        ctl, prov, log, notes, _ = make_ctl([ValueError("x")])
        ctl.tick()
        self.assertTrue(ctl.failed)

    def test_fan_switch_failure_disables_switching_but_not_pause(self):
        ctl, prov, log, notes, _ = make_ctl([99, 99, 99, 60, 60, 60], fail_fan_full=True)
        run_ticks(ctl, 6)
        self.assertFalse(ctl.fan_ok)
        self.assertEqual(ops(log), ["pause", "fan_full", "fan_restore", "resume"])  # Zustand unbekannt -> zurueckgesetzt
        self.assertIn("Lüfter konnten nicht auf voll geschaltet werden", notes.text("warn"))
        self.assertEqual(ctl.failed, "")

    def test_restore_failure_is_reported_never_raised(self):
        ctl, prov, log, notes, _ = make_ctl([90, 90], fail_restore=5)
        run_ticks(ctl, 2)
        ctl.finish()  # wirft nicht
        self.assertIn("NICHT wiederhergestellt", notes.text("warn"))
        self.assertTrue(prov.closed)

    def test_notify_exception_does_not_break_controller(self):
        log = []
        prov = FakeProvider([90, 90], log)
        ctl = thermal.ThermalController(prov, None, FakePause(log), notify=lambda *a: 1 / 0, status=lambda *a: 1 / 0)
        run_ticks(ctl, 2)
        self.assertTrue(ctl.fan_full is False and ctl.failed == "")  # fan_ok erst durch setup()
        ctl.setup()
        run_ticks(ctl, 2)
        self.assertTrue(ctl.fan_full)

    # ---- Wiederherstellen in allen Endpfaden
    def test_finish_paths_restore_fan(self):
        for end in ("normal", "paused", "failed"):
            with self.subTest(end=end):
                ctl, prov, log, notes, _ = make_ctl([99, 99] if end == "paused" else [90, 90])
                run_ticks(ctl, 2)
                if end == "failed":
                    prov.temps = [RuntimeError("x")]
                    prov.i = 0
                    ctl.tick()
                ctl.finish()
                ctl.finish()  # idempotent
                self.assertEqual(ops(log).count("fan_restore"), 1)
                self.assertFalse(ctl.paused)
                self.assertTrue(prov.closed)
                if end == "paused":
                    self.assertLess(ops(log).index("resume"), ops(log).index("fan_restore"))
                ctl.tick()  # nach finish() passiert nichts mehr
                self.assertEqual(ops(log).count("fan_full"), 1)

    def test_nothing_to_restore_when_never_switched(self):
        ctl, prov, log, notes, _ = make_ctl([60, 60])
        run_ticks(ctl, 2)
        ctl.finish()
        self.assertEqual(ops(log), [])
        self.assertTrue(prov.closed)

    def test_finish_while_tick_is_blocked_in_measure(self):
        """Haengt die Messung (Hilfsprozess), darf finish() des Hauptthreads trotzdem alles herstellen."""
        log = []
        gate = threading.Event()

        class Slow(FakeProvider):
            def measure(self):
                if self.i >= 2:
                    gate.wait(5)
                return super().measure()

        prov = Slow([90, 90, 90], log)
        ctl = thermal.ThermalController(prov, None, FakePause(log))
        ctl.setup()
        run_ticks(ctl, 2)
        t = threading.Thread(target=ctl.tick)
        t.start()
        time.sleep(0.1)
        ctl.finish()
        self.assertEqual(ops(log).count("fan_restore"), 1)
        gate.set()
        t.join(5)
        self.assertEqual(ops(log).count("fan_restore"), 1)  # die freigegebene Messung schaltet nichts mehr


# --------------------------------------------------------------------------- Sitzung
class SessionTests(unittest.TestCase):
    def test_unavailable_reports_reason_and_never_raises(self):
        notes = Notes()
        sess = thermal.ThermalSession(
            thermal.ThermalConfig(), FakePause([]), notes.notify, notes.status,
            provider_factory=lambda: FakeProvider([1], fail_open=thermal.ProviderUnavailable("Administratorrechte nötig", "admin")),
            interval_s=0.01)
        sess.start()
        sess.stop()
        self.assertIsNone(sess.controller)
        self.assertEqual(notes.states[-1][1], "admin")
        self.assertIn("Administratorrechte nötig", notes.states[-1][0])
        self.assertIn("Die Umwandlung läuft unverändert", notes.text("info"))
        self.assertIn("Administratorrechte", sess.stats.unavailable_reason)

    def test_factory_exception_is_contained(self):
        notes = Notes()

        def boom():
            raise RuntimeError("Fabrik kaputt")

        sess = thermal.ThermalSession(thermal.ThermalConfig(), FakePause([]), notes.notify, notes.status,
                                      provider_factory=boom, interval_s=0.01)
        sess.start()
        sess.stop()
        self.assertEqual(notes.states[-1][1], "unavailable")

    def test_implausible_first_measurement_is_unavailable(self):
        av = thermal.check_availability(lambda: FakeProvider([0]))
        self.assertFalse(av.ok)
        self.assertEqual(av.code, "implausible")

    def test_runs_pauses_and_restores_on_stop(self):
        log = []
        notes = Notes()
        prov = FakeProvider([99, 99] + [99] * 3, log)
        sess = thermal.ThermalSession(thermal.ThermalConfig(), FakePause(log), notes.notify, notes.status,
                                      provider_factory=lambda: prov, interval_s=0.01)
        sess.start()
        self.assertTrue(wait_for(lambda: "pause" in log))
        sess.stop()
        o = ops(log)
        self.assertEqual(o[:2], ["pause", "fan_full"])
        self.assertEqual(o[-2:], ["resume", "fan_restore"])
        self.assertTrue(prov.closed)
        self.assertEqual(sess.stats.pauses, 1)

    def test_cancel_during_pause_resumes_before_kill_and_restores_fan(self):
        events = []
        proc = mock.Mock()
        proc.poll.return_value = None
        token = tools.CancelToken()
        with mock.patch.object(tools, "suspend_process", lambda p: events.append("suspend") or True), \
                mock.patch.object(tools, "resume_process", lambda p: events.append("resume") or True), \
                mock.patch.object(tools, "kill_process", lambda p: events.append("kill")):
            token.register(proc)
            log = []
            prov = FakeProvider([99], log)
            sess = thermal.ThermalSession(thermal.ThermalConfig(), token, None, None,
                                          provider_factory=lambda: prov, interval_s=0.01)
            sess.start()
            self.assertTrue(wait_for(lambda: token.paused))
            self.assertTrue(wait_for(lambda: "fan_full" in log))
            token.cancel()  # Abbrechen waehrend der Pause: sofort fortsetzen, dann beenden
            self.assertEqual(events, ["suspend", "resume", "kill"])
            self.assertFalse(token.paused)
            sess.stop()
        self.assertEqual(ops(log).count("fan_restore"), 1)
        self.assertEqual(sess.stats.pauses, 1)
        self.assertTrue(prov.closed)

    def test_stop_when_thread_never_finishes_still_restores(self):
        log = []
        gate = threading.Event()

        class Hang(FakeProvider):
            def measure(self):
                if self.i >= 2:
                    gate.wait(10)
                return super().measure()

        prov = Hang([90], log)
        sess = thermal.ThermalSession(thermal.ThermalConfig(), FakePause(log), None, None,
                                      provider_factory=lambda: prov, interval_s=0.01, stop_wait_s=0.3)
        sess.start()
        self.assertTrue(wait_for(lambda: "fan_full" in log))
        t0 = time.monotonic()
        sess.stop()
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(ops(log).count("fan_restore"), 1)
        gate.set()


# --------------------------------------------------------------------------- CancelToken: Pause / Abbruch
class TokenPauseTests(unittest.TestCase):
    def test_pause_suspends_registered_and_later_registered(self):
        events = []
        p1, p2 = mock.Mock(name="p1"), mock.Mock(name="p2")
        token = tools.CancelToken()
        with mock.patch.object(tools, "suspend_process", lambda p: events.append(("suspend", p)) or True), \
                mock.patch.object(tools, "resume_process", lambda p: events.append(("resume", p)) or True):
            token.register(p1)
            self.assertTrue(token.pause())
            self.assertFalse(token.pause())  # schon pausiert
            token.register(p2)  # waehrend der Pause gestartet: ebenfalls angehalten
            self.assertEqual(events, [("suspend", p1), ("suspend", p2)])
            self.assertTrue(token.resume())
            self.assertFalse(token.resume())
            self.assertEqual({e for e in events[2:]}, {("resume", p1), ("resume", p2)})
            self.assertFalse(token.paused)

    def test_cancel_forbids_further_pause(self):
        token = tools.CancelToken()
        token.cancel()
        self.assertFalse(token.pause())

    def test_paused_time_accumulates(self):
        token = tools.CancelToken()
        self.assertEqual(token.paused_time(), 0.0)
        with mock.patch.object(tools, "suspend_process", lambda p: True), \
                mock.patch.object(tools, "resume_process", lambda p: True):
            token.register(mock.Mock())
            self.assertTrue(token.pause())
            time.sleep(0.2)
            self.assertGreaterEqual(token.paused_time(), 0.19)  # laufende Pause zaehlt mit
            token.resume()
        t = token.paused_time()
        time.sleep(0.1)
        self.assertAlmostEqual(token.paused_time(), t, places=2)

    def test_run_capture_timeout_ignores_paused_time(self):
        """Zeitueberwachung: ein pausierter Prozess ist nicht 'haengend'."""
        token = tools.CancelToken()
        result = {}

        def work():
            try:
                tools.run_capture([PY, "-c", "import time; time.sleep(30)"], cancel=token, timeout=1.0)
                result["r"] = "ende"
            except tools.Cancelled:
                result["r"] = "cancelled"

        with mock.patch.object(tools, "suspend_process", lambda p: True), mock.patch.object(tools, "resume_process", lambda p: True):
            t = threading.Thread(target=work)
            t.start()
            time.sleep(0.3)
            token.pause()
            time.sleep(2.0)  # laenger als das Zeitlimit von 1 s
            self.assertTrue(t.is_alive(), "Zeitlimit hat waehrend der Pause zugeschlagen")
            token.resume()
            token.cancel()
            t.join(15)
        self.assertEqual(result.get("r"), "cancelled")

    def test_real_suspend_and_resume_of_a_child_process(self):
        """Echtes Anhalten/Fortsetzen eines (leichten) Python-Kindprozesses, kein ffmpeg."""
        code = "import time\nwhile True:\n    print('x', flush=True)\n    time.sleep(0.05)\n"
        token = tools.CancelToken()
        lines = []
        t = threading.Thread(
            target=lambda: _swallow(lambda: tools.run_capture([PY, "-c", code], cancel=token, on_stdout_line=lines.append)))
        t.start()
        try:
            self.assertTrue(wait_for(lambda: len(lines) >= 3))
            self.assertTrue(token.pause())
            time.sleep(0.3)  # laufende Zeilen verklingen lassen
            n = len(lines)
            time.sleep(0.8)
            self.assertEqual(len(lines), n, "Prozess lief waehrend der Pause weiter")
            self.assertTrue(token.resume())
            self.assertTrue(wait_for(lambda: len(lines) > n + 2), "Prozess lief nach dem Fortsetzen nicht weiter")
        finally:
            token.cancel()
            t.join(15)
        self.assertFalse(t.is_alive())

    def test_cancel_while_really_suspended_ends_promptly(self):
        token = tools.CancelToken()
        t = threading.Thread(target=lambda: _swallow(
            lambda: tools.run_capture([PY, "-c", "import time; time.sleep(60)"], cancel=token)))
        t.start()
        time.sleep(0.5)
        token.pause()
        time.sleep(0.3)
        t0 = time.monotonic()
        token.cancel()
        t.join(15)
        self.assertFalse(t.is_alive())
        self.assertLess(time.monotonic() - t0, 10)


def _swallow(fn):
    try:
        fn()
    except tools.Cancelled:
        pass


# --------------------------------------------------------------------------- PowerShell-Hilfsprozess (Protokoll)
FAKE_SHELL = r'''
import sys, time
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    rid, _, cmd = line.partition(" ")
    if cmd == "EXIT":
        break
    if cmd == "PING":
        print(rid + " OK pong", flush=True)
    elif cmd.startswith("CALL OK"):
        print(rid + " OK Data=42", flush=True)
    elif cmd.startswith("CALL DENIED"):
        print(rid + " ERR AccessDenied|Zugriff verweigert", flush=True)
    elif cmd.startswith("CALL JUNK"):
        print("Banner ohne id", flush=True)
        print("0 OK Data=1", flush=True)
        print(rid + " OK Data=7", flush=True)
    elif cmd.startswith("CALL HANG"):
        time.sleep(60)
    elif cmd.startswith("CALL DIE"):
        sys.exit(3)
'''


class HelperTests(unittest.TestCase):
    def make(self, **kw):
        h = thermal.PowerShellHelper(argv=[PY, "-u", "-c", FAKE_SHELL], startup_timeout=20, timeout=1.0, **kw)
        self.addCleanup(h.close, 0.0)
        return h

    def test_ping_call_and_remote_error(self):
        h = self.make()
        h.start()
        self.assertTrue(h.alive)
        self.assertEqual(h.call("CALL OK x"), "Data=42")
        with self.assertRaises(thermal.HelperRemoteError) as cm:
            h.call("CALL DENIED x")
        self.assertEqual(cm.exception.code, "AccessDenied")
        self.assertIn("verweigert", cm.exception.message)
        self.assertEqual(h.call("CALL JUNK x"), "Data=7")  # fremde/alte Zeilen werden uebersprungen
        self.assertEqual(h.call("CALL OK y"), "Data=42")  # nach einem Fehler weiter nutzbar

    def test_timeout_kills_process_and_marks_provider_failed(self):
        h = self.make()
        h.start()
        proc = h._proc
        t0 = time.monotonic()
        with self.assertRaises(thermal.HelperTimeout):
            h.call("CALL HANG x", timeout=0.5)
        self.assertLess(time.monotonic() - t0, 5)
        proc.wait(timeout=10)  # Prozess wurde beendet
        self.assertIsNotNone(proc.returncode)
        self.assertFalse(h.alive)
        with self.assertRaises(thermal.HelperError):
            h.call("CALL OK x")

    def test_process_dies(self):
        h = self.make()
        h.start()
        with self.assertRaises(thermal.HelperError):
            h.call("CALL DIE x")
        with self.assertRaises(thermal.HelperError):
            h.call("CALL OK x")

    def test_close_ends_process_and_is_repeatable(self):
        h = self.make()
        h.start()
        proc = h._proc
        h.close()
        h.close()
        self.assertIsNotNone(proc.poll())

    def test_close_kills_process_that_ignores_exit(self):
        h = thermal.PowerShellHelper(argv=[PY, "-u", "-c", "import time\nprint('1 OK pong',flush=True)\ntime.sleep(60)"],
                                     startup_timeout=20, timeout=1.0)
        # (die Attrappe antwortet nur auf die erste Zeile, ignoriert EXIT)
        h.start()
        proc = h._proc
        t0 = time.monotonic()
        h.close(grace=0.3)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertIsNotNone(proc.poll())

    def test_start_failures(self):
        h = thermal.PowerShellHelper(argv=["/gibt/es/nicht.exe"])
        with self.assertRaises(thermal.HelperError):
            h.start()
        h2 = thermal.PowerShellHelper(argv=[PY, "-c", "pass"], startup_timeout=5)  # beendet sich sofort
        with self.assertRaises(thermal.HelperError):
            h2.start()
        h3 = thermal.PowerShellHelper(argv=[PY, "-c", "import time; time.sleep(60)"], startup_timeout=0.5)
        with self.assertRaises(thermal.HelperTimeout):  # PING bleibt unbeantwortet
            h3.start()
        self.assertFalse(h3.alive)
        h3._proc.wait(timeout=10)  # und der Prozess ist beendet, nicht haengen geblieben

    def test_popen_gets_no_window_flag_and_pipes(self):
        seen = {}

        class P:
            pid = 1

            def __init__(self, argv, **kw):
                seen.update(kw)
                self.stdin = io.BytesIO()
                self.stdout = io.BytesIO(b"1 OK pong\n")
                self.stderr = None

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

            def kill(self):
                pass

        h = thermal.PowerShellHelper(argv=["x"], startup_timeout=5, popen=P)
        h.start()
        self.assertEqual(seen["creationflags"], tools.CREATE_NO_WINDOW)
        self.assertIs(seen["stdin"], subprocess.PIPE)
        self.assertIs(seen["stdout"], subprocess.PIPE)
        self.assertIs(seen["stderr"], subprocess.DEVNULL)
        h.close()

    def test_powershell_argv_is_hidden_noninteractive(self):
        d = Path(tempfile.mkdtemp(dir=_STATE_ROOT))
        exe = d / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        exe.parent.mkdir(parents=True)
        exe.write_bytes(b"")
        with mock.patch.object(thermal, "_system_directory", lambda: str(d)):
            a = thermal.powershell_argv()
        self.assertEqual(a[0], str(exe))
        for flag in ("-NoProfile", "-NonInteractive", "-EncodedCommand"):
            self.assertIn(flag, a)
        import base64
        script = base64.b64decode(a[-1]).decode("utf-16-le")
        self.assertIn("LENOVO_", script)
        self.assertIn('Import-Module "$PSHOME\\Modules\\CimCmdlets"', script)  # Modul ausdruecklich von dort laden
        self.assertNotIn("Set-", script.replace("Set-StrictMode", ""))  # das feste Skript schaltet selbst nichts


# --------------------------------------------------------------------------- Lenovo-Legion-Anbieter (Attrappe fuer den Hilfsprozess)
class FakeLegionHelper:
    """Simuliert den PowerShell-Hilfsprozess samt WMI.  ``feature``/``smart`` = Startwerte (None = Methode scheitert)."""

    def __init__(self, cpu=60, fan1=2000, fan2=2100, fan_max=5500, feature=0, smart=2, denied=None, ignore_writes=False,
                 start_error=None, die_on=None, boost=False, world=None):
        self.cpu, self.fan1, self.fan2, self.fan_max = cpu, fan1, fan2, fan_max
        self.feature, self.smart = feature, smart
        self.denied = denied  # (code, text) -> jeder Aufruf scheitert so
        self.ignore_writes = ignore_writes
        self.start_error = start_error
        self.die_on = die_on
        self.boost = boost  # Luefterdrehzahl verdoppelt sich, wenn "voll" geschaltet ist
        self.on_set = None  # Hook vor jedem SetFeatureValue
        self.calls = []
        self.closed = False
        self.started = False

    def start(self):
        if self.start_error:
            raise self.start_error
        self.started = True

    def close(self, grace=2.0):
        self.closed = True

    def call(self, line, timeout=None, lock_timeout=None):
        self.calls.append(line)
        t = line.split()
        assert t[0] == "CALL"
        cls, method = t[1], t[2]
        params = dict(x.split("=") for x in t[3:])
        if self.die_on and method == self.die_on:
            raise thermal.HelperTimeout("keine Antwort")
        if self.denied:
            raise thermal.HelperRemoteError(*self.denied)
        if method == "GetCPUTemp":
            return f"Data={self.cpu}"
        mult = 2 if (self.boost and self.feature == 1) else 1
        if method == "GetFan1Speed":
            return f"Data={self.fan1 * mult}"
        if method == "GetFan2Speed":
            return f"Data={self.fan2 * mult}"
        if method == "GetFanMaxSpeed":
            return f"Data={self.fan_max}"
        if method == "GetFeatureValue":
            if "value" in params:  # laut Get-CimClass ist 'value' bei GetFeatureValue eine AUSGABE
                raise thermal.HelperRemoteError("InvalidParameter", "value ist keine Eingabe von GetFeatureValue")
            if self.feature is None:
                raise thermal.HelperRemoteError("NotSupported", "Feature unbekannt")
            return f"IDs={params['IDs']} value={self.feature}"
        if method == "SetFeatureValue":
            if self.on_set:
                self.on_set()
            if not self.ignore_writes:
                self.feature = int(params["value"])
            return "ReturnValue=0"
        if method == "GetSmartFanMode":
            if self.smart is None:
                raise thermal.HelperRemoteError("NotSupported", "SmartFan unbekannt")
            return f"Data={self.smart}"
        if method == "SetSmartFanMode":
            raise AssertionError("SetSmartFanMode darf NIE aufgerufen werden (Leistungsmodus = mehr Waerme)")
        raise thermal.HelperRemoteError("InvalidMethod", method)

    def writes(self):
        return [c for c in self.calls if " Set" in c]


def legion(helper, **kw):
    kw.setdefault("state_store", new_store())
    return thermal.LegionProvider(helper_factory=lambda: helper, platform="win32", **kw)


class LegionProviderTests(unittest.TestCase):
    def assertUnavailable(self, prov, code, text):
        with self.assertRaises(thermal.ProviderUnavailable) as cm:
            prov.open()
        self.assertEqual(cm.exception.code, code)
        self.assertIn(text, cm.exception.reason)

    def test_without_admin_rights_is_clean_unavailable_and_never_writes(self):
        for denied in (("AccessDenied", "Access denied"), ("", "Zugriff verweigert"), ("", "HRESULT 0x80041003")):
            h = FakeLegionHelper(denied=denied)
            self.assertUnavailable(legion(h), "admin", "Administratorrechte nötig")
            self.assertEqual(h.writes(), [], "ohne Rechte darf nichts geschrieben werden")
            self.assertTrue(h.closed)  # Hilfsprozess wieder beendet

    def test_availability_text_without_rights(self):
        av = thermal.check_availability(lambda: legion(FakeLegionHelper(denied=("AccessDenied", "x"))))
        self.assertFalse(av.ok)
        self.assertEqual(av.code, "admin")
        self.assertIn("Administratorrechte nötig", av.reason)
        self.assertIsNone(av.provider)

    def test_no_legion_device(self):
        h = FakeLegionHelper(denied=("InvalidClass", "Ungueltige Klasse"))
        self.assertUnavailable(legion(h), "nodevice", "Kein Lenovo-Legion-Gerät")
        h = FakeLegionHelper(denied=("", "InvalidClass: keine Instanz von LENOVO_GAMEZONE_DATA"))
        self.assertUnavailable(legion(h), "nodevice", "Kein Lenovo-Legion-Gerät")

    def test_other_error_is_unavailable_with_message(self):
        h = FakeLegionHelper(denied=("Failed", "etwas anderes"))
        self.assertUnavailable(legion(h), "other", "Temperaturabfrage ist fehlgeschlagen")

    def test_implausible_cpu(self):
        for cpu in (0, 126, 4000):
            h = FakeLegionHelper(cpu=cpu)
            self.assertUnavailable(legion(h), "implausible", "unplausibel")
            self.assertEqual(h.writes(), [])
            self.assertTrue(h.closed)

    def test_not_windows(self):
        made = []
        p = thermal.LegionProvider(helper_factory=lambda: made.append(1), platform="linux")
        self.assertUnavailable(p, "platform", "nur unter Windows")
        self.assertEqual(made, [])

    def test_powershell_missing_or_dead(self):
        h = FakeLegionHelper(start_error=thermal.HelperError("PowerShell nicht gefunden"))
        self.assertUnavailable(legion(h), "powershell", "PowerShell ist nicht nutzbar")
        h = FakeLegionHelper(die_on="GetCPUTemp")
        self.assertUnavailable(legion(h), "other", "Temperaturabfrage ist fehlgeschlagen")
        self.assertTrue(h.closed)

    def test_measure(self):
        p = legion(FakeLegionHelper(cpu=71, fan1=4800, fan2=5100))
        p.open()
        m = p.measure()
        self.assertEqual((m.cpu_temp_c, m.fan_rpm, m.fan_max_rpm), (71.0, (4800, 5100), 5500))

    def test_fan_via_feature_reads_first_then_switches_and_restores(self):
        h = FakeLegionHelper(feature=0)
        p = legion(h)
        p.open()
        self.assertEqual(h.writes(), [])  # open() liest nur
        self.assertEqual(p.fan_mode, "feature")
        self.assertTrue(p.fan_control_info()[0])
        n_before = len(h.calls)
        p.fan_full()
        new = h.calls[n_before:]
        self.assertEqual(new[0], f"CALL LENOVO_OTHER_METHOD GetFeatureValue IDs={thermal.FEATURE_ID_FAN_FULL_SPEED}")  # K5
        self.assertEqual(new[1], f"CALL LENOVO_OTHER_METHOD SetFeatureValue IDs={thermal.FEATURE_ID_FAN_FULL_SPEED} value=1")
        self.assertTrue(new[2].startswith("CALL LENOVO_OTHER_METHOD GetFeatureValue"))  # zurueckgelesen
        self.assertEqual(h.feature, 1)
        p.fan_restore()
        self.assertEqual(h.feature, 0)

    def test_feature_id_constant(self):
        self.assertEqual(thermal.FEATURE_ID_FAN_FULL_SPEED, 0x04020000)

    def test_get_feature_value_gets_only_ids_never_value(self):
        h = FakeLegionHelper(feature=0)
        p = legion(h)
        p.open()
        gets = [c for c in h.calls if "GetFeatureValue" in c]
        self.assertTrue(gets)
        for c in gets:
            self.assertEqual(c, f"CALL LENOVO_OTHER_METHOD GetFeatureValue IDs={thermal.FEATURE_ID_FAN_FULL_SPEED}")
        self.assertEqual(p.fan_mode, "feature")  # die Attrappe lehnt 'value' als Eingabe ab

    def test_feature_restore_keeps_already_full_state(self):
        h = FakeLegionHelper(feature=1)
        p = legion(h)
        p.open()
        p.fan_full()
        p.fan_restore()
        self.assertEqual(h.feature, 1)

    def test_smart_fan_mode_is_never_switched_only_read(self):
        """G1: Der Leistungsmodus (SmartFanMode 3) hebt die CPU-Grenzen an -> mehr Waerme: nie schalten."""
        self.assertNotIn("SetSmartFanMode", Path(thermal.__file__).read_text(encoding="utf-8"))
        self.assertFalse(hasattr(thermal, "SMART_FAN_MODE_PERFORMANCE"))
        for feature in (0, None):  # mit und ohne lesbare Kennung
            h = FakeLegionHelper(feature=feature, smart=2)  # SetSmartFanMode wirft AssertionError
            p = legion(h)
            p.open()
            self.assertEqual(p.read_smart_fan_mode(), 2)  # lesen ist erlaubt
            if feature is not None:
                p.fan_full()
                p.fan_restore()
            self.assertFalse([c for c in h.calls if "SetSmartFanMode" in c])
            self.assertEqual(h.smart, 2)

    def test_unreadable_feature_means_measure_and_pause_only_with_reason(self):
        h = FakeLegionHelper(feature=None, smart=2)
        p = legion(h)
        p.open()
        can, note = p.fan_control_info()
        self.assertFalse(can)
        self.assertIn("auf diesem Gerät nicht verfügbar", note)
        self.assertEqual(p.measure().cpu_temp_c, 60)  # Anzeige geht
        with self.assertRaises(thermal.ProviderError):
            p.fan_full()
        p.fan_restore()  # nichts zu tun, kein Fehler
        self.assertEqual(h.writes(), [])
        # Statustext des Reglers nennt Lüftersteuerung + Grund
        notes = Notes()
        ctl = thermal.ThermalController(p, None, FakePause([]), notes.notify, notes.status)
        ctl.setup()
        ctl.tick(thermal.Messung(70.0, (2000,)))
        self.assertIn("Lüftersteuerung auf diesem Gerät nicht verfügbar", notes.states[-1][0])
        self.assertNotIn("nicht lesbar", notes.states[-1][0])  # Kennung nur im Protokoll
        self.assertIn("nicht lesbar", notes.text("detail"))

    def test_no_fan_control_only_displays(self):
        h = FakeLegionHelper(feature=None, smart=None)
        p = legion(h)
        p.open()
        self.assertEqual(p.fan_control_info()[0], False)
        self.assertEqual(p.measure().cpu_temp_c, 60)  # Anzeige geht
        self.assertIsNone(p.read_smart_fan_mode())
        with self.assertRaises(thermal.ProviderError):
            p.fan_full()
        p.fan_restore()  # nichts zu tun, kein Fehler
        self.assertEqual(h.writes(), [])

    def test_readback_mismatch_is_an_error(self):
        h = FakeLegionHelper(feature=0, ignore_writes=True)
        p = legion(h)
        p.open()
        with self.assertRaises(thermal.ProviderError):
            p.fan_full()

    def test_trace_reports_each_step(self):
        lines = []
        h = FakeLegionHelper(feature=0)
        p = legion(h, trace=lines.append)
        p.open()
        p.fan_full()
        text = "\n".join(lines)
        for part in ("GetCPUTemp", "GetFeatureValue", "SetFeatureValue(IDs=67239936, value=1)", "->"):
            self.assertIn(part, text)

    def test_close_closes_helper(self):
        h = FakeLegionHelper()
        p = legion(h)
        p.open()
        p.close()
        p.close()
        self.assertTrue(h.closed)

    def test_whole_chain_provider_to_controller(self):
        h = FakeLegionHelper(cpu=90, feature=0)
        av = thermal.check_availability(lambda: legion(h))
        self.assertTrue(av.ok)
        notes = Notes()
        log = []
        ctl = thermal.ThermalController(av.provider, None, FakePause(log), notes.notify, notes.status)
        ctl.setup()
        ctl.tick(av.first)
        ctl.tick()
        self.assertEqual(h.feature, 1)
        ctl.finish()
        self.assertEqual(h.feature, 0)
        self.assertTrue(h.closed)


class ThermalTestCommandTests(unittest.TestCase):
    def go(self, prov, input_fn=lambda prompt="": "", full_seconds=0.05, samples=3, trace=None):
        lines = []
        rc = thermal.run_thermal_test(lines.append, provider_factory=lambda: prov, sleep=lambda s: time.sleep(0.01),
                                      full_seconds=full_seconds, samples=samples, input_fn=input_fn)
        return rc, lines

    def test_unavailable_returns_1_and_switches_nothing(self):
        h = FakeLegionHelper(denied=("AccessDenied", "x"))
        rc, lines = self.go(legion(h))
        out = "\n".join(lines)
        self.assertEqual(rc, 1)
        self.assertIn("NICHT VERFÜGBAR: Administratorrechte nötig", out)
        self.assertTrue(lines[-1].startswith("ERGEBNIS: nicht verfügbar"))
        self.assertEqual(h.writes(), [])

    def test_says_that_the_feature_id_is_unconfirmed(self):
        rc, lines = self.go(legion(FakeLegionHelper(feature=0)))
        self.assertTrue(any("UNBESTÄTIGT" in l and "0x04020000" in l for l in lines), lines)

    def test_prompts_before_switching_and_shows_smart_fan_mode_read_only(self):
        h = FakeLegionHelper(feature=0, smart=2)
        seen = {}

        def input_fn(prompt=""):
            seen["prompt"] = prompt
            seen["writes"] = list(h.writes())
            return ""

        rc, lines = self.go(legion(h), input_fn=input_fn)
        self.assertIn("Enter drücken zum Fortfahren (Strg+C = abbrechen)", seen["prompt"])
        self.assertEqual(seen["writes"], [], "vor dem Enter darf nichts geschaltet sein")
        self.assertTrue(any("SmartFanMode (nur gelesen" in l and "2" in l for l in lines))
        self.assertEqual(h.smart, 2)

    def test_effect_verdict_with_rpms_before_during_after(self):
        h = FakeLegionHelper(feature=0, boost=True)
        lines = []
        rc = thermal.run_thermal_test(lines.append, provider_factory=lambda: legion(h, trace=lines.append),
                                      sleep=lambda s: time.sleep(0.01), full_seconds=0.05, samples=3,
                                      input_fn=lambda p="": "")
        out = "\n".join(lines)
        self.assertEqual(rc, 0, out)
        self.assertEqual(lines[-1], "ERGEBNIS: Lüfter voll WIRKT (Drehzahl von 2100 auf 4200 U/min)")
        for part in ("Drehzahl VORHER: 2000/2100", "während voll:", "Drehzahl NACHHER", "Schritt 4",
                     "SetFeatureValue(IDs=67239936, value=1)", "SetFeatureValue(IDs=67239936, value=0)"):
            self.assertIn(part, out)
        self.assertEqual(h.feature, 0)

    def test_no_effect_verdict_asks_for_feedback(self):
        h = FakeLegionHelper(feature=0, boost=False)
        rc, lines = self.go(legion(h))
        self.assertEqual(lines[-1], "ERGEBNIS: KEINE Wirkung messbar - bitte diese Ausgabe an den Autor zurückmelden")
        self.assertEqual(h.feature, 0)

    def test_measure_only_verdict(self):
        h = FakeLegionHelper(feature=None)
        called = []
        rc, lines = self.go(legion(h), input_fn=lambda p="": called.append(p))
        self.assertEqual(rc, 0)
        self.assertEqual(lines[-1], "ERGEBNIS: nur Messung möglich - Lüftersteuerung auf diesem Gerät nicht verfügbar")
        self.assertEqual((called, h.writes()), ([], []))

    def test_abort_at_prompt_switches_nothing(self):
        h = FakeLegionHelper(feature=0)

        def input_fn(prompt=""):
            raise KeyboardInterrupt

        rc, lines = self.go(legion(h), input_fn=input_fn)
        self.assertEqual(rc, 1)
        self.assertEqual(h.writes(), [])
        self.assertTrue(lines[-1].startswith("ERGEBNIS: abgebrochen"))

    def test_ctrl_c_while_full_is_friendly_and_restores_with_sigint_ignored(self):
        h = FakeLegionHelper(feature=0)
        prov = legion(h)
        seen = {}
        orig = prov.fan_restore

        def restore():
            seen["handler"] = signal.getsignal(signal.SIGINT)
            return orig()

        prov.fan_restore = restore
        before = signal.getsignal(signal.SIGINT)
        lines = []

        def sleep(s):
            if h.feature == 1:
                raise KeyboardInterrupt

        rc = thermal.run_thermal_test(lines.append, provider_factory=lambda: prov, sleep=sleep, full_seconds=5.0,
                                      samples=1, input_fn=lambda p="": "")  # wirft NICHT
        self.assertEqual(rc, 1)
        self.assertEqual(h.feature, 0)
        self.assertTrue(h.closed)
        self.assertEqual(seen["handler"], signal.SIG_IGN, "Strg+C muss waehrend des Zurueckstellens ignoriert werden")
        self.assertEqual(signal.getsignal(signal.SIGINT), before, "Signalbehandlung wieder hergestellt")
        out = "\n".join(lines)
        self.assertIn("Abgebrochen (Strg+C)", out)
        self.assertNotIn("Traceback", out)
        self.assertTrue(lines[-1].startswith("ERGEBNIS: abgebrochen"))


# --------------------------------------------------------------------------- CLI
class CliThermalTests(unittest.TestCase):
    def parse(self, *a):
        return cli.build_parser().parse_args(["q", "z", *a])

    def test_defaults(self):
        a = self.parse()
        self.assertEqual((a.thermal, a.temp_fan, a.temp_pause, a.threads, a.thermal_test), ("auto", 85.0, 97.0, None, False))

    def test_options(self):
        a = self.parse("--thermal", "off", "--temp-fan", "80", "--temp-pause", "92,5", "--threads", "4")
        self.assertEqual((a.thermal, a.temp_fan, a.temp_pause, a.threads), ("off", 80.0, 92.5, 4))
        self.assertTrue(cli.build_parser().parse_args(["--thermal-test"]).thermal_test)

    def test_invalid_values(self):
        for bad in (["--threads", "0"], ["--threads", "x"], ["--threads", "257"], ["--thermal", "an"], ["--temp-fan", "abc"],
                    ["--temp-pause", "abc"]):
            with self.subTest(bad=bad), mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
                self.parse(*bad)

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
        with mock.patch.object(converter, "Converter", FakeConv), mock.patch("sys.stdout", io.StringIO()):
            rc = cli.main(argv)
        return rc, captured["opts"]

    def test_main_passes_options_to_converter(self):
        rc, o = self._main(["q", "z", "--thermal", "off", "--temp-fan", "80", "--temp-pause", "95", "--threads", "3"])
        self.assertEqual((rc, o.thermal, o.temp_fan, o.temp_pause, o.threads), (0, "off", 80.0, 95.0, 3))

    def test_main_defaults_to_auto_and_unlimited(self):
        rc, o = self._main(["q", "z"])
        self.assertEqual((o.thermal, o.threads), ("auto", None))

    def test_main_rejects_inconsistent_limits(self):
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as cm:
            cli.main(["q", "z", "--temp-fan", "99", "--temp-pause", "90"])
        self.assertEqual(cm.exception.code, 2)

    def test_thermal_test_flag_runs_diagnosis_only(self):
        with mock.patch.object(thermal, "run_thermal_test", return_value=3) as m, \
                mock.patch.object(converter, "Converter") as conv:
            self.assertEqual(cli.main(["--thermal-test"]), 3)
        m.assert_called_once()
        conv.assert_not_called()

    def test_reporter_appends_temperature_to_progress(self):
        r = cli.CliReporter()
        r.thermal_status("CPU 70 °C", "ok")
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            r.progress("A", 1.0, 0.5, None, "x")
        self.assertIn("CPU 70 °C", buf.getvalue())
        r.thermal_status("", "")
        buf2 = io.StringIO()
        with mock.patch("sys.stdout", buf2):
            r.progress("A", 1.0, 0.5, None, "x")
        self.assertNotIn("CPU", buf2.getvalue())


# --------------------------------------------------------------------------- --threads im ffmpeg-Aufruf
class ThreadsTests(RunTestBase):
    def _ffmpeg_args(self, threads):
        seen = []

        def fake_run(args, **kw):
            seen.append(list(args))
            kw["on_stdout_line"]("frame=10")
            return tools.ProcResult(0, "", "")

        from xdroot_converter import mxfinfo
        info = mxfinfo.MxfInfo(mp_uid="AA", fp_uid="BB")
        plan = converter.ClipPlan(mxf=Path("A.MXF"), stem="A", material=None, info=info)
        plan.audio_map = [0]
        plan.new_codec = CODEC_L
        conv = converter.Converter(converter.Options(source="s", target="t", threads=threads), Rep())
        with mock.patch.object(tools, "run_capture", fake_run), mock.patch.object(tools, "find_tool", lambda n: n):
            _REAL_TRANSCODE(conv, plan, Fraction(50), 10, Path("v.h264"), [Path("a0.wav")])
        return seen[0]

    def test_threads_for_decoder_and_encoder(self):
        a = self._ffmpeg_args(2)
        idx = [i for i, x in enumerate(a) if x == "-threads"]
        self.assertEqual(len(idx), 2)
        self.assertTrue(all(a[i + 1] == "2" for i in idx))
        self.assertLess(idx[0], a.index("-i"))  # Decoder: vor der Eingabe
        self.assertGreater(idx[1], a.index("-c:v"))  # Encoder: Ausgabeoption fuer die Video-Ausgabe
        self.assertLess(idx[1], a.index("-f"))
        self.assertLess(idx[1], a.index("v.h264"))

    def test_no_threads_option_by_default(self):
        self.assertNotIn("-threads", self._ffmpeg_args(None))

    def test_threads_not_a_resume_setting(self):
        self.assertEqual(converter.diff_settings({"threads": None}, {"threads": 2}), [])
        self.run_conv(threads=2)
        s, rep, _ = self.run_conv(threads=4)
        self.assertNotIn("anderen Einstellungen", rep.text("warn"))
        self.assertEqual(s.count(converter.ST_SKIPPED_DONE), 2)
        import json
        store = json.loads((self.tgt / converter.SETTINGS_NAME).read_text(encoding="utf-8"))
        self.assertEqual(store["clips"]["Clip/A.MXF"]["threads"], 2)  # nur zur Information

    def test_threads_logged(self):
        _s, rep, _ = self.run_conv(threads=3)
        self.assertIn("ffmpeg-Threads begrenzt auf 3", rep.text("info"))


# --------------------------------------------------------------------------- Integration in den Ablauf
class RunIntegrationTests(RunTestBase):
    def setUp(self):
        super().setUp()
        self.log = []
        self.statuses = []

    def run_thermal(self, provider, transcode=None, **kw):
        kw.setdefault("thermal", "auto")
        kw.setdefault("thermal_interval_s", 0.01)
        kw["thermal_provider"] = (lambda: provider) if isinstance(provider, thermal.ThermalProvider) else provider
        rep = Rep()
        rep.thermal_status = lambda text, state: self.statuses.append((text, state))
        conv = converter.Converter(converter.Options(source=str(self.src), target=str(self.tgt), **kw), rep)
        if transcode is None:
            return conv.run(), rep, conv
        fake = converter.Converter._transcode  # (Fakes) fuer den Standardfall
        with mock.patch.object(converter.Converter, "_transcode", transcode(fake)):
            return conv.run(), rep, conv

    def test_default_options_do_not_touch_thermal_at_all(self):
        def boom():
            raise AssertionError("Anbieter darf nicht angefasst werden")

        s, rep, _ = self.run_thermal(boom, thermal="off")
        self.assertEqual((s.count(converter.ST_CONVERTED), s.failed, s.pauses), (2, 0, 0))
        self.assertEqual(self.statuses, [])
        self.assertNotIn("Temperatur", s.text())

    def test_unavailable_provider_run_is_unchanged(self):
        def factory():
            return FakeProvider([1], fail_open=thermal.ProviderUnavailable("Administratorrechte nötig", "admin"))

        s, rep, _ = self.run_thermal(factory)
        self.assertEqual((s.count(converter.ST_CONVERTED), s.failed, s.fatal), (2, 0, ""))
        self.assertIn("Administratorrechte nötig", s.thermal_note)
        self.assertIn("Administratorrechte nötig", s.text())
        self.assertTrue(any(st == "admin" for _t, st in self.statuses))
        self.assertNotIn("Temperatur", rep.text("warn"))  # nur Info, keine Warnung
        self.assert_consistent()

    def _hot_transcode(self, prov, extra=None):
        def make(orig):
            def tr(conv, plan, fps, frames, v_path, wavs):
                if plan.name == "A.MXF":
                    wait_for(lambda: "fan_full" in self.log, 10)
                    if extra:
                        extra(conv)
                return orig(conv, plan, fps, frames, v_path, wavs)
            return tr
        return make

    def test_fan_switched_and_restored_after_normal_run(self):
        prov = FakeProvider([90], self.log)
        s, rep, _ = self.run_thermal(prov, self._hot_transcode(prov))
        self.assertEqual((s.count(converter.ST_CONVERTED), s.failed), (2, 0))
        self.assertEqual(ops(self.log)[0], "fan_full")
        self.assertEqual(ops(self.log)[-1], "fan_restore")
        self.assertEqual(ops(self.log).count("fan_restore"), 1)
        self.assertTrue(prov.closed)
        self.assertIn("Lüfter voll", rep.text("info"))
        self.assertEqual(self.statuses[-1], ("", ""))  # Statuszeile am Ende geleert
        self.assertIn("Höchste gemessene CPU-Temperatur: 90", s.text())

    def test_cancel_restores_fan(self):
        prov = FakeProvider([90], self.log)
        cancel = tools.CancelToken()

        def make(orig):
            def tr(conv, plan, fps, frames, v_path, wavs):
                wait_for(lambda: "fan_full" in self.log, 10)
                cancel.cancel()
                cancel.check()
            return tr

        opts = converter.Options(source=str(self.src), target=str(self.tgt), thermal="auto", thermal_interval_s=0.01,
                                 thermal_provider=lambda: prov)
        with mock.patch.object(converter.Converter, "_transcode", make(None)):
            s = converter.Converter(opts, Rep(), cancel).run()
        self.assertTrue(s.cancelled)
        self.assertEqual(ops(self.log), ["fan_full", "fan_restore"])
        self.assertTrue(prov.closed)

    def test_unexpected_exception_restores_fan(self):
        prov = FakeProvider([90], self.log)

        def make(orig):
            def tr(conv, plan, fps, frames, v_path, wavs):
                wait_for(lambda: "fan_full" in self.log, 10)
                raise KeyboardInterrupt  # BaseException: verlaesst run()
            return tr

        with self.assertRaises(KeyboardInterrupt):
            self.run_thermal(prov, make)
        self.assertEqual(ops(self.log), ["fan_full", "fan_restore"])
        self.assertTrue(prov.closed)

    def test_fatal_error_restores_fan(self):
        prov = FakeProvider([90], self.log)
        real = converter.Converter._write_mediapro

        def failing(conv, final=False):
            if not final:
                wait_for(lambda: "fan_full" in self.log, 10)
                raise RuntimeError("Plattenfehler")
            return real(conv, final)

        with mock.patch.object(converter.Converter, "_write_mediapro", failing):
            s, rep, _ = self.run_thermal(prov)
        self.assertIn("Plattenfehler", s.fatal)
        self.assertEqual(ops(self.log), ["fan_full", "fan_restore"])

    def test_pause_in_log_and_summary_and_run_continues(self):
        cool = threading.Event()

        class Gated(FakeProvider):
            def measure(self):  # heiss, bis der Test "abkuehlen" laesst
                self.temps = [60 if cool.is_set() else 99]
                self.i = 0
                return super().measure()

        prov = Gated([99], self.log)
        seen = {}

        def make(orig):
            def tr(conv, plan, fps, frames, v_path, wavs):
                if plan.name == "A.MXF":
                    fake_proc = mock.Mock()
                    fake_proc.poll.return_value = None
                    conv.cancel.register(fake_proc)  # ein laufender Prozess, den die Pause anhalten kann
                    seen["paused"] = wait_for(lambda: conv.cancel.paused, 10)
                    cool.set()
                    seen["resumed"] = wait_for(lambda: not conv.cancel.paused, 10)
                    conv.cancel.unregister(fake_proc)
                return orig(conv, plan, fps, frames, v_path, wavs)
            return tr

        # Runde 5: ohne setzbare Affinität (hier: nicht unterstützt) gilt das alte Verhalten "sofort pausieren"; sonst würde der
        # Regler erst drosseln (30 s Sperre) und der Test hinge vom Zeitpunkt der Prozess-Registrierung ab.
        with mock.patch.object(tools, "suspend_process", lambda p: True), \
                mock.patch.object(tools, "resume_process", lambda p: True), \
                mock.patch.object(tools, "available_cpus", lambda: None):
            s, rep, _ = self.run_thermal(prov, make)
        self.assertTrue(seen["paused"] and seen["resumed"])
        self.assertEqual((s.count(converter.ST_CONVERTED), s.failed), (2, 0))
        self.assertEqual(s.pauses, 1)
        self.assertGreater(s.pause_seconds, 0)
        self.assertIn("Temperaturpausen: 1, gesamt", s.text())
        self.assertIn("Pause wegen Temperatur", rep.text("warn"))
        self.assertIn("Pause beendet", rep.text("info"))
        log_file = (self.tgt / converter.LOG_NAME).read_text(encoding="utf-8")
        self.assertIn("Pause wegen Temperatur", log_file)
        self.assertIn("Temperaturpausen: 1", log_file)
        # (die Pause selbst laeuft ueber das echte CancelToken, nicht ueber die Attrappe im log)
        self.assertEqual(ops(self.log), ["fan_full", "fan_restore"])

    def test_provider_failure_midrun_does_not_fail_clips(self):
        prov = FakeProvider([80, thermal.ProviderError("USB weg")], self.log)

        def make(orig):
            def tr(conv, plan, fps, frames, v_path, wavs):
                wait_for(lambda: prov.i >= 2, 10)
                time.sleep(0.1)
                return orig(conv, plan, fps, frames, v_path, wavs)
            return tr

        s, rep, _ = self.run_thermal(prov, make)
        self.assertEqual((s.count(converter.ST_CONVERTED), s.failed, s.fatal), (2, 0, ""))
        self.assertIn("ausgefallen", rep.text("warn"))
        self.assertIn("nicht verfügbar/ausgefallen", s.text())
        self.assert_consistent()

    def test_invalid_thresholds_do_not_fail_the_run(self):
        s, rep, _ = self.run_thermal(FakeProvider([60]), temp_fan=99, temp_pause=90)
        self.assertEqual((s.count(converter.ST_CONVERTED), s.failed, s.fatal), (2, 0, ""))
        self.assertIn("Temperaturüberwachung konnte nicht gestartet werden", rep.text("warn"))

    def test_eta_excludes_pause_time(self):
        conv = converter.Converter(converter.Options(source="s", target="t"), Rep())
        conv._clip_t0 = time.time() - 10
        conv._pause0_clip = 1.0
        with mock.patch.object(conv.cancel, "paused_time", lambda: 5.0):
            self.assertAlmostEqual(conv._clip_active_secs(), 6.0, delta=0.5)


class SummaryTextTests(unittest.TestCase):
    def test_no_thermal_lines_by_default(self):
        t = converter.RunSummary().text()
        self.assertNotIn("Temperatur", t)

    def test_pause_line(self):
        s = converter.RunSummary(pauses=3, pause_seconds=252, max_cpu_c=98.0)
        self.assertIn("Temperaturpausen: 3, gesamt 4:12 (höchste gemessene CPU-Temperatur 98 °C)", s.text())


class GuiLogicTests(unittest.TestCase):
    def test_parse_threads(self):
        from xdroot_converter import gui
        self.assertIsNone(gui.parse_threads(""))
        self.assertIsNone(gui.parse_threads("  "))
        self.assertEqual(gui.parse_threads(" 4 "), 4)
        for bad in ("0", "-1", "x", "1.5", "9999"):
            with self.assertRaises(ValueError, msg=bad):
                gui.parse_threads(bad)

    def test_parse_temp_limits(self):
        from xdroot_converter import gui
        self.assertEqual(gui.parse_temp_limits("85", "97"), (85.0, 97.0))
        self.assertEqual(gui.parse_temp_limits("80,5", "95"), (80.5, 95.0))
        for fan, pause in (("x", "97"), ("85", ""), ("97", "97"), ("99", "90"), ("10", "97"), ("85", "200")):
            with self.assertRaises(ValueError, msg=(fan, pause)):
                gui.parse_temp_limits(fan, pause)

    def test_admin_hint_text_mentions_bat(self):
        from xdroot_converter import gui
        self.assertIn("XDROOT-Converter (Administrator).bat", gui.HINT_ADMIN)


if __name__ == "__main__":
    unittest.main()
