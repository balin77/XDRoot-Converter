"""Tests der Fix-Runde zur Temperaturueberwachung: fester PowerShell-Pfad, Hilfsprozess-Neustart, Wirkungspruefung,
Zustandsdatei, Pausen-Hoechstdauer, Start mit Administratorrechten (mit ersetztem ShellExecuteW - keine UAC),
Abschlussfenster, CLI-Grenzen, Messthread-Ende und --threads bei --verify --deep."""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from xdroot_converter import cli, converter, elevate, gui, thermal, tools, verify

try:
    from test_thermal import (FakeLegionHelper, FakePause, FakeProvider, Notes, PY, RunTestBase, Rep, legion, make_ctl,
                              new_store, ops, run_ticks, wait_for, FAKE_SHELL)
except ImportError:  # pragma: no cover
    from tests.test_thermal import (FakeLegionHelper, FakePause, FakeProvider, Notes, PY, RunTestBase, Rep, legion,
                                    make_ctl, new_store, ops, run_ticks, wait_for, FAKE_SHELL)

ROOT = Path(__file__).resolve().parent.parent


@contextlib.contextmanager
def tmpdir():
    d = Path(tempfile.mkdtemp(prefix="xdroot-test-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------- G3: fester PowerShell-Pfad
class PowerShellPathTests(unittest.TestCase):
    def test_fixed_path_from_system_directory(self):
        with tmpdir() as d:
            exe = d / "WindowsPowerShell" / "v1.0" / "powershell.exe"
            exe.parent.mkdir(parents=True)
            exe.write_bytes(b"")
            with mock.patch.object(thermal, "_system_directory", lambda: str(d)):
                self.assertEqual(thermal.system_powershell_path(), str(exe))

    def test_missing_file_is_unavailable(self):
        with tmpdir() as d, mock.patch.object(thermal, "_system_directory", lambda: str(d)):
            self.assertIsNone(thermal.system_powershell_path())
            with self.assertRaises(thermal.HelperError):
                thermal.powershell_argv()
            h = thermal.PowerShellHelper()
            with self.assertRaises(thermal.HelperError):
                h.start()
            self.assertFalse(h.alive)

    def test_unknown_system_directory_is_unavailable(self):
        with mock.patch.object(thermal, "_system_directory", lambda: None):
            self.assertIsNone(thermal.system_powershell_path())

    def test_powershell_bat_and_exe_in_working_directory_are_never_used(self):
        """Unter Python 3.11 findet shutil.which zuerst Dateien im Arbeitsordner - hier darf nichts davon laufen."""
        with tmpdir() as d:
            for name in ("powershell.bat", "powershell.exe", "pwsh.exe", "pwsh.bat"):
                (d / name).write_text("@echo boese\n", encoding="ascii")
            old = os.getcwd()
            os.chdir(d)
            try:
                with mock.patch.object(thermal, "_system_directory", lambda: str(d / "leer")), \
                        mock.patch.object(subprocess, "Popen", side_effect=AssertionError("nichts darf starten")):
                    with self.assertRaises(thermal.HelperError):
                        thermal.powershell_argv()
                    with self.assertRaises(thermal.HelperError):
                        thermal.PowerShellHelper(popen=subprocess.Popen).start()
                    # und mit echtem Systemverzeichnis steht dort ein absoluter Pfad, nicht der Arbeitsordner
                if sys.platform == "win32":
                    exe = thermal.system_powershell_path()
                    if exe:
                        self.assertTrue(os.path.isabs(exe))
                        self.assertNotEqual(Path(exe).parent.resolve(), d.resolve())
                        self.assertEqual(thermal.powershell_argv()[0], exe)
            finally:
                os.chdir(old)

    def test_module_does_not_search_the_path(self):
        self.assertFalse(hasattr(thermal, "shutil"))
        src = Path(thermal.__file__).read_text(encoding="utf-8")
        self.assertNotIn("which(", src.replace("shutil.which findet", ""))
        self.assertNotIn("pwsh", src.replace("kein pwsh", "").replace("pwsh benutzt", ""))

    @unittest.skipUnless(sys.platform == "win32", "nur Windows")
    def test_real_system_directory(self):
        d = tools.system_directory()
        self.assertTrue(d and os.path.isdir(d))

    @unittest.skipUnless(sys.platform == "win32", "nur Windows")
    def test_taskkill_uses_absolute_path(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        with mock.patch.object(tools, "system_directory", lambda: "C:\\Fest"), \
                mock.patch.object(tools.subprocess, "run") as run:
            tools.kill_process(proc)
        self.assertEqual(run.call_args[0][0][0], os.path.join("C:\\Fest", "taskkill.exe"))


# --------------------------------------------------------------------------- G2: Hilfsprozess neu starten
class World:
    """Gemeinsamer WMI-Zustand mehrerer (nacheinander gestarteter) Attrappen-Hilfsprozesse."""

    def __init__(self, feature=0, cpu=60):
        self.feature = feature
        self.cpu = cpu
        self.helpers = []
        self.timeout_on = []  # Methodennamen: der naechste Aufruf bleibt unbeantwortet (Zeitlimit, Prozess tot)
        self.fail_start = 0

    def factory(self):
        if self.fail_start > 0:
            self.fail_start -= 1
            raise_on_start = True
        else:
            raise_on_start = False
        h = WorldHelper(self, raise_on_start)
        self.helpers.append(h)
        return h


class WorldHelper:
    def __init__(self, world, raise_on_start=False):
        self.w = world
        self.dead = False
        self.closed = False
        self.raise_on_start = raise_on_start
        self.calls = []

    @property
    def alive(self):
        return not self.dead and not self.closed

    def start(self):
        if self.raise_on_start:
            self.dead = True
            raise thermal.HelperError("Start gescheitert")

    def abort(self):
        self.dead = True

    def close(self, grace=2.0):
        self.closed = True

    def call(self, line, timeout=None, lock_timeout=None):
        if not self.alive:
            raise thermal.HelperError("Hilfsprozess nicht aktiv")
        self.calls.append((line, timeout))
        t = line.split()
        method = t[2]
        params = dict(x.split("=") for x in t[3:])
        if method in self.w.timeout_on:
            self.w.timeout_on.remove(method)
            self.dead = True
            raise thermal.HelperTimeout("keine Antwort")
        if method == "GetCPUTemp":
            return f"Data={self.w.cpu}"
        if method in ("GetFan1Speed", "GetFan2Speed"):
            return "Data=2000"
        if method == "GetFanMaxSpeed":
            return "Data=5500"
        if method == "GetFeatureValue":
            return f"IDs={params['IDs']} value={self.w.feature}"
        if method == "SetFeatureValue":
            self.w.feature = int(params["value"])
            return "ReturnValue=0"
        raise thermal.HelperRemoteError("InvalidMethod", method)


def world_provider(world, store=None):
    return thermal.LegionProvider(helper_factory=world.factory, platform="win32", state_store=store or new_store())


class HelperRestartTests(unittest.TestCase):
    def test_restore_after_dead_helper_starts_a_fresh_one(self):
        w = World(feature=0)
        p = world_provider(w)
        p.open()
        p.fan_full()
        self.assertEqual(w.feature, 1)
        w.helpers[0].dead = True  # z. B. nach einem Zeitlimit einer Messung beendet
        p.fan_restore()
        self.assertEqual(w.feature, 0)
        self.assertEqual(len(w.helpers), 2, "frischer Hilfsprozess fuers Zurueckstellen")
        self.assertIsNone(p._store.path and (p._store.path.exists() or None))  # Zustandsdatei geloescht

    def test_restore_timeout_on_write_retries_with_fresh_helper(self):
        w = World(feature=0)
        p = world_provider(w)
        p.open()
        p.fan_full()
        w.timeout_on.append("SetFeatureValue")  # langsame WMI-Antwort beim Zurueckstellen
        p.fan_restore()  # Versuch 1 scheitert, Versuch 2 mit frischem Hilfsprozess klappt
        self.assertEqual(w.feature, 0)
        self.assertEqual(len(w.helpers), 2)

    def test_restore_gives_up_after_two_attempts_and_keeps_state_file(self):
        w = World(feature=0)
        store = new_store()
        p = world_provider(w, store)
        p.open()
        p.fan_full()
        w.fail_start = 5  # frische Hilfsprozesse lassen sich nicht starten
        w.helpers[0].dead = True
        with self.assertRaises(thermal.ProviderError):
            p.fan_restore()
        self.assertEqual(w.feature, 1)
        self.assertTrue(store.path.exists(), "Zustandsdatei bleibt, damit der naechste Lauf zurueckstellt")

    def test_write_and_readback_timeouts_are_30_seconds(self):
        w = World(feature=0)
        p = world_provider(w)
        p.open()
        p.fan_full()
        timeouts = {line.split()[2]: t for h in w.helpers for line, t in h.calls if "SetFeatureValue" in line}
        self.assertEqual(timeouts["SetFeatureValue"], 30.0)
        readbacks = [t for h in w.helpers for line, t in h.calls if "GetFeatureValue" in line][-1]
        self.assertEqual(readbacks, 30.0)
        self.assertEqual(thermal.HELPER_WRITE_TIMEOUT_S, 30.0)

    def test_measurement_timeout_restarts_once_and_keeps_measuring(self):
        w = World()
        p = world_provider(w)
        p.open()
        w.timeout_on.append("GetCPUTemp")
        m = p.measure()  # Zeitlimit -> Neustart -> Wiederholung klappt
        self.assertEqual(m.cpu_temp_c, 60.0)
        self.assertEqual(len(w.helpers), 2)
        self.assertEqual(p.measure().cpu_temp_c, 60.0)  # und es geht weiter
        self.assertEqual(len(w.helpers), 2)

    def test_repeated_measurement_failure_makes_provider_failed_and_controller_restores(self):
        w = World(feature=0, cpu=90)
        p = world_provider(w)
        p.open()
        notes = Notes()
        ctl = thermal.ThermalController(p, None, FakePause([]), notes.notify, notes.status)
        ctl.setup()
        ctl.tick()
        ctl.tick()  # 2x heiss -> Luefter voll
        self.assertEqual(w.feature, 1)
        w.timeout_on.append("GetCPUTemp")
        w.fail_start = 1  # der Neustart scheitert -> wiederholtes Scheitern
        ctl.tick()
        self.assertIn("Temperaturmessung ist ausgefallen", ctl.failed)
        self.assertEqual(w.feature, 0, "Luefter zurueckgestellt (mit frischem Hilfsprozess)")
        self.assertIn("ausgefallen", notes.text("warn"))

    def test_real_process_that_stops_answering_is_replaced(self):
        """Echter Hilfsprozess-Ersatz (Python): beantwortet den ersten SetFeatureValue nicht -> Zeitlimit -> Neustart."""
        shell = r'''
import sys, os, json
state = sys.argv[1]
def load():
    return json.load(open(state))
def save(d):
    json.dump(d, open(state, "w"))
for line in sys.stdin:
    rid, _, cmd = line.strip().partition(" ")
    if cmd == "EXIT":
        break
    if cmd == "PING":
        print(rid + " OK pong", flush=True); continue
    t = cmd.split()
    method = t[2]
    d = load()
    if method == "GetCPUTemp":
        print(rid + " OK Data=60", flush=True)
    elif method in ("GetFan1Speed", "GetFan2Speed"):
        print(rid + " OK Data=2000", flush=True)
    elif method == "GetFanMaxSpeed":
        print(rid + " OK Data=5500", flush=True)
    elif method == "GetFeatureValue":
        print(rid + " OK IDs=1 value=%d" % d["feature"], flush=True)
    elif method == "SetFeatureValue":
        if d.get("hang_once"):
            d["hang_once"] = False; save(d)
            import time; time.sleep(60)
        d["feature"] = int(t[4].split("=")[1]); save(d)
        print(rid + " OK ReturnValue=0", flush=True)
'''
        with tmpdir() as d:
            state = d / "wmi.json"
            state.write_text(json.dumps({"feature": 0, "hang_once": False}), encoding="ascii")
            made = []

            def factory():
                h = thermal.PowerShellHelper(argv=[PY, "-u", "-c", shell, str(state)], startup_timeout=20, timeout=1.0)
                made.append(h)
                return h

            p = thermal.LegionProvider(helper_factory=factory, platform="win32", state_store=new_store())
            self.addCleanup(p.close)
            with mock.patch.object(thermal, "HELPER_WRITE_TIMEOUT_S", 1.0):
                p.open()
                p.fan_full()
                self.assertEqual(json.loads(state.read_text())["feature"], 1)
                state.write_text(json.dumps({"feature": 1, "hang_once": True}), encoding="ascii")
                t0 = time.monotonic()
                p.fan_restore()  # 1. Versuch: keine Antwort (Zeitlimit 1 s, Prozess beendet), 2. Versuch: frischer Prozess
                self.assertLess(time.monotonic() - t0, 30)
            self.assertEqual(json.loads(state.read_text())["feature"], 0)
            self.assertEqual(len(made), 2)
            self.assertFalse(made[0].alive)

    def test_helper_abort_ends_startup_quickly(self):
        h = thermal.PowerShellHelper(argv=[PY, "-c", "import time; time.sleep(60)"], startup_timeout=30)
        err = []

        def run():
            try:
                h.start()
            except thermal.HelperError as e:
                err.append(e)

        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.5)
        t0 = time.monotonic()
        h.abort()
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertLess(time.monotonic() - t0, 5)
        self.assertTrue(err)
        h.close(grace=0.0)


# --------------------------------------------------------------------------- M1: Wirkungspruefung
class FanEffectTests(unittest.TestCase):
    def test_rule(self):
        f = thermal.fan_effect_confirmed
        self.assertTrue(f(3000, 3450))  # +15 %
        self.assertFalse(f(3000, 3449))
        self.assertTrue(f(3000, 5000, 5500))  # nahe Hoechstdrehzahl (>= 90 %), vorher darunter
        self.assertFalse(f(5000, 5000, 5500))  # liefen schon vorher nahe Maximum: nur der Anstieg zaehlt
        self.assertFalse(f(3000, 3100, 100))  # GetFanMaxSpeed in anderer Einheit (unplausibel): nicht benutzt
        self.assertFalse(f(3000, 3100, 15000))
        self.assertFalse(f(3000, 3100, 2500))  # Maximum unter der Drehzahl vorher: unplausibel
        self.assertFalse(f(3000, 3100, 5500))
        self.assertFalse(f(None, None, 5500))
        self.assertFalse(f(3000, 0))
        self.assertFalse(f(None, 5200, 5500))  # ohne Drehzahl vorher keine 90-%-Regel

    def hot(self, rpms, fan_max=None):
        ctl, prov, log, notes, clock = make_ctl([90] * (len(rpms) + 2), rpms=rpms, fan_max=fan_max)
        return ctl, notes, clock

    def test_confirmed_when_rpm_rose(self):
        ctl, notes, clock = self.hot([(3000, 3100), (3000, 3100), (3000, 3100), (4600, 4700)])
        run_ticks(ctl, 2)  # 2. Messung schaltet
        self.assertIn("Lüfter voll angefordert", notes.states[-1][0])
        self.assertNotIn("keine Wirkung", notes.states[-1][0])
        clock[0] = 5.0
        run_ticks(ctl, 1)
        self.assertTrue(notes.states[-1][0].endswith("Lüfter voll angefordert") or "angefordert" in notes.states[-1][0])
        clock[0] = 10.0
        run_ticks(ctl, 1)
        self.assertTrue(notes.states[-1][0].endswith("Lüfter voll"), notes.states[-1][0])
        self.assertIn("Lüfter voll wirkt (Drehzahl von 3100 auf 4700 U/min)", notes.text("info"))

    def test_no_effect_after_window_logs_once(self):
        ctl, notes, clock = self.hot([(3000, 3100)] * 8)
        run_ticks(ctl, 2)
        for t in (5, 10, 15):
            clock[0] = float(t)
            run_ticks(ctl, 1)
            self.assertNotIn("keine Wirkung", notes.states[-1][0], "vor Ablauf der 20 s noch nicht")
        for t in (21, 26, 31):
            clock[0] = float(t)
            run_ticks(ctl, 1)
        self.assertIn("Lüfter voll angefordert (keine Wirkung messbar)", notes.states[-1][0])
        self.assertEqual(notes.text("warn").count("keine Wirkung messbar"), 1)  # einmal eine Protokollzeile

    def test_unreadable_rpms_only_requested(self):
        ctl, notes, clock = self.hot([()] * 8)
        run_ticks(ctl, 2)
        clock[0] = 30.0
        run_ticks(ctl, 3)
        self.assertTrue(notes.states[-1][0].endswith("Lüfter voll angefordert"), notes.states[-1][0])
        self.assertNotIn("keine Wirkung", notes.states[-1][0])
        self.assertNotIn("wirkt", notes.text())

    def test_near_max_counts_as_effect(self):
        ctl, notes, clock = self.hot([(4900, 5000)] * 6, fan_max=5500)
        run_ticks(ctl, 2)
        clock[0] = 5.0
        run_ticks(ctl, 1)
        self.assertTrue(notes.states[-1][0].endswith("Lüfter laufen bereits nahe Maximum"), notes.states[-1][0])
        self.assertNotIn("wirkt", notes.text())

    def test_format_status_variants(self):
        m = thermal.Messung(90.0, (3000,))
        self.assertTrue(thermal.format_status(m, True).endswith("Lüfter voll"))
        self.assertTrue(thermal.format_status(m, True, fan_effect="pending").endswith("Lüfter voll angefordert"))
        self.assertTrue(thermal.format_status(m, True, fan_effect="none").endswith(
            "Lüfter voll angefordert (keine Wirkung messbar)"))
        self.assertIn("Lüftersteuerung nicht verfügbar (Grund)", thermal.format_status(m, False, fan_reason="Grund"))


# --------------------------------------------------------------------------- M4/M5: Zustandsdatei
class StateFileTests(unittest.TestCase):
    def test_written_before_first_switch_and_removed_after_restore(self):
        h = FakeLegionHelper(feature=0)
        store = new_store()
        p = legion(h, state_store=store)
        p.open()
        self.assertFalse(store.path.exists(), "open() schreibt noch nichts")
        seen = {}
        h.on_set = lambda: seen.setdefault("existed", store.path.exists())
        p.fan_full()
        self.assertTrue(seen["existed"], "Datei muss VOR dem ersten Schalten existieren")
        data = json.loads(store.path.read_text(encoding="utf-8"))
        self.assertEqual((data["pid"], data["prev"], data["feature_id"]), (os.getpid(), 0, thermal.FEATURE_ID_FAN_FULL_SPEED))
        self.assertIn("pstart", data)
        self.assertEqual(list(store.dir.glob("*.tmp")), [])
        p.fan_restore()
        self.assertFalse(store.path.exists())

    def test_previous_state_is_read_right_before_first_switch(self):
        h = FakeLegionHelper(feature=0)
        store = new_store()
        p = legion(h, state_store=store)
        p.open()
        h.feature = 1  # inzwischen hat jemand anderes auf "voll" gestellt
        p.fan_full()
        self.assertEqual(json.loads(store.path.read_text(encoding="utf-8"))["prev"], 1)
        p.fan_restore()
        self.assertEqual(h.feature, 1)

    def test_write_failure_means_no_switching(self):
        h = FakeLegionHelper(feature=0)
        store = new_store()
        p = legion(h, state_store=store)
        p.open()
        with mock.patch.object(thermal.os, "open", side_effect=OSError("Platte schreibgeschuetzt")):
            with self.assertRaises(thermal.ProviderError):
                p.fan_full()
        self.assertEqual(h.writes(), [], "ohne Zustandsdatei wird nicht geschaltet")
        self.assertFalse(store.path.exists())
        self.assertEqual(list(store.dir.glob("*.tmp")), [])

    def test_unwritable_directory_means_not_switching_from_the_start(self):
        with tmpdir() as d:
            blocker = d / "datei"
            blocker.write_text("x")
            store = thermal.FanStateStore(blocker / "unterordner")  # mkdir scheitert
            h = FakeLegionHelper(feature=0)
            p = legion(h, state_store=store)
            p.open()
            can, note = p.fan_control_info()
            self.assertFalse(can)
            self.assertIn("nicht gesichert", note)
            with self.assertRaises(thermal.ProviderError):
                p.fan_full()
            self.assertEqual(h.writes(), [])

    def _write_state(self, store, pid, prev, pstart="auto"):
        from xdroot_converter import converter as cv
        store.dir.mkdir(parents=True, exist_ok=True)
        if pstart == "auto":
            pstart = cv._proc_start_time(pid)
        store.path.write_text(json.dumps({"version": 1, "pid": pid, "pstart": pstart,
                                          "feature_id": thermal.FEATURE_ID_FAN_FULL_SPEED, "prev": prev}), encoding="utf-8")

    def test_live_owner_means_this_run_only_measures(self):
        child = subprocess.Popen([PY, "-c", "import time; time.sleep(60)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        h = FakeLegionHelper(feature=1)
        store = new_store()
        self._write_state(store, child.pid, 0)
        p = legion(h, state_store=store)
        p.open()
        can, note = p.fan_control_info()
        self.assertFalse(can)
        self.assertIn("Lüftersteuerung wird von einem anderen Lauf benutzt", note)
        self.assertEqual(h.writes(), [])
        with self.assertRaises(thermal.ProviderError):
            p.fan_full()
        self.assertEqual(h.writes(), [])
        self.assertTrue(store.path.exists(), "Datei des anderen Laufs bleibt unangetastet")
        self.assertEqual(p.measure().cpu_temp_c, 60)  # messen geht
        # der Statustext nennt den Grund
        notes = Notes()
        ctl = thermal.ThermalController(p, None, FakePause([]), notes.notify, notes.status)
        ctl.setup()
        ctl.tick(thermal.Messung(70.0, (2000,)))
        self.assertIn("von einem anderen Lauf benutzt", notes.states[-1][0])

    def test_dead_owner_state_is_restored_and_logged(self):
        child = subprocess.Popen([PY, "-c", "pass"])
        child.wait()
        h = FakeLegionHelper(feature=1)  # steht noch auf "voll"
        store = new_store()
        self._write_state(store, child.pid, 0)
        p = legion(h, state_store=store)
        p.open()
        self.assertEqual(h.feature, 0, "gespeicherter Zustand wiederhergestellt")
        self.assertFalse(store.path.exists())
        self.assertTrue(p.fan_control_info()[0], "danach darf dieser Lauf schalten")
        notes = Notes()
        ctl = thermal.ThermalController(p, None, FakePause([]), notes.notify, notes.status)
        ctl.setup()
        self.assertIn("Lüfterzustand nach abgebrochenem Lauf wiederhergestellt", notes.text("info"))

    def test_reused_pid_with_other_start_time_counts_as_dead(self):
        child = subprocess.Popen([PY, "-c", "import time; time.sleep(60)"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        store = new_store()
        from xdroot_converter import converter as cv
        real = cv._proc_start_time(child.pid)
        if real is None:
            self.skipTest("Prozess-Startzeit nicht ermittelbar")
        self._write_state(store, child.pid, 0, pstart=real + 12345)
        h = FakeLegionHelper(feature=1)
        p = legion(h, state_store=store)
        p.open()
        self.assertEqual(h.feature, 0)
        self.assertTrue(p.fan_control_info()[0])

    def test_failed_restore_after_abort_blocks_switching(self):
        child = subprocess.Popen([PY, "-c", "pass"])
        child.wait()
        h = FakeLegionHelper(feature=1, ignore_writes=True)  # Zurueckschreiben wirkt nicht
        store = new_store()
        self._write_state(store, child.pid, 0)
        p = legion(h, state_store=store)
        p.open()
        can, note = p.fan_control_info()
        self.assertFalse(can)
        self.assertIn("vorheriger Lauf nicht sauber beendet", note)
        self.assertTrue(store.path.exists())

    def test_corrupt_file_is_removed_with_warning(self):
        store = new_store()
        store.dir.mkdir(parents=True, exist_ok=True)
        store.path.write_text("{kaputt", encoding="utf-8")
        h = FakeLegionHelper(feature=0)
        p = legion(h, state_store=store)
        p.open()
        self.assertTrue(p.fan_control_info()[0])
        self.assertFalse(store.path.exists())
        self.assertTrue(any("beschädigt" in t for _l, t in p.take_messages()))

    def test_failed_restore_in_run_keeps_file_for_next_run(self):
        h = FakeLegionHelper(feature=0)
        store = new_store()
        p = legion(h, state_store=store)
        p.open()
        p.fan_full()
        h.ignore_writes = True  # Zurueckstellen wirkt nicht
        with self.assertRaises(thermal.ProviderError):
            p.fan_restore()
        self.assertTrue(store.path.exists())

    def test_default_location(self):
        with mock.patch.object(thermal.sys, "platform", "win32"), mock.patch.dict(os.environ, {"LOCALAPPDATA": "X:\\L"}):
            self.assertEqual(thermal.default_state_dir(), Path("X:\\L") / "XDROOT-Converter")
        with mock.patch.object(thermal.sys, "platform", "linux"):
            self.assertEqual(thermal.default_state_dir(), Path.home() / ".cache" / "xdroot-converter")

    def test_no_dir_known_means_unwritable(self):
        self.assertTrue(thermal.FanStateStore.__init__)
        s = thermal.FanStateStore(None)
        s.dir = None
        self.assertIn("kein Ordner", s.check_writable())


# --------------------------------------------------------------------------- M6: Pause-Hoechstdauer
class PauseLimitTests(unittest.TestCase):
    def make(self, temps, **cfg):
        log = []
        prov = FakeProvider(temps, log)
        notes = Notes()
        clock = [0.0]
        aborts = []
        ctl = thermal.ThermalController(prov, thermal.ThermalConfig(**cfg), FakePause(log), notes.notify, notes.status,
                                        clock=lambda: clock[0], on_abort=aborts.append)
        prov.open()
        ctl.setup()
        return ctl, log, notes, clock, aborts

    def test_progress_line_every_two_minutes(self):
        ctl, log, notes, clock, aborts = self.make([99] * 40)
        run_ticks(ctl, 2)
        self.assertTrue(ctl.paused)
        for t in range(5, 1200, 5):
            clock[0] = float(t)
            run_ticks(ctl, 1)
            if aborts:
                break
        lines = [x for x in notes.text("warn").splitlines() if x.startswith("Pause dauert seit")]
        self.assertGreaterEqual(len(lines), 8)  # alle 120 s bis 20 min
        self.assertIn("Pause dauert seit 2 min, CPU 99 °C", lines[0])
        self.assertIn("Pause dauert seit 4 min, CPU 99 °C", lines[1])

    def test_abort_after_max_pause(self):
        ctl, log, notes, clock, aborts = self.make([99] * 400)
        run_ticks(ctl, 2)
        self.assertEqual(thermal.DEFAULT_MAX_PAUSE_S, 1200.0)
        clock[0] = 1199.0
        run_ticks(ctl, 1)
        self.assertFalse(aborts)
        clock[0] = 1200.0
        run_ticks(ctl, 1)
        self.assertEqual(len(aborts), 1)
        reason = aborts[0]
        self.assertIn("Abgebrochen: CPU kühlt nicht ab (99 °C nach 20 min Pause)", reason)
        self.assertIn("Rechner abkühlen lassen, Lüftung prüfen, dann erneut starten (setzt fort)", reason)
        self.assertIn("'CPU-Kerne begrenzen'", reason)
        self.assertFalse(ctl.paused)
        self.assertIn("resume", log)  # erst fortsetzen, dann abbrechen
        self.assertEqual(ctl.stats.pause_seconds, 1200.0)
        self.assertEqual(ctl.stats.abort_reason, reason)
        n = len(log)
        run_ticks(ctl, 3)
        self.assertEqual(len(aborts), 1, "nur einmal")
        self.assertEqual(len(log), n, "nach dem Abbruch wird nicht mehr gemessen/geschaltet")
        ctl.finish()
        self.assertEqual(ops(log)[-1], "fan_restore")

    def test_max_pause_is_configurable(self):
        ctl, log, notes, clock, aborts = self.make([99] * 20, max_pause_s=60.0)
        run_ticks(ctl, 2)
        clock[0] = 60.0
        run_ticks(ctl, 1)
        self.assertEqual(len(aborts), 1)
        self.assertIn("nach 60 s Pause", aborts[0])

    def test_cooling_down_in_time_does_not_abort(self):
        ctl, log, notes, clock, aborts = self.make([99, 99, 60, 60, 60])
        run_ticks(ctl, 2)
        clock[0] = 600.0
        run_ticks(ctl, 3)
        self.assertFalse(ctl.paused)
        self.assertFalse(aborts)

    def test_invalid_max_pause_rejected(self):
        with self.assertRaises(ValueError):
            thermal.ThermalConfig(max_pause_s=0).validate()


class PauseLimitRunTests(RunTestBase):
    def run_hot(self, **kw):
        prov = FakeProvider([99], [])
        opts = converter.Options(source=str(self.src), target=str(self.tgt), thermal="auto", thermal_interval_s=0.01,
                                 thermal_provider=lambda: prov, thermal_max_pause_s=0.3, **kw)
        rep = Rep()
        statuses = []
        rep.thermal_status = lambda text, state: statuses.append((text, state))
        conv = converter.Converter(opts, rep)

        def tr(c, plan, fps, frames, v_path, wavs):
            proc = mock.Mock()
            proc.poll.return_value = None
            c.cancel.register(proc)  # ein laufender Prozess, den die Pause anhaelt
            wait_for(lambda: c.cancel.cancelled, 20)
            c.cancel.check()

        with mock.patch.object(tools, "suspend_process", lambda p: True), \
                mock.patch.object(tools, "resume_process", lambda p: True), \
                mock.patch.object(converter.Converter, "_transcode", tr):
            s = conv.run()
        return s, rep, conv, statuses

    def test_run_is_cancelled_cleanly_with_own_reason(self):
        s, rep, conv, statuses = self.run_hot()
        self.assertTrue(s.cancelled)
        self.assertIn("Abgebrochen: CPU kühlt nicht ab", s.abort_reason)
        self.assertEqual(s.fatal, "")
        self.assertNotIn(s.abort_reason, rep.text("error"), "Hitze-Abbruch ist kein FEHLER")
        self.assertIn(s.abort_reason, rep.text("warn"))
        text = s.text()
        self.assertIn("Fehler: 0", text)
        self.assertIn("ABGEBROCHEN", text)
        self.assertIn(s.abort_reason, text)
        log = (self.tgt / converter.LOG_NAME).read_text(encoding="utf-8")
        self.assertIn("CPU kühlt nicht ab", log)
        self.assertIn("Dauer", text)
        self.assertIn("(davon Pause", text)
        self.assertEqual(s.pauses, 1)
        # fortsetzbar: ein zweiter Lauf ohne Ueberwachung wandelt alles um
        s2, _rep, _ = self.run_conv()
        self.assertEqual((s2.count(converter.ST_CONVERTED), s2.failed), (2, 0))

    def test_gui_completion_window_and_status(self):
        s, rep, conv, statuses = self.run_hot()
        kind, title, text = gui.completion_message(s)
        self.assertEqual((kind, title), ("warning", "Abgebrochen"))
        self.assertIn("Abgebrochen: CPU kühlt nicht ab", text)
        self.assertIn("Temperaturpausen: 1, gesamt", text)
        self.assertIn("kühlt nicht ab", gui.finish_status(s))

    def test_cli_exit_code_like_cancel(self):
        summary = converter.RunSummary(cancelled=True, abort_reason="Abgebrochen: CPU kühlt nicht ab (99 °C)")

        class FakeConv:
            def __init__(self, opts, rep, cancel):
                pass

            def run(self):
                return summary

        import signal
        saved = {n: signal.getsignal(getattr(signal, n)) for n in ("SIGINT", "SIGTERM") if hasattr(signal, n)}
        for n, hd in saved.items():
            self.addCleanup(signal.signal, getattr(signal, n), hd)
        with mock.patch.object(converter, "Converter", FakeConv), mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(cli.main(["q", "z"]), 130)


# --------------------------------------------------------------------------- M7: Start mit Administratorrechten
class ElevateTests(unittest.TestCase):
    def fake_python_dir(self, d):
        (d / "python.exe").write_bytes(b"")
        (d / "pythonw.exe").write_bytes(b"")
        return str(d / "python.exe")

    def test_gui_launch_arguments(self):
        calls = []
        with tmpdir() as d:
            exe = self.fake_python_dir(d)
            rc = elevate.relaunch_as_admin(False, shell_execute=lambda *a: calls.append(a) or 42, out=lambda s="": None,
                                           executable=exe)
        self.assertEqual(rc, 0)
        verb, prog, params, cwd, show = calls[0]
        self.assertEqual(verb, "runas")
        self.assertTrue(prog.endswith("pythonw.exe") and os.path.isabs(prog))
        script = (ROOT / "start_gui.pyw").resolve()
        self.assertEqual(params, f'-E -s "{script}"')
        self.assertTrue(os.path.isabs(params.split('"')[1]))
        self.assertEqual(Path(cwd).resolve(), ROOT.resolve())
        self.assertEqual(show, 1)

    def test_console_launch_uses_python_exe_and_thermal_script(self):
        calls = []
        with tmpdir() as d:
            exe = self.fake_python_dir(d)
            elevate.relaunch_as_admin(True, elevate.THERMAL_SCRIPT, shell_execute=lambda *a: calls.append(a) or 33,
                                      out=lambda s="": None, executable=str(d / "pythonw.exe"))
        self.assertTrue(calls[0][1].endswith("python.exe"))
        self.assertEqual(calls[0][2], f'-E -s "{(ROOT / "start_thermal_test.py").resolve()}"')

    def test_return_code_up_to_32_is_failure_with_clear_message(self):
        for code, word in ((5, "abgelehnt"), (2, "Fehlercode 2"), (32, "Fehlercode 32")):
            msgs = []
            rc = elevate.relaunch_as_admin(False, shell_execute=lambda *a, c=code: c, out=msgs.append)
            self.assertEqual(rc, 1)
            self.assertTrue(any(word in m for m in msgs), (code, msgs))

    def test_exception_in_shell_execute_is_contained(self):
        msgs = []

        def boom(*a):
            raise OSError("kaputt")

        self.assertEqual(elevate.relaunch_as_admin(False, shell_execute=boom, out=msgs.append), 1)
        self.assertTrue(msgs)

    def test_missing_script(self):
        msgs = []
        with tmpdir() as d:
            rc = elevate.relaunch_as_admin(False, project_dir=d, shell_execute=lambda *a: 99, out=msgs.append)
        self.assertEqual(rc, 1)
        self.assertIn("nicht gefunden", msgs[0])

    def test_main_module_flag(self):
        from xdroot_converter import __main__ as m
        calls = []
        with mock.patch.object(thermal, "is_admin", lambda: False), \
                mock.patch.object(elevate, "_shell_execute", lambda *a: calls.append(a) or 42):
            with self.assertRaises(SystemExit) as cm:
                m.run(["--als-administrator"])
        self.assertEqual(cm.exception.code, 0)
        self.assertEqual(calls[0][0], "runas")
        self.assertTrue(calls[0][2].strip('"').endswith("start_gui.pyw"))
        with mock.patch.object(thermal, "is_admin", lambda: False), \
                mock.patch.object(elevate, "_shell_execute", lambda *a: 5), mock.patch("sys.stderr", io.StringIO()) as err:
            with self.assertRaises(SystemExit) as cm:
                m.run(["--als-administrator"])
        self.assertEqual(cm.exception.code, elevate.EXIT_ELEVATION_FAILED)
        self.assertIn("abgelehnt", err.getvalue())

    def test_main_module_flag_does_nothing_special_when_already_admin(self):
        from xdroot_converter import __main__ as m
        with mock.patch.object(thermal, "is_admin", lambda: True), \
                mock.patch.object(elevate, "_shell_execute", side_effect=AssertionError("keine erneute Erhoehung")), \
                mock.patch.object(elevate, "start_detached", return_value=0) as sd, \
                mock.patch("xdroot_converter.gui.main") as gm:
            with self.assertRaises(SystemExit) as cm:
                m.run(["--als-administrator"])
        self.assertEqual(cm.exception.code, 0)  # das Konsolenfenster der .bat schliesst sich
        sd.assert_called_once()
        gm.assert_not_called()

    def test_cli_thermal_test_as_admin(self):
        calls = []
        with mock.patch.object(thermal, "is_admin", lambda: False), \
                mock.patch.object(elevate, "_shell_execute", lambda *a: calls.append(a) or 42), \
                mock.patch.object(thermal, "run_thermal_test", side_effect=AssertionError("nicht ohne Rechte")), \
                mock.patch("sys.stdout", io.StringIO()):
            rc = cli.main(["--thermal-test", "--als-administrator"])
        self.assertEqual(rc, 0)
        self.assertTrue(calls[0][2].split('"')[1].endswith("start_thermal_test.py"))
        self.assertTrue(os.path.isabs(calls[0][2].split('"')[1]))
        self.assertTrue(calls[0][2].startswith("-E -s "))
        self.assertTrue(os.path.isabs(calls[0][1]))
        with mock.patch.object(thermal, "is_admin", lambda: True), \
                mock.patch.object(thermal, "run_thermal_test", return_value=0) as rt, \
                mock.patch("builtins.input", lambda p="": asked.append(p) or ""), mock.patch("sys.stdout", io.StringIO()):
            asked = []
            self.assertEqual(cli.main(["--thermal-test", "--als-administrator"]), 0)
        rt.assert_called_once()
        self.assertEqual(len(asked), 1, "schon erhoeht: Test im selben Fenster, am Ende Enter-Abfrage")
        # Ergebnis negativ (1) bleibt 1; Erhoehung abgelehnt ist ein eigener Code (3)
        with mock.patch.object(thermal, "is_admin", lambda: True), mock.patch.object(thermal, "run_thermal_test", return_value=1), \
                mock.patch("builtins.input", lambda p="": ""), mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(cli.main(["--thermal-test", "--als-administrator"]), 1)
        with mock.patch.object(thermal, "is_admin", lambda: False), mock.patch.object(elevate, "_shell_execute", lambda *a: 5), \
                mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(cli.main(["--thermal-test", "--als-administrator"]), elevate.EXIT_ELEVATION_FAILED)
        # Strg+C im Test: Abfrage am Ende kommt trotzdem
        asked2 = []
        with mock.patch.object(thermal, "is_admin", lambda: True), \
                mock.patch.object(thermal, "run_thermal_test", side_effect=KeyboardInterrupt), \
                mock.patch("builtins.input", lambda p="": asked2.append(p) or ""), mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(cli.main(["--thermal-test", "--als-administrator"]), 1)
        self.assertEqual(len(asked2), 1)

    def test_cli_flag_alone_is_an_error(self):
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as cm:
            cli.main(["--als-administrator"])
        self.assertEqual(cm.exception.code, 2)

    def test_thermal_test_with_source_and_target_prints_hint(self):
        out = io.StringIO()
        with mock.patch.object(thermal, "run_thermal_test", return_value=0) as rt, \
                mock.patch.object(converter, "Converter") as conv, mock.patch("sys.stdout", out):
            self.assertEqual(cli.main(["q", "z", "--thermal-test"]), 0)
        rt.assert_called_once()
        conv.assert_not_called()
        self.assertIn("NUR der Test", out.getvalue())

    # ---- Startskripte
    def load(self, name):
        loader = importlib.machinery.SourceFileLoader("start_script_" + name.split(".")[0], str(ROOT / name))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod

    def test_start_gui_reports_any_exception_in_a_message_box(self):
        mod = self.load("start_gui.pyw")
        old_cwd, old_path = os.getcwd(), list(sys.path)
        self.addCleanup(os.chdir, old_cwd)
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), old_path))
        shown = []
        with tmpdir() as d:
            mod.HERE = str(d)
            with mock.patch("xdroot_converter.gui.main", side_effect=RuntimeError("Fehler beim Start")), \
                    mock.patch("tkinter.Tk"), \
                    mock.patch("tkinter.messagebox.showerror", lambda *a, **k: shown.append(a)):
                self.assertEqual(mod.main(), 1)
            self.assertTrue(shown and "RuntimeError" in shown[0][1] and "Fehler beim Start" in shown[0][1])
            txt = (d / "start_fehler.txt").read_text(encoding="utf-8")  # die Einzelheiten stehen IMMER in der Datei
            self.assertIn("Fehler beim Start", txt)
            self.assertIn(str(d / "start_fehler.txt"), shown[0][1], "das Fenster nennt den Pfad")
            self.assertIn("Ist Python 3.11 oder neuer mit tkinter installiert?", shown[0][1])

    def test_start_gui_falls_back_to_file_without_tkinter(self):
        mod = self.load("start_gui.pyw")
        old_cwd, old_path = os.getcwd(), list(sys.path)
        self.addCleanup(os.chdir, old_cwd)
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), old_path))
        with tmpdir() as d:
            mod.HERE = str(d)
            with mock.patch("xdroot_converter.gui.main", side_effect=ValueError("kaputt")), \
                    mock.patch("tkinter.Tk", side_effect=RuntimeError("kein Display")):
                self.assertEqual(mod.main(), 1)
            txt = (d / "start_fehler.txt").read_text(encoding="utf-8")
            self.assertIn("ValueError", txt)
            self.assertIn("kaputt", txt)

    def test_start_gui_sets_path_and_cwd_and_starts_gui(self):
        mod = self.load("start_gui.pyw")
        old_cwd, old_path = os.getcwd(), list(sys.path)
        self.addCleanup(os.chdir, old_cwd)
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), old_path))
        with tmpdir() as d, mock.patch("xdroot_converter.gui.main") as gm:
            mod.HERE = str(d)
            self.assertEqual(mod.main(), 0)
            self.assertEqual(sys.path[0], str(d))
            self.assertEqual(Path(os.getcwd()).resolve(), d.resolve())
        gm.assert_called_once()
        self.assertTrue(Path(mod.__file__).resolve() == (ROOT / "start_gui.pyw").resolve())

    def test_start_thermal_test_waits_for_enter(self):
        mod = self.load("start_thermal_test.py")
        old_cwd, old_path = os.getcwd(), list(sys.path)
        self.addCleanup(os.chdir, old_cwd)
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), old_path))
        asked = []
        with mock.patch.object(cli, "main", return_value=0) as cm, mock.patch("builtins.input", lambda p="": asked.append(p) or ""):
            self.assertEqual(mod.main(), 0)
        cm.assert_called_once_with(["--thermal-test"])
        self.assertEqual(len(asked), 1)
        asked.clear()
        with mock.patch.object(cli, "main", side_effect=RuntimeError("x")), mock.patch("builtins.input", lambda p="": asked.append(p) or ""), \
                mock.patch("traceback.print_exc"):
            self.assertEqual(mod.main(), 1)
        self.assertEqual(len(asked), 1, "Fenster bleibt auch nach einem Fehler offen")

    # ---- .bat-Dateien
    def bat(self, name):
        return (ROOT / name).read_text(encoding="ascii")

    def test_admin_bat_files(self):
        normal = self.bat("XDROOT-Converter.bat")
        for name, call in (("XDROOT-Converter (Administrator).bat", "-m xdroot_converter --als-administrator"),
                           ("Temperatur-Test (Administrator).bat", "-m xdroot_converter.cli --thermal-test --als-administrator")):
            txt = self.bat(name)
            self.assertIn(call, txt)
            low = txt.lower()
            self.assertNotIn("powershell", low)
            self.assertNotIn("for /f", low)
            self.assertNotIn("start-process", low)
            # Python wird exakt wie in der normalen .bat gesucht
            for line in ('py -3 -c "import sys,tkinter; sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>nul',
                         'python -c "import sys,tkinter; sys.exit(0 if sys.version_info>=(3,11) else 1)" >nul 2>nul'):
                self.assertIn(line, normal)
                self.assertIn(line, txt)
            self.assertIn("pushd", txt)
        self.assertNotIn("als-administrator", normal)  # die normale .bat bleibt unveraendert


