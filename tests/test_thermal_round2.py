"""Tests der Fix-Runde 2 zur Temperaturueberwachung: .bat-Dateien (statisch und im Wegwerf-Ordner mit Attrappen-Programm),
exklusive Zustandsdatei mit zwei Anbietern, strenge Pruefung der Zustandsdatei, Hilfsprozess-Sperre ohne Kill, PATH-Suche
ohne Arbeitsordner, Strg+C in der Diagnose, Wirkungspruefung mit plausiblem Maximum, GUI-Mindesthoehe."""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from xdroot_converter import cli, converter, elevate, thermal, tools

try:
    from test_thermal import (FakeLegionHelper, FakePause, FakeProvider, Notes, PY, legion, new_store, run_ticks, wait_for)
except ImportError:  # pragma: no cover
    from tests.test_thermal import (FakeLegionHelper, FakePause, FakeProvider, Notes, PY, legion, new_store, run_ticks,
                                    wait_for)

ROOT = Path(__file__).resolve().parent.parent
ADMIN_BATS = ("XDROOT-Converter (Administrator).bat", "Temperatur-Test (Administrator).bat")


@contextlib.contextmanager
def tmpdir():
    d = Path(tempfile.mkdtemp(prefix="xdroot-test2-"))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


# --------------------------------------------------------------------------- G1: .bat-Dateien
def paren_problems(text: str) -> list:
    """Zeilen in mehrzeiligen Klammerbloecken ('... (' am Zeilenende bis ')' am Zeilenanfang), die in einer echo-Zeile ein
    nicht mit ^ maskiertes '(' oder ')' haben - das schliesst den Block vorzeitig bzw. oeffnet einen neuen."""
    bad = []
    depth = 0
    for no, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if depth > 0 and re.match(r"(?i)@?echo\b", line):
            body = line.replace("^(", "").replace("^)", "")
            if "(" in body or ")" in body:
                bad.append((no, raw))
        if line.startswith(")") and depth > 0:
            depth -= 1
        if re.search(r"\(\s*$", line) and not line.lower().startswith("rem"):
            depth += 1
    return bad


class BatStaticTests(unittest.TestCase):
    def test_no_unescaped_parentheses_in_echo_lines_of_blocks(self):
        bats = sorted(ROOT.glob("*.bat"))
        self.assertTrue(len(bats) >= 3)
        for bat in bats:
            with self.subTest(bat=bat.name):
                self.assertEqual(paren_problems(bat.read_text(encoding="ascii", errors="replace")), [])

    def test_checker_finds_the_old_bug(self):
        bad = 'if errorlevel 1 (\n    echo Start (normale Startdatei XDROOT-Converter.bat).\n    pause\n)\n'
        self.assertEqual(len(paren_problems(bad)), 1)
        ok = 'if errorlevel 1 (\n    echo Start ^(maskiert^)\n)\necho ausserhalb (ok)\n'
        self.assertEqual(paren_problems(ok), [])

    def test_admin_bats_have_no_text_blocks_and_distinguish_elevation_failure(self):
        for name in ADMIN_BATS:
            txt = (ROOT / name).read_text(encoding="ascii")
            self.assertNotIn("if errorlevel 1 (", txt)
            self.assertIn('if "%RC%"=="3" goto :elevfail', txt)
            self.assertIn('if "%RC%"=="0" exit /b 0', txt)
            self.assertIn("goto :startfail", txt)
            self.assertIn("Start fehlgeschlagen (Code %RC%).", txt)
            self.assertNotIn("||", txt, "kein '|| ( ... )'-Block")
            self.assertIn(":nodir", txt)


@unittest.skipUnless(sys.platform == "win32", "cmd.exe nur unter Windows")
class BatRunTests(unittest.TestCase):
    """Fuehrt KOPIEN der beiden Administrator-.bat in einem Wegwerf-Ordner aus.  Statt des Programms liegt dort ein
    Attrappen-Paket ``xdroot_converter`` (beendet sich mit dem Exit-Code aus DUMMY_RC); die echte Erhoehung (UAC) wird nie
    ausgeloest.  stdin ist NUL, damit ``pause`` nicht blockiert."""

    FORBIDDEN = ("syntaktisch", "unexpected", "unerwartet", "syntax")

    @classmethod
    def setUpClass(cls):
        cls.have_python = False
        for exe, args in (("py", ["-3"]), ("python", [])):
            p = shutil.which(exe)
            if p:
                r = subprocess.run([p, *args, "-c", "import sys,tkinter; sys.exit(0 if sys.version_info>=(3,11) else 1)"],
                                   capture_output=True)
                if r.returncode == 0:
                    cls.have_python = True
                    break

    def make_project(self, d, bat_name):
        shutil.copy(ROOT / bat_name, d / bat_name)
        pkg = d / "xdroot_converter"
        pkg.mkdir()
        (pkg / "__init__.py").write_text("", encoding="utf-8")
        code = "import os, sys\nsys.exit(int(os.environ.get('DUMMY_RC', '0')))\n"
        (pkg / "__main__.py").write_text(code, encoding="utf-8")
        (pkg / "cli.py").write_text(code, encoding="utf-8")

    def run_bat(self, bat_name, rc, path=None):
        with tmpdir() as d:
            self.make_project(d, bat_name)
            env = dict(os.environ)
            env["DUMMY_RC"] = str(rc)
            if path is not None:
                env["PATH"] = path
            r = subprocess.run(["cmd.exe", "/d", "/c", str(d / bat_name)], cwd=str(d), env=env, stdin=subprocess.DEVNULL,
                               capture_output=True, timeout=90)
        out = (r.stdout + r.stderr).decode("cp850", errors="replace")
        for word in self.FORBIDDEN:
            self.assertNotIn(word, out.lower(), out)
        return r.returncode, out

    def test_result_codes_and_hint_only_for_failed_elevation(self):
        if not self.have_python:
            self.skipTest("kein Python 3.11+ mit tkinter im PATH")
        for name in ADMIN_BATS:
            with self.subTest(bat=name, case="ok"):
                rc, out = self.run_bat(name, 0)
                self.assertEqual(rc, 0, out)
                self.assertNotIn("abgebrochen oder ist fehlgeschlagen", out)
            with self.subTest(bat=name, case="Code 1"):
                rc, out = self.run_bat(name, 1)
                self.assertEqual(rc, 1, out)
                self.assertNotIn("abgebrochen oder ist fehlgeschlagen", out)
                if name.startswith("Temperatur"):  # das Python-Skript hat selbst auf Enter gewartet: kein zweites pause
                    self.assertNotIn("Start fehlgeschlagen", out)
                else:
                    self.assertIn("Start fehlgeschlagen (Code 1)", out)
            with self.subTest(bat=name, case="anderer Code"):
                rc, out = self.run_bat(name, 2)
                self.assertEqual(rc, 2, out)
                self.assertIn("Start fehlgeschlagen (Code 2)", out)
                self.assertNotIn("abgebrochen oder ist fehlgeschlagen", out)
            with self.subTest(bat=name, case="Erhoehung abgelehnt"):
                rc, out = self.run_bat(name, elevate.EXIT_ELEVATION_FAILED)
                self.assertEqual(rc, 1, out)
                self.assertIn("abgebrochen oder ist fehlgeschlagen", out)
                self.assertNotIn("Start fehlgeschlagen", out)

    def test_no_python_gives_readable_message(self):
        sysdir = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
        for name in ADMIN_BATS:
            with self.subTest(bat=name):
                rc, out = self.run_bat(name, 0, path=sysdir)
                self.assertEqual(rc, 1, out)
                self.assertIn("kein passendes Python", out)


# --------------------------------------------------------------------------- G2: zwei Anbieter, ein Store
class LinkedHelper(FakeLegionHelper):
    """Wie FakeLegionHelper, aber die 'Hardware' (Lueftermerker) gehoert mehreren Helfern gemeinsam."""

    def __init__(self, hw, **kw):
        self._hw = hw
        super().__init__(**kw)

    feature = property(lambda s: s._hw["feature"], lambda s, v: s._hw.__setitem__("feature", v))