# --------------------------------------------------------------------------- M8: Pausen im Abschlussfenster
class SummaryPauseTests(unittest.TestCase):
    def test_completion_window_mentions_pauses(self):
        s = converter.RunSummary(pauses=3, pause_seconds=252, max_cpu_c=98.0)
        s.seconds = 1000
        kind, title, text = gui.completion_message(s)
        self.assertIn("Temperaturpausen: 3, gesamt 4 min 12 s (höchste CPU-Temperatur 98 °C)", text)

    def test_no_line_without_pauses(self):
        s = converter.RunSummary(pauses=0, pause_seconds=0, max_cpu_c=70.0)
        self.assertNotIn("Temperaturpausen", gui.completion_message(s)[2])
        self.assertNotIn("Temperaturpausen", gui.completion_message(converter.RunSummary())[2])

    def test_summary_duration_with_pause(self):
        s = converter.RunSummary(pauses=2, pause_seconds=252)
        s.seconds = 3600
        s.results.append(converter.ClipResult(name="A.MXF", status=converter.ST_CONVERTED))
        self.assertIn("Dauer 1:00:00 (davon Pause 4:12)", s.text())
        s2 = converter.RunSummary()
        s2.seconds = 60
        s2.results.append(converter.ClipResult(name="A.MXF", status=converter.ST_CONVERTED))
        self.assertNotIn("davon Pause", s2.text())

    def test_abort_reason_in_text_and_window(self):
        s = converter.RunSummary(cancelled=True, abort_reason="Abgebrochen: CPU kühlt nicht ab (99 °C nach 20 min Pause).")
        self.assertIn("kühlt nicht ab", s.text())
        self.assertIn("kühlt nicht ab", gui.completion_message(s)[2])