class TwoProvidersTests(unittest.TestCase):
    def test_interleaved_runs_leave_original_state_and_no_file(self):
        hw = {"feature": 0}
        store = new_store()
        ha, hb = LinkedHelper(hw), LinkedHelper(hw)
        a = legion(ha, state_store=store)
        b = legion(hb, state_store=store)
        a.open()
        b.open()  # beide sehen: keine Datei, Schalten moeglich
        self.assertTrue(a.fan_control_info()[0] and b.fan_control_info()[0])
        a.fan_full()  # A: prev=0, schaltet
        self.assertEqual(hw["feature"], 1)
        with self.assertRaises(thermal.FanControlBusy):  # B liest prev=1, darf aber nicht schalten
            b.fan_full()
        self.assertEqual(hb.writes(), [], "B hat nichts geschrieben")
        a.fan_restore()  # A stellt zurueck und loescht NUR seine Datei
        b.fan_restore()  # B hat nichts zu tun
        self.assertEqual(hw["feature"], 0, "ursprünglicher Zustand")
        self.assertFalse(store.path.exists(), "keine Datei übrig")

    def test_second_run_takes_over_after_first_finished(self):
        hw = {"feature": 0}
        store = new_store()
        a = legion(LinkedHelper(hw), state_store=store)
        b = legion(LinkedHelper(hw), state_store=store)
        a.open()
        b.open()
        a.fan_full()
        a.fan_restore()
        b.fan_full()  # jetzt frei
        self.assertEqual(hw["feature"], 1)
        b.fan_restore()
        self.assertEqual((hw["feature"], store.path.exists()), (0, False))

    def test_file_is_created_exclusively_and_second_claim_fails(self):
        store = new_store()
        tok = store.claim(0, thermal.FEATURE_ID_FAN_FULL_SPEED)
        self.addCleanup(store.release, tok)
        with self.assertRaises(FileExistsError):
            store.claim(1, thermal.FEATURE_ID_FAN_FULL_SPEED)
        self.assertEqual(json.loads(store.path.read_text(encoding="utf-8"))["prev"], 0, "Datei des ersten unverändert")

    def test_release_deletes_only_own_file(self):
        store = new_store()
        tok = store.claim(0, thermal.FEATURE_ID_FAN_FULL_SPEED)
        data = json.loads(store.path.read_text(encoding="utf-8"))
        data["token"] = "fremd"  # ein anderer Lauf (andere Kennung) hat die Datei ersetzt
        store.path.write_text(json.dumps(data), encoding="utf-8")
        self.assertFalse(store.release(tok))
        self.assertTrue(store.path.exists(), "fremde Datei bleibt")
        data = json.loads(store.path.read_text(encoding="utf-8"))
        data["token"], data["pid"] = tok, os.getpid()
        data["pstart"] = (data.get("pstart") or 0) + 1 if data.get("pstart") else None
        if data["pstart"]:  # andere Prozess-Startzeit = nicht meine Datei
            store.path.write_text(json.dumps(data), encoding="utf-8")
            self.assertFalse(store.release(tok))
            self.assertTrue(store.path.exists())

    def test_dead_owner_file_is_restored_then_claimed_again(self):
        child = subprocess.Popen([PY, "-c", "pass"])
        child.wait()
        hw = {"feature": 1}  # steht noch auf "voll"
        store = new_store()
        store.dir.mkdir(parents=True, exist_ok=True)
        pstart = converter._proc_start_time(child.pid)
        store.path.write_text(json.dumps({"version": 2, "pid": child.pid, "pstart": pstart, "token": "t",
                                          "feature_id": thermal.FEATURE_ID_FAN_FULL_SPEED, "prev": 0}), encoding="utf-8")
        a = legion(LinkedHelper(hw), state_store=store)
        a.open()  # stellt wieder her (Besitzer tot)
        self.assertEqual(hw["feature"], 0)
        a.fan_full()
        self.assertEqual(hw["feature"], 1)
        a.fan_restore()
        self.assertEqual((hw["feature"], store.path.exists()), (0, False))

    def test_claim_failure_means_no_switching(self):
        hw = {"feature": 0}
        store = new_store()
        h = LinkedHelper(hw)
        a = legion(h, state_store=store)
        a.open()
        with mock.patch.object(thermal.os, "open", side_effect=PermissionError("verweigert")):
            with self.assertRaises(thermal.ProviderError):
                a.fan_full()
        self.assertEqual(h.writes(), [])
        self.assertEqual(hw["feature"], 0)