# --------------------------------------------------------------------------- K1/K2/K3/K4/K7
class SmallFixTests(unittest.TestCase):
    def test_cli_limits_like_gui(self):
        p = cli.build_parser()
        for bad, word in ((["--temp-fan", "40"], "40"), (["--temp-fan", "111"], "111"), (["--temp-pause", "59"], "59"),
                          (["--temp-pause", "121"], "121")):
            err = io.StringIO()
            with mock.patch("sys.stderr", err), self.assertRaises(SystemExit):
                cli.main(["q", "z", *bad])
            self.assertIn(word, err.getvalue())
            self.assertEqual(err.getvalue().count(bad[0] + ":"), 1, "Optionsname nicht doppelt")
        # bei ausgeschalteter Ueberwachung werden die Grenzen nicht geprueft (wie in der Oberflaeche)
        a = p.parse_args(["q", "z", "--thermal", "off", "--temp-fan", "40"])
        self.assertEqual(a.temp_fan, 40.0)
        a = p.parse_args(["q", "z", "--temp-fan", "50", "--temp-pause", "120"])
        self.assertEqual((a.temp_fan, a.temp_pause), (50.0, 120.0))

    def test_cli_relation_message_names_entered_values(self):
        err = io.StringIO()
        with mock.patch("sys.stderr", err), self.assertRaises(SystemExit):
            cli.main(["q", "z", "--temp-fan", "99", "--temp-pause", "90"])
        msg = err.getvalue()
        self.assertIn("--temp-pause (90 °C)", msg)
        self.assertIn("--temp-fan (99 °C)", msg)
        self.assertNotIn("Rueckschwelle", msg)  # nicht der abgeleitete Wert

    def test_check_limits_shared(self):
        thermal.check_limits(85, 97)
        for fan, pause in ((40, 97), (85, 130), (97, 97), (99, 90)):
            with self.assertRaises(ValueError):
                thermal.check_limits(fan, pause)
        self.assertEqual((gui.TEMP_FAN_MIN, gui.TEMP_FAN_MAX, gui.TEMP_PAUSE_MIN, gui.TEMP_PAUSE_MAX),
                         (50.0, 110.0, 60.0, 120.0))

    def test_threads_at_or_above_cpu_count_is_a_hint_not_an_error(self):
        class T(RunTestBase):
            def runTest(self):
                pass

        t = T()
        t.setUp()
        try:
            with mock.patch.object(converter.os, "cpu_count", lambda: 4):
                s, rep, _ = t.run_conv(threads=4)
                self.assertEqual((s.count(converter.ST_CONVERTED), s.failed), (2, 0))
                self.assertIn("begrenzt nichts", rep.text("info"))
                shutil.rmtree(t.tgt, ignore_errors=True)
                s, rep, _ = t.run_conv(threads=8)
                self.assertIn("begrenzt nichts", rep.text("info"))
                shutil.rmtree(t.tgt, ignore_errors=True)
                s, rep, _ = t.run_conv(threads=2)
                self.assertNotIn("begrenzt nichts", rep.text("info"))
        finally:
            t.doCleanups()

    def test_cancel_token_uses_rlock(self):
        token = tools.CancelToken()
        self.assertIsInstance(token._lock, type(threading.RLock()))
        done = []

        def reenter():
            with token._lock:  # Signal-Handler im selben Thread, waehrend die Sperre gehalten wird
                token.cancel()
                done.append(1)

        t = threading.Thread(target=reenter)
        t.start()
        t.join(5)
        self.assertEqual(done, [1], "Wiedereintritt im selben Thread darf nicht blockieren")

    def test_pause_reports_success_only_if_something_was_stopped(self):
        token = tools.CancelToken()
        self.assertFalse(token.pause(), "kein Prozess registriert")
        self.assertFalse(token.paused)
        self.assertEqual(token.pause_error, "")
        proc = mock.Mock()
        proc.poll.return_value = None
        with mock.patch.object(tools, "suspend_process", lambda p: False):
            token.register(proc)
            self.assertFalse(token.pause())
        self.assertFalse(token.paused)
        self.assertEqual(token.paused_time(), 0.0)
        self.assertIn("nicht angehalten", token.pause_error)
        with mock.patch.object(tools, "suspend_process", lambda p: True), mock.patch.object(tools, "resume_process", lambda p: True):
            self.assertTrue(token.pause())
            self.assertTrue(token.paused)
            self.assertEqual(token.pause_error, "")

    def test_controller_logs_failed_suspend_once_and_counts_no_pause(self):
        class Failing(FakePause):
            pause_error = "1 Prozess(e) konnten nicht angehalten werden (NtSuspendProcess/SIGSTOP fehlgeschlagen)"

        log = []
        prov = FakeProvider([99] * 12, log)
        notes = Notes()
        ctl = thermal.ThermalController(prov, None, Failing(log, result=False), notes.notify, notes.status)
        ctl.setup()
        run_ticks(ctl, 10)
        self.assertFalse(ctl.paused)
        self.assertEqual((ctl.stats.pauses, ctl.stats.pause_seconds), (0, 0.0))
        self.assertEqual(notes.text("warn").count("nicht möglich"), 1)
        self.assertNotIn("ffmpeg angehalten", notes.text())
        self.assertNotIn("pause", [s for _t, s in notes.states])

    def test_deep_decode_threads(self):
        seen = []

        def fake(args, **kw):
            seen.append(list(args))
            return tools.ProcResult(0, "", "")

        with mock.patch.object(tools, "run_capture", fake), mock.patch.object(tools, "find_tool", lambda n: n):
            verify.deep_decode_check("x.mxf", threads=3)
            verify.deep_decode_check("x.mxf")
        a, b = seen
        i = a.index("-threads")
        self.assertEqual(a[i + 1], "3")
        self.assertLess(i, a.index("-i"))
        self.assertNotIn("-threads", b)

    def test_verify_deep_gets_threads_from_cli(self):
        rep = mock.Mock(ok=True)
        rep.text.return_value = "ok"
        with mock.patch.object(verify, "verify_card", return_value=rep) as vc, mock.patch("sys.stdout", io.StringIO()):
            cli.main(["--verify", "z", "--deep", "--threads", "2"])
            self.assertEqual(vc.call_args.kwargs["threads"], 2)
            cli.main(["--verify", "z", "--deep"])
            self.assertIsNone(vc.call_args.kwargs["threads"])

    def test_threads_help_mentions_not_bit_identical(self):
        h = cli.build_parser().format_help().replace("\n", " ")
        h = " ".join(h.split())
        self.assertIn("NICHT bitgleichen", h)
        self.assertIn("--verify --deep", h)