# --------------------------------------------------------------------------- G3: strenge Zustandsdatei
class StateFileValidationTests(unittest.TestCase):
    FID = thermal.FEATURE_ID_FAN_FULL_SPEED

    def good(self, **over):
        child = subprocess.Popen([PY, "-c", "pass"])
        child.wait()
        d = {"version": 2, "pid": child.pid, "pstart": None, "feature_id": self.FID, "prev": 0}
        d.update(over)
        return d

    def put(self, store, content):
        store.dir.mkdir(parents=True, exist_ok=True)
        store.path.write_text(content if isinstance(content, str) else json.dumps(content), encoding="utf-8")

    def test_invalid_contents_are_corrupt_and_never_written_to_hardware(self):
        cases = {
            "fremde Kennung": self.good(feature_id=0x12345678),
            "Kennung als Text": self.good(feature_id=str(self.FID)),
            "prev=7": self.good(prev=7),
            "prev=True": self.good(prev=True),
            "prev=-1": self.good(prev=-1),
            "prev als Text": self.good(prev="1"),
            "prev fehlt": {k: v for k, v in self.good().items() if k != "prev"},
            "pid=bool": self.good(pid=True),
            "pid als Text": self.good(pid="1234"),
            "pid=0": self.good(pid=0),
            "pid negativ": self.good(pid=-4),
            "pstart Text": self.good(pstart="gestern"),
            "pstart=0": self.good(pstart=0),
            "attempts negativ": self.good(attempts=-1),
            "attempts bool": self.good(attempts=True),
            "Liste": [1, 2, 3],
            "kaputtes JSON": "{kaputt",
            "leere Datei": "",
            "null": "null",
        }
        for label, content in cases.items():
            with self.subTest(label):
                store = new_store()
                self.put(store, content)
                self.assertEqual(store.read(), ("corrupt", None))
                h = FakeLegionHelper(feature=1)
                p = legion(h, state_store=store)
                p.open()
                self.assertEqual(h.writes(), [], "nichts in die Hardware schreiben")
                self.assertFalse(store.path.exists(), "Datei gelöscht")
                msgs = p.take_messages()
                self.assertTrue(any(l == "warn" for l, _t in msgs), msgs)
                self.assertTrue(p.fan_control_info()[0], "danach ist die Steuerung nicht gesperrt")

    def test_valid_file_is_accepted(self):
        store = new_store()
        self.put(store, self.good(prev=1, attempts=2))
        st, info = store.read()
        self.assertEqual((st, info["prev"], info["attempts"]), ("ok", 1, 2))

    def test_write_guard_allows_only_fan_full_with_zero_or_one(self):
        h = FakeLegionHelper(feature=0)
        p = legion(h)
        p.open()
        for fid, val in ((0x12345678, 1), (self.FID, 2), (self.FID, -1), (self.FID, True), (self.FID, "1"), (True, 1)):
            with self.subTest(fid=fid, val=val), self.assertRaises(thermal.ProviderError):
                p._feature_set(val, fid)
        self.assertEqual(h.writes(), [])
        p._feature_set(1)
        p._feature_set(0)
        self.assertEqual(len(h.writes()), 2)

    def test_failed_restore_with_valid_values_names_file_counts_attempts_and_gives_up_after_three(self):
        child = subprocess.Popen([PY, "-c", "pass"])
        child.wait()
        store = new_store()
        self.put(store, {"version": 2, "pid": child.pid, "pstart": None, "feature_id": self.FID, "prev": 0})
        h = FakeLegionHelper(feature=1, ignore_writes=True)  # Zurückschreiben wirkt nicht
        # Versuch 1 und 2: gesperrt, Meldung nennt den Dateipfad, Zähler steigt
        for n in (1, 2):
            p = legion(h, state_store=store)
            p.open()
            can, note = p.fan_control_info()
            self.assertFalse(can)
            self.assertNotIn(str(store.path), note, "Pfad nur im Protokoll")
            msgs = " ".join(t for _l, t in p.take_messages())
            self.assertIn(str(store.path), msgs)
            self.assertIn(f"Versuch {n})", msgs)
            self.assertEqual(json.loads(store.path.read_text(encoding="utf-8"))["attempts"], n)
        # Versuch 3: Datei wird verworfen, klare Warnung, die Steuerung ist frei
        p = legion(h, state_store=store)
        p.open()
        self.assertFalse(store.path.exists())
        warn = " ".join(t for l, t in p.take_messages() if l == "warn")
        self.assertIn("Lüfterzustand konnte nicht wiederhergestellt werden; bitte in Lenovo Vantage prüfen", warn)
        self.assertTrue(p.fan_control_info()[0])


# --------------------------------------------------------------------------- M1: Hilfsprozess ohne Kill durch Wartende
class BusyHelperTests(unittest.TestCase):
    def test_waiting_call_does_not_kill_the_helper(self):
        proc = mock.Mock()
        proc.poll.return_value = None
        h = thermal.PowerShellHelper(timeout=0.1)
        h._proc = proc
        self.assertTrue(h._lock.acquire(timeout=1))  # ein anderer Thread haelt die Sperre (z. B. beim Zurückstellen)
        try:
            with self.assertRaises(thermal.HelperBusy):
                h.call("CALL LENOVO_X Y", timeout=0.1)
        finally:
            h._lock.release()
        proc.kill.assert_not_called()
        self.assertTrue(h.alive, "der Hilfsprozess läuft weiter")
        self.assertNotIsInstance(thermal.HelperBusy("x"), thermal.HelperTimeout)


class LockingHelper(FakeLegionHelper):
    """Wie der echte Helfer: eine Sperre; eine haengende Messung haelt sie bis zu ihrem EIGENEN Zeitlimit und beendet
    dann den Prozess; ein wartender Aufruf beendet NICHTS."""

    def __init__(self, hw, hang_s=0.0, **kw):
        self._hw = hw
        self.hang_s = hang_s
        self.in_call = threading.Event()
        self.kills = []  # Namen der Aufrufer, die den Prozess beendet haben
        self.dead = False
        self._lock = threading.Lock()
        super().__init__(**kw)

    feature = property(lambda s: s._hw["feature"], lambda s, v: s._hw.__setitem__("feature", v))

    @property
    def alive(self):
        return not self.dead

    def call(self, line, timeout=None, lock_timeout=None):
        if self.dead:
            raise thermal.HelperError("Hilfsprozess nicht aktiv")
        if not self._lock.acquire(timeout=5.0):
            raise thermal.HelperBusy("belegt")
        try:
            if self.hang_s and " GetCPUTemp" in line:
                self.in_call.set()
                time.sleep(self.hang_s)
                self.dead = True
                self.kills.append(threading.current_thread().name)
                raise thermal.HelperTimeout("keine Antwort")
            return super().call(line, timeout, lock_timeout)
        finally:
            self._lock.release()

    def abort(self):
        self.dead = True


class FinishDuringHangingMeasurementTests(unittest.TestCase):
    def test_restore_succeeds_and_waiting_thread_kills_nothing(self):
        hw = {"feature": 0}
        store = new_store()
        helpers = []

        def factory():
            h = LockingHelper(hw)
            helpers.append(h)
            return h

        prov = thermal.LegionProvider(helper_factory=factory, platform="win32", state_store=store)
        notes = Notes()
        ctl = thermal.ThermalController(prov, None, FakePause([]), notes.notify, notes.status)
        prov.open()
        ctl.setup()
        hot = thermal.Messung(90.0, (3000, 3100))
        ctl.tick(hot)
        ctl.tick(hot)  # 2 Messungen in Folge -> Lüfter voll
        self.assertEqual(hw["feature"], 1)
        first = helpers[0]
        first.hang_s = 0.5  # ab jetzt haengt die naechste Messung
        t = threading.Thread(target=ctl.tick, name="messthread")  # Messung haengt (haelt die Sperre)
        t.start()
        self.assertTrue(first.in_call.wait(5))
        ctl.finish()  # gleichzeitig: Zurückstellen
        t.join(10)
        self.assertEqual(hw["feature"], 0, "Zurückstellen gelang")
        self.assertFalse(store.path.exists())
        self.assertEqual(first.kills, ["messthread"], "nur die Messung selbst hat ihr Zeitlimit gerissen")
        self.assertNotIn("ausgefallen", notes.text(), "keine verspätete Meldung 'ausgefallen'")
        self.assertEqual(ctl.failed, "")
        self.assertTrue(ctl._finished)

    def test_measure_after_close_fails_immediately_without_restart(self):
        calls = []
        prov = thermal.LegionProvider(helper_factory=lambda: calls.append(1) or FakeLegionHelper(), platform="win32",
                                      state_store=new_store())
        prov.open()
        prov.begin_close()
        n = len(calls)
        with self.assertRaises(thermal.HelperError):
            prov.measure()
        self.assertEqual(len(calls), n, "kein neuer Hilfsprozess")


class RestartLimitTests(unittest.TestCase):
    def test_at_most_three_restarts_per_run(self):
        made = []

        def factory():
            h = FakeLegionHelper(die_on="GetCPUTemp") if made else FakeLegionHelper()
            made.append(h)
            return h

        p = thermal.LegionProvider(helper_factory=factory, platform="win32", state_store=new_store())
        p.open()
        made[0].die_on = "GetCPUTemp"  # ab jetzt stirbt jede Messung
        for _ in range(6):
            with self.assertRaises(thermal.HelperError):
                p.measure()
        self.assertEqual(len(made), 1 + thermal.MAX_HELPER_RESTARTS)

    def test_abort_during_restart_stops_the_new_helper_too(self):
        class SlowStart(FakeLegionHelper):
            def __init__(self):
                super().__init__()
                self.go = threading.Event()
                self.starting = threading.Event()
                self.aborted = False

            def start(self):
                self.starting.set()
                self.go.wait(10)

            def abort(self):
                self.aborted = True
                self.go.set()

        made = []

        def factory():
            h = FakeLegionHelper() if not made else SlowStart()
            made.append(h)
            return h

        p = thermal.LegionProvider(helper_factory=factory, platform="win32", state_store=new_store())
        p.open()
        made[0].die_on = "GetCPUTemp"
        errors = []

        def run():
            try:
                p.measure()
            except thermal.ThermalError as e:
                errors.append(e)

        t = threading.Thread(target=run)
        t.start()
        self.assertTrue(wait_for(lambda: len(made) == 2 and made[1].starting.is_set(), 5))
        p.abort()  # waehrend der Neustart noch laeuft
        t.join(10)
        self.assertFalse(t.is_alive())
        self.assertTrue(made[1].aborted, "auch der neue, noch startende Hilfsprozess wurde beendet")
        self.assertEqual(len(errors), 1)