# --------------------------------------------------------------------------- K6: Ende des Messthreads
class SessionStopTests(unittest.TestCase):
    def test_stop_during_availability_check_does_not_wait_and_stays_silent(self):
        gate = threading.Event()
        notes = Notes()
        holder = {}

        class Hanging(FakeProvider):
            def open(self):
                holder["p"] = self
                gate.wait(30)
                raise thermal.ProviderUnavailable("zu spaet", "other")

            def abort(self):
                gate.set()

        sess = thermal.ThermalSession(thermal.ThermalConfig(), FakePause([]), notes.notify, notes.status,
                                      provider_factory=lambda: Hanging([60]), stop_wait_s=20.0)
        sess.start()
        self.assertTrue(wait_for(lambda: "p" in holder, 5))
        t0 = time.monotonic()
        sess.stop()
        self.assertLess(time.monotonic() - t0, 6.0, "stop() darf nicht bis zu 20 s warten")
        time.sleep(0.3)
        self.assertEqual((notes.items, notes.states), ([], []), "nach dem Ende keine verspaetete Zeile")

    def test_stop_while_powershell_still_starting(self):
        notes = Notes()

        def factory():
            return thermal.LegionProvider(
                helper_factory=lambda: thermal.PowerShellHelper(argv=[PY, "-c", "import time; time.sleep(60)"],
                                                                startup_timeout=30),
                platform="win32", state_store=new_store())

        sess = thermal.ThermalSession(thermal.ThermalConfig(), FakePause([]), notes.notify, notes.status,
                                      provider_factory=factory, stop_wait_s=20.0)
        sess.start()
        time.sleep(1.0)
        t0 = time.monotonic()
        sess.stop()
        self.assertLess(time.monotonic() - t0, 8.0)
        time.sleep(0.3)
        self.assertEqual(notes.items, [])

    def test_stop_with_active_controller_still_restores_and_may_log(self):
        log = []
        prov = FakeProvider([90, 90, 90], log)
        notes = Notes()
        sess = thermal.ThermalSession(thermal.ThermalConfig(), FakePause(log), notes.notify, notes.status,
                                      provider_factory=lambda: prov, interval_s=0.01)
        sess.start()
        self.assertTrue(wait_for(lambda: "fan_full" in log, 10))
        sess.stop()
        self.assertEqual(ops(log)[-1], "fan_restore")
        n = len(notes.items)
        time.sleep(0.2)
        self.assertEqual(len(notes.items), n)


if __name__ == "__main__":
    unittest.main()