# --------------------------------------------------------------------------- M2: PATH-Suche
class FindToolTests(unittest.TestCase):
    def setUp(self):
        saved = dict(tools._cache)
        tools._cache.clear()
        self.addCleanup(lambda: (tools._cache.clear(), tools._cache.update(saved)))

    def test_working_directory_and_bat_are_never_used(self):
        with tmpdir() as d:
            (d / "ffmpeg.bat").write_text("@echo boese\n", encoding="ascii")
            (d / "ffmpeg.com").write_text("x", encoding="ascii")
            (d / "ffmpeg").write_text("x", encoding="ascii")
            old = os.getcwd()
            os.chdir(d)
            try:
                with mock.patch.object(tools, "PROJECT_ROOT", d / "leer"), \
                        mock.patch.dict(os.environ, {"PATH": os.pathsep.join(["", ".", str(d)[0:0] or "relativ"])}):
                    with self.assertRaises(tools.ToolNotFound):
                        tools.find_tool("ffmpeg")
                with mock.patch.object(tools, "PROJECT_ROOT", d / "leer"), mock.patch.dict(os.environ, {"PATH": str(d)}):
                    # absoluter Ordner mit ffmpeg.bat/.com: unter Windows zaehlt nur ffmpeg.exe
                    if sys.platform == "win32":
                        with self.assertRaises(tools.ToolNotFound):
                            tools.find_tool("ffmpeg")
            finally:
                os.chdir(old)

    def test_finds_exe_in_absolute_path_directory_as_absolute_path(self):
        with tmpdir() as d:
            exe = d / "bin" / ("ffmpeg.exe" if sys.platform == "win32" else "ffmpeg")
            exe.parent.mkdir()
            exe.write_bytes(b"")
            if sys.platform != "win32":
                exe.chmod(0o755)
            with mock.patch.object(tools, "PROJECT_ROOT", d / "leer"), mock.patch.dict(os.environ, {"PATH": str(exe.parent)}):
                found = tools.find_tool("ffmpeg")
            self.assertTrue(os.path.isabs(found))
            self.assertEqual(Path(found).resolve(), exe.resolve())

    def test_tools_folder_comes_before_path(self):
        with tmpdir() as d:
            exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
            (d / "tools" / "x").mkdir(parents=True)
            (d / "tools" / "x" / exe).write_bytes(b"")
            (d / "bin").mkdir()
            (d / "bin" / exe).write_bytes(b"")
            with mock.patch.object(tools, "PROJECT_ROOT", d), mock.patch.dict(os.environ, {"PATH": str(d / "bin")}):
                self.assertEqual(Path(tools.find_tool("ffmpeg")).resolve(), (d / "tools" / "x" / exe).resolve())

    def test_relative_path_entries_are_ignored(self):
        with tmpdir() as d:
            exe = "ffmpeg.exe" if sys.platform == "win32" else "ffmpeg"
            (d / "bin").mkdir()
            (d / "bin" / exe).write_bytes(b"")
            old = os.getcwd()
            os.chdir(d)
            try:
                with mock.patch.object(tools, "PROJECT_ROOT", d / "leer"), mock.patch.dict(os.environ, {"PATH": "bin"}):
                    with self.assertRaises(tools.ToolNotFound):
                        tools.find_tool("ffmpeg")
            finally:
                os.chdir(old)

    def test_module_does_not_use_shutil_which(self):
        self.assertNotIn("shutil.which", Path(tools.__file__).read_text(encoding="utf-8").replace("statt shutil.which", ""))


# --------------------------------------------------------------------------- M3: Strg+C in der Diagnose
class ThermalTestInterruptTests(unittest.TestCase):
    def run_test(self, prov):
        lines = []
        rc = thermal.run_thermal_test(lines.append, provider_factory=lambda: prov, sleep=lambda s: None, full_seconds=0.01,
                                      samples=3, input_fn=lambda p="": "")
        return rc, lines

    def test_ctrl_c_during_measurements_is_a_clean_abort(self):
        h = FakeLegionHelper(feature=0)
        p = legion(h)
        real = p.measure
        n = [0]

        def measure():
            n[0] += 1
            if n[0] == 2:
                raise KeyboardInterrupt
            return real()

        p.measure = measure
        rc, lines = self.run_test(p)
        self.assertEqual(rc, 1)
        self.assertEqual(lines[-1], "ERGEBNIS: abgebrochen (es wurde nichts geschaltet)")
        self.assertEqual(h.writes(), [])
        self.assertTrue(h.closed)

    def test_ctrl_c_while_opening(self):
        class P(thermal.ThermalProvider):
            name = "x"

            def open(self):
                raise KeyboardInterrupt

        rc, lines = self.run_test(P())
        self.assertEqual((rc, lines[-1]), (1, "ERGEBNIS: abgebrochen (es wurde nichts geschaltet)"))

    def test_ctrl_c_after_switching_restores_safely(self):
        h = FakeLegionHelper(feature=0)
        p = legion(h)
        real = p.measure
        state = {"after": False}

        def measure():
            if h.feature == 1 and not state["after"]:
                state["after"] = True
                raise KeyboardInterrupt
            return real()

        p.measure = measure
        rc, lines = self.run_test(p)
        self.assertEqual(rc, 1)
        self.assertIn("abgebrochen", lines[-1])
        self.assertEqual(h.feature, 0, "Lüfter zurückgestellt")

    def load_script(self):
        loader = importlib.machinery.SourceFileLoader("start_thermal_r2", str(ROOT / "start_thermal_test.py"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod

    def test_start_script_always_asks_for_enter(self):
        mod = self.load_script()
        old_cwd, old_path = os.getcwd(), list(sys.path)
        self.addCleanup(os.chdir, old_cwd)
        self.addCleanup(lambda: sys.path.__setitem__(slice(None), old_path))
        for exc in (KeyboardInterrupt(), RuntimeError("x"), MemoryError()):
            asked = []
            with mock.patch.object(cli, "main", side_effect=exc), mock.patch("builtins.input", lambda p="": asked.append(p) or ""), \
                    mock.patch("traceback.print_exc"), mock.patch("builtins.print"):
                self.assertEqual(mod.main(), 1, exc)
            self.assertEqual(len(asked), 1, exc)
        asked = []
        with mock.patch.object(cli, "main", side_effect=SystemExit(5)),                 mock.patch("builtins.input", lambda p="": asked.append(p) or ""):
            self.assertEqual(mod.main(), 5, "SystemExit: erst Enter, dann derselbe Exit-Code")
        self.assertEqual(len(asked), 1)
        self.assertIn("drücken", asked[0])


# --------------------------------------------------------------------------- M5: Wirkungspruefung
class FanEffectPlausibilityTests(unittest.TestCase):
    def test_plausible_fan_max(self):
        self.assertEqual(thermal.plausible_fan_max(5500, 3000), 5500)
        for bad in (100, 999, 12001, None, "x", 2000):
            self.assertIsNone(thermal.plausible_fan_max(bad, 3000), bad)

    def test_thermal_test_prints_raw_max_and_near_max_verdict(self):
        h = FakeLegionHelper(feature=0, fan1=5200, fan2=5300, fan_max=5500)
        lines = []
        rc = thermal.run_thermal_test(lines.append, provider_factory=lambda: legion(h, trace=lines.append),
                                      sleep=lambda s: time.sleep(0.01), full_seconds=0.05, samples=2, input_fn=lambda p="": "")
        out = "\n".join(lines)
        self.assertIn("GetFanMaxSpeed (Rohwert, Einheit unbekannt): 5500", out)
        self.assertIn("nahe Maximum", lines[-1])
        self.assertEqual(rc, 0)

    def test_raw_max_in_other_unit_is_shown_as_implausible(self):
        h = FakeLegionHelper(feature=0, fan_max=100)
        lines = []
        thermal.run_thermal_test(lines.append, provider_factory=lambda: legion(h), sleep=lambda s: time.sleep(0.01),
                                 full_seconds=0.05, samples=2, input_fn=lambda p="": "")
        out = "\n".join(lines)
        self.assertIn("Rohwert, Einheit unbekannt): 100  <- unplausibel", out)


# --------------------------------------------------------------------------- K4/K6/K7
class SmallFixes2Tests(unittest.TestCase):
    def test_verify_and_mxf_info_on_missing_path_exit_2_without_traceback(self):
        for argv in (["--verify", str(ROOT / "gibt-es-nicht")], ["--mxf-info", str(ROOT / "gibt-es-nicht.mxf")]):
            err, out = io.StringIO(), io.StringIO()
            with mock.patch("sys.stderr", err), mock.patch("sys.stdout", out):
                self.assertEqual(cli.main(argv), 2, argv)
            self.assertIn("FEHLER", err.getvalue())
            self.assertNotIn("Traceback", err.getvalue() + out.getvalue())

    def test_threads_upper_limit_like_gui(self):
        self.assertEqual(cli.build_parser().parse_args(["q", "z", "--threads", "256"]).threads, 256)
        with mock.patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["q", "z", "--threads", "257"])

    def test_interpreter_empty_or_missing_is_a_clear_message(self):
        for exe in ("", str(ROOT / "gibt-es-nicht.exe"), str(ROOT)):  # leer / fehlt / Ordner
            with self.assertRaises(elevate.InterpreterNotFound):
                elevate.interpreter_for(False, exe)
            msgs, calls = [], []
            rc = elevate.relaunch_as_admin(False, shell_execute=lambda *a: calls.append(a) or 42, out=msgs.append, executable=exe)
            self.assertEqual(rc, 1)
            self.assertFalse(calls, "nichts wird gestartet (und nie der Arbeitsordner)")
            self.assertTrue(msgs and "Interpreter" in msgs[0])

    def test_elevated_interpreter_gets_isolation_switches(self):
        calls = []
        elevate.relaunch_as_admin(False, shell_execute=lambda *a: calls.append(a) or 42, out=lambda s="": None,
                                  executable=sys.executable)
        self.assertTrue(calls[0][2].startswith("-E -s "))

    def test_start_detached_uses_isolated_interpreter_without_window(self):
        seen = {}

        def fake_popen(argv, **kw):
            seen["argv"], seen["kw"] = argv, kw

        self.assertEqual(elevate.start_detached(popen=fake_popen, executable=sys.executable), 0)
        self.assertEqual(seen["argv"][1:3], ["-E", "-s"])
        self.assertTrue(seen["argv"][3].endswith("start_gui.pyw"))

    def test_fmt_span(self):
        self.assertEqual(thermal.fmt_span(90), "90 s")
        self.assertEqual(thermal.fmt_span(119), "119 s")
        self.assertEqual(thermal.fmt_span(1200), "20 min")

    def test_clip_line_shows_pause_part(self):
        r = converter.ClipResult("A.MXF", status=converter.ST_CONVERTED, frames=10, seconds=100.0, pause_seconds=40.0)
        s = converter.RunSummary(results=[r])
        self.assertIn("(10 Frames, 100 s, davon Pause 40 s)", s.text())
        r2 = converter.ClipResult("B.MXF", status=converter.ST_CONVERTED, frames=10, seconds=100.0)
        self.assertIn("(10 Frames, 100 s)", converter.RunSummary(results=[r2]).text())


# --------------------------------------------------------------------------- M4: GUI-Mindesthoehe (echtes Tk)
class GuiMinSizeTests(unittest.TestCase):
    def test_settings_open_keeps_status_lines_and_log_visible(self):
        try:
            from xdroot_converter import gui
            app = gui.App()
        except Exception as e:  # noqa: BLE001  (kein Tk/Display)
            self.skipTest(f"kein Tk: {e}")
        try:
            app.update()
            app._toggle_settings()
            app.update()
            minw, minh = app.minsize()
            self.assertLessEqual(minw, 720)
            app.geometry(f"{minw}x{minh}")
            app.var_status.set("Status")
            app.var_thermal.set("CPU 87 °C · Lüfter 4800/5100 U/min · Lüfter voll")
            for _ in range(3):
                app.update()
                time.sleep(0.1)
            h = app.winfo_height()

            def bottom(w):
                return w.winfo_rooty() - app.winfo_rooty() + w.winfo_height()

            self.assertLessEqual(bottom(app.lbl_status), h, "Statuszeile sichtbar")
            self.assertLessEqual(bottom(app.lbl_thermal), h, "Temperaturzeile sichtbar")
            self.assertGreaterEqual(app.txt.winfo_height(), 78, "mindestens ~80 px Protokoll")
            self.assertLessEqual(bottom(app.frm_settings), h)
        finally:
            app.destroy()


if __name__ == "__main__":
    unittest.main()
