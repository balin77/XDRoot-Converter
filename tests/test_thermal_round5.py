"""Runde 5 (Fixes nach dem Opus-Review): Konstant-Wächter, Drehzahl-/Kennungsfehler im Lauf, Beschriftung der Quelle,
echter Test der Erlaubnisliste des PowerShell-Skripts (mit Attrappe statt Invoke-CimMethod, kein Hardwarezugriff)."""

import base64
import shutil
import sys
import tempfile
import unittest

from xdroot_converter import thermal

try:  # unter "discover -s tests" ist tests/ der Suchpfad, sonst das Paket "tests"
    from test_thermal_sources import SourceHelper
except ImportError:  # pragma: no cover
    from tests.test_thermal_sources import SourceHelper

_ROOT = tempfile.mkdtemp(prefix="xdroot-round5-")


def tearDownModule():
    shutil.rmtree(_ROOT, ignore_errors=True)


def new_store():
    return thermal.FanStateStore(tempfile.mkdtemp(dir=_ROOT))


M = thermal.Messung


class FakeCtl:
    """Attrappe des pause_ctl."""

    def __init__(self):
        self.paused = False
        self.resumed = 0

    def pause(self):
        self.paused = True
        return True

    def resume(self):
        self.paused = False
        self.resumed += 1


class FakeProvider(thermal.ThermalProvider):
    name = "t"

    def __init__(self, fan=True, source="perf"):
        self.fan = fan
        self.temp_source = source
        self.temp_source_label = "x"
        self.seq = []

    def fan_control_info(self):
        return self.fan, "Lüfter über Test"

    def fan_full(self):
        pass

    def fan_restore(self):
        pass

    def measure(self):
        v = self.seq.pop(0)
        if isinstance(v, Exception):
            raise v
        return v

    def close(self):
        pass


def make(source="perf", fan=True, cfg=None, clock=None, on_abort=None):
    notes, statuses = [], []
    prov = FakeProvider(fan, source)
    pc = FakeCtl()
    kw = {"clock": clock} if clock else {}
    ctl = thermal.ThermalController(prov, cfg, pc, lambda lvl, txt: notes.append((lvl, txt)),
                                    lambda t, s: statuses.append((t, s)), on_abort=on_abort, **kw)
    ctl.setup()
    return ctl, prov, pc, notes, statuses


def warns(notes):
    return [t for lv, t in notes if lv == "warn" and "denselben Wert" in t]


N = thermal.CONSTANT_GUARD_COUNT


class ConstantGuardTests(unittest.TestCase):
    def test_never_changed_below_threshold_fails_without_pause_or_fan(self):
        for key in thermal.CONSTANT_GUARD_SOURCES:
            ctl, _, pc, notes, _ = make(key)
            for _ in range(N):
                ctl.tick(M(50.0, (), None, key))
            self.assertEqual(ctl.failed, "Temperaturquelle liefert einen konstanten Wert", key)
            self.assertEqual(warns(notes), [], key)

    def test_changed_once_never_fails_only_one_warning(self):
        ctl, _, pc, notes, _ = make("acpi")
        ctl.tick(M(50.0, (), None, "acpi"))
        for _ in range(3 * N):
            ctl.tick(M(51.0, (), None, "acpi"))
        self.assertEqual(ctl.failed, "")
        self.assertEqual(len(warns(notes)), 1)
        self.assertIn("seit 3 min denselben Wert", warns(notes)[0])
        self.assertIn("träge Quelle?", warns(notes)[0])
        self.assertEqual(ctl.last.cpu_temp_c, 51.0)  # Überwachung läuft weiter

    def test_never_changed_above_fan_threshold_with_full_fan_does_not_fail(self):
        ctl, _, pc, notes, _ = make("perf", fan=True)
        for _ in range(3 * N):
            ctl.tick(M(90.0, (), None, "perf"))
        self.assertTrue(ctl.fan_full)
        self.assertEqual(ctl.failed, "")
        self.assertTrue(ctl.fan_full)  # Lüfter wurde NICHT zurückgenommen
        self.assertEqual(len(warns(notes)), 1)

    def test_never_changed_above_threshold_without_fan_control_does_not_fail(self):
        ctl, _, pc, notes, _ = make("perf", fan=False)
        for _ in range(2 * N):
            ctl.tick(M(90.0, (), None, "perf"))
        self.assertFalse(ctl.fan_full)
        self.assertEqual(ctl.failed, "")
        self.assertEqual(len(warns(notes)), 1)

    def test_never_changed_with_active_pause_does_not_fail(self):
        cfg = thermal.ThermalConfig(max_pause_s=10 ** 9)
        ctl, _, pc, notes, _ = make("acpi", cfg=cfg)
        for _ in range(2 * N):
            ctl.tick(M(99.0, (), None, "acpi"))
        self.assertTrue(ctl.paused)
        self.assertTrue(pc.paused)
        self.assertTrue(ctl.fan_full)
        self.assertEqual(ctl.failed, "")
        self.assertEqual(ctl.stats.pauses, 1)
        self.assertEqual(len(warns(notes)), 1)

    def test_max_pause_duration_still_applies_with_guard(self):
        for key, name in (("perf", "Thermalzone (grob, nicht CPU)"), ("feature", "CPU (Quelle unbestätigt)")):
            now = [0.0]
            aborts = []
            cfg = thermal.ThermalConfig(max_pause_s=100.0)
            ctl, _, pc, notes, statuses = make(key, cfg=cfg, clock=lambda: now[0], on_abort=aborts.append)
            for _ in range(N):
                now[0] += 5.0
                ctl.tick(M(99.0, (), None, key))
            self.assertEqual(ctl.failed, "", key)
            self.assertIn(f"Abgebrochen: {name} kühlt nicht ab", ctl.aborted, key)
            self.assertEqual(len(aborts), 1, key)
            self.assertFalse(pc.paused, key)  # fortgesetzt, dann sauber abgebrochen
            self.assertTrue(statuses[-1][0].startswith(f"{name} 99 °C - Abgebrochen"), statuses[-1])

    def test_abort_text_for_cpu_source(self):
        now = [0.0]
        cfg = thermal.ThermalConfig(max_pause_s=50.0)
        ctl, *_ = make("gamezone", cfg=cfg, clock=lambda: now[0])
        for _ in range(20):
            now[0] += 5.0
            ctl.tick(M(99.0, (), None, "gamezone"))
        self.assertIn("Abgebrochen: CPU kühlt nicht ab (99 °C", ctl.aborted)

    def test_guard_fails_later_when_never_changed_and_protection_idle(self):
        # zuerst über der Schwelle (kein Ausfall, nur Warnung) - sinkt der Wert nie, bleibt es dabei
        ctl, _, pc, notes, _ = make("perf", fan=False)
        for _ in range(N + 5):
            ctl.tick(M(90.0, (), None, "perf"))
        self.assertEqual(ctl.failed, "")


class ControllerErrorTests(unittest.TestCase):
    def test_single_remote_error_counts_like_implausible(self):
        ctl, prov, *_ = make("gamezone")
        R = thermal.HelperRemoteError("Failed", "kurz weg")
        prov.seq = [M(60.0), R, M(60.0), R, R, M(60.0), R, R, R]
        for _ in range(2):  # Fehler, dann Wert: kein Ausfall
            ctl.tick()
        ctl.tick()
        self.assertEqual(ctl.failed, "")
        ctl.tick()
        ctl.tick()
        self.assertEqual(ctl.failed, "")  # 2 Fehler in Folge
        ctl.tick()  # Wert setzt den Zähler zurück
        ctl.tick()
        ctl.tick()
        self.assertEqual(ctl.failed, "")
        ctl.tick()
        self.assertEqual(ctl.failed, "Die Temperaturmessung ist ausgefallen")

    def test_helper_timeout_from_provider_still_fails_at_once(self):
        ctl, prov, *_ = make("gamezone")
        prov.seq = [thermal.HelperTimeout("x")]
        ctl.tick()
        self.assertEqual(ctl.failed, "Die Temperaturmessung ist ausgefallen")


class CountingFactory:
    """Fabrik, die bei jedem Aufruf ein NEUES Objekt liefert; ``setup(helper)`` konfiguriert es."""

    def __init__(self, make_helper):
        self.make_helper = make_helper
        self.created = []

    def __call__(self):
        h = self.make_helper(len(self.created))
        self.created.append(h)
        return h


class DyingHelper(SourceHelper):
    """Attrappe, die bei bestimmten Befehlen wirklich "stirbt": HelperTimeout, danach ist sie nicht mehr aktiv."""

    def __init__(self, die_on=(), **kw):
        super().__init__(**kw)
        self.die_on = list(die_on)
        self.alive = True
        self.timeouts = []

    def call(self, line, timeout=None, lock_timeout=None):
        self.timeouts.append((line, timeout))
        if not self.alive:
            raise thermal.HelperError("Hilfsprozess nicht aktiv")
        if any(tok in line for tok in self.die_on):
            self.alive = False
            raise thermal.HelperTimeout("keine Antwort des Hilfsprozesses")
        return super().call(line, timeout, lock_timeout)

    def close(self, grace=2.0):
        self.alive = False
        self.closed = True


def legion(factory):
    return thermal.LegionProvider(helper_factory=factory, platform="win32", state_store=new_store())


class RpmFailureInRunTests(unittest.TestCase):
    def test_rpm_hang_keeps_temperature_restarts_and_continues_without_rpm(self):
        fac = CountingFactory(lambda i: DyingHelper(gz=64, fan1=2000, fan2=2100))
        p = legion(fac)
        p.open()
        self.assertEqual(len(fac.created), 1)
        first = fac.created[0]
        first.die_on = ["GetFan1Speed"]
        m = p.measure()
        self.assertEqual(m.cpu_temp_c, 64.0)  # die gelesene Temperatur wird nicht verworfen
        self.assertEqual(m.fan_rpm, ())
        self.assertEqual(len(fac.created), 2, "Hilfsprozess wurde nicht neu gestartet")
        self.assertTrue(first.closed)
        self.assertFalse(first.alive)
        second = fac.created[1]
        self.assertTrue(second.alive)
        # danach läuft es mit dem NEUEN Hilfsprozess normal weiter (Drehzahl wieder lesbar, erst ein Fehler)
        m = p.measure()
        self.assertEqual((m.cpu_temp_c, m.fan_rpm), (64.0, (2000, 2100)))
        self.assertTrue(any("GetCPUTemp" in c for c in second.calls))
        self.assertEqual(p.take_messages(), [])

    def test_second_failure_switches_rpm_source_off_for_the_rest_of_the_run(self):
        fac = CountingFactory(lambda i: DyingHelper(gz=64, fan1=2000, fan2=2100))
        p = legion(fac)
        p.open()
        fac.created[0].die_on = ["GetFan1Speed"]
        self.assertEqual(p.measure().fan_rpm, ())
        fac.created[1].die_on = ["GetFan2Speed"]
        m = p.measure()
        self.assertEqual((m.cpu_temp_c, m.fan_rpm), (64.0, ()))
        self.assertEqual(len(fac.created), 3)
        infos = [t for lv, t in p.take_messages() if lv == "info"]
        self.assertEqual(len(infos), 1)
        self.assertIn("Drehzahlquelle", infos[0])
        third = fac.created[2]
        for _ in range(3):
            m = p.measure()
            self.assertEqual((m.cpu_temp_c, m.fan_rpm), (64.0, ()))
        self.assertFalse(any("GetFan" in c or "IDs=%d" % thermal.FEATURE_ID_FAN1_SPEED in c for c in third.calls))
        self.assertEqual(p.take_messages(), [])  # die Info-Zeile kommt nur einmal
        self.assertEqual(len(fac.created), 3)

    def test_fallback_source_failure_also_keeps_temperature(self):
        feat = {thermal.FEATURE_ID_FAN1_SPEED: 3100, thermal.FEATURE_ID_FAN2_SPEED: 3300}
        fac = CountingFactory(lambda i: DyingHelper(gz=64, fan1=0, fan2=0, feat_fan=feat))
        p = legion(fac)
        p.open()
        fac.created[0].die_on = ["IDs=%d" % thermal.FEATURE_ID_FAN2_SPEED]
        m = p.measure()
        self.assertEqual((m.cpu_temp_c, m.fan_rpm), (64.0, ()))
        self.assertEqual(len(fac.created), 2)

    def test_restart_limit_is_respected_and_temperature_still_returned(self):
        fac = CountingFactory(lambda i: DyingHelper(gz=64, fan1=2000, fan2=2100))
        p = legion(fac)
        p.open()
        p._restarts = thermal.MAX_HELPER_RESTARTS
        fac.created[0].die_on = ["GetFan1Speed"]
        m = p.measure()
        self.assertEqual((m.cpu_temp_c, m.fan_rpm), (64.0, ()))
        self.assertEqual(len(fac.created), 1)  # kein Neustart über das Limit hinaus

    def test_controller_survives_rpm_hang(self):
        fac = CountingFactory(lambda i: DyingHelper(gz=64, fan1=2000, fan2=2100))
        av = thermal.check_availability(lambda: legion(fac))
        self.assertTrue(av.ok)
        ctl = thermal.ThermalController(av.provider, None, FakeCtl(), None, None)
        ctl.setup()
        fac.created[0].die_on = ["GetFan1Speed"]
        ctl.tick()
        self.assertEqual(ctl.failed, "")
        self.assertEqual(ctl.last.cpu_temp_c, 64.0)
        ctl.finish()


class FanControlProbeTests(unittest.TestCase):
    FULL = "IDs=%d" % thermal.FEATURE_ID_FAN_FULL_SPEED

    def test_one_hang_on_fan_identifier_is_retried_with_fresh_helper(self):
        fac = CountingFactory(lambda i: DyingHelper(die_on=[self.FULL] if i == 0 else [], gz=64))
        p = legion(fac)
        p.open()
        self.assertEqual(len(fac.created), 2)
        self.assertEqual(p.temp_source, "gamezone")
        self.assertTrue(p.fan_control_info()[0])

    def test_persistent_hang_keeps_provider_available_without_fan_control(self):
        fac = CountingFactory(lambda i: DyingHelper(die_on=[self.FULL], gz=64))
        av = thermal.check_availability(lambda: legion(fac))
        self.assertTrue(av.ok, av.reason)
        can, note = av.provider.fan_control_info()
        self.assertFalse(can)
        self.assertTrue(note.startswith("Lüftersteuerung nicht verfügbar"), note)
        self.assertEqual(av.provider.temp_source, "gamezone")
        self.assertEqual(av.first.cpu_temp_c, 64.0)  # Anzeige/Pause funktionieren
        notes = []
        ctl = thermal.ThermalController(av.provider, None, FakeCtl(), lambda lvl, txt: notes.append((lvl, txt)), None)
        ctl.setup()
        self.assertFalse(ctl.fan_ok)
        self.assertIn(note, ctl.fan_reason)
        self.assertTrue(any("nur gemessen und bei Bedarf pausiert" in t for lv, t in notes))
        self.assertEqual(
            thermal.format_status(M(70.0), False, False, None, "pending", ctl.fan_reason),
            f"CPU 70 °C · {note}")
        ctl.finish()


class PerfOpenTimeoutTests(unittest.TestCase):
    def test_open_uses_generous_timeout_run_uses_default(self):
        h = DyingHelper(gz=0, feat=0, acpi="CurrentTemperature=", perf="Temperature=320 HighPrecisionTemperature=")
        p = thermal.LegionProvider(helper_factory=lambda: h, platform="win32", state_store=new_store())
        p.open()
        self.assertEqual(p.temp_source, "perf")
        opening = [t for line, t in h.timeouts if line == "READ PERF"]
        self.assertEqual(opening, [thermal.PERF_OPEN_TIMEOUT_S])
        self.assertEqual(thermal.PERF_OPEN_TIMEOUT_S, 20.0)
        p.measure()
        running = [t for line, t in h.timeouts if line == "READ PERF"]
        self.assertEqual(running[1:], [None])  # im Lauf unverändert (Standardzeitlimit des Hilfsprozesses)


class SourceLabelTests(unittest.TestCase):
    def test_names(self):
        self.assertEqual(thermal.source_name(""), "CPU")
        self.assertEqual(thermal.source_name("gamezone"), "CPU")
        self.assertEqual(thermal.source_name("feature"), "CPU (Quelle unbestätigt)")
        self.assertEqual(thermal.source_name("acpi"), "Thermalzone (grob, nicht CPU)")
        self.assertEqual(thermal.source_name("perf"), "Thermalzone (grob, nicht CPU)")

    def test_pause_status_and_log_lines(self):
        cfg = thermal.ThermalConfig()
        self.assertEqual(thermal.format_status(M(98.0, (), None, "gamezone"), True, True, cfg),
                         "Pause wegen Temperatur (98 °C), weiter unter 85 °C")
        self.assertEqual(thermal.format_status(M(98.0, (), None, "perf"), True, True, cfg),
                         "Pause wegen Temperatur – Thermalzone (grob, nicht CPU) 98 °C, weiter unter 85 °C")
        self.assertEqual(thermal.format_status(M(98.0, (), None, "feature"), True, True, cfg),
                         "Pause wegen Temperatur – CPU (Quelle unbestätigt) 98 °C, weiter unter 85 °C")
        now = [0.0]
        ctl, _, pc, notes, _ = make("perf", cfg=thermal.ThermalConfig(pause_log_s=10.0), clock=lambda: now[0])
        for _ in range(6):
            now[0] += 5.0
            ctl.tick(M(99.0, (), None, "perf"))
        self.assertTrue(any(t.startswith("Pause dauert seit") and "Thermalzone (grob, nicht CPU) 99 °C" in t
                            for lv, t in notes), notes)

    def test_thermal_test_labels_follow_the_source(self):
        h = SourceHelper(gz=0, feat=0, acpi="CurrentTemperature=3231", fan1=0, fan2=0)
        lines = []
        thermal.run_thermal_test(
            lines.append, provider_factory=lambda: thermal.LegionProvider(
                helper_factory=lambda: h, platform="win32", state_store=new_store(), diagnose=True),
            sleep=lambda s: None, full_seconds=0.0, samples=2, input_fn=lambda *_: "")
        msgs = [x for x in lines if "Messung " in x and "°C" in x]
        self.assertEqual(len(msgs), 2, lines)
        for x in msgs:
            self.assertIn("Thermalzone (grob, nicht CPU) 49.95", x)
            self.assertNotIn(": CPU ", x)
        after = [x for x in lines if "Drehzahl NACHHER" in x]
        for x in after:
            self.assertIn("Thermalzone (grob, nicht CPU)", x)
        h2 = SourceHelper(gz=60, fan1=0, fan2=0)
        lines = []
        thermal.run_thermal_test(
            lines.append, provider_factory=lambda: thermal.LegionProvider(
                helper_factory=lambda: h2, platform="win32", state_store=new_store(), diagnose=True),
            sleep=lambda s: None, full_seconds=0.0, samples=1, input_fn=lambda *_: "")
        self.assertTrue(any("Messung 1: CPU 60 °C" in x for x in lines), lines)


@unittest.skipUnless(sys.platform == "win32", "Erlaubnisliste wird mit der Windows-PowerShell geprüft")
class RealScriptAllowlistTests(unittest.TestCase):
    """Das ECHTE PowerShell-Skript läuft im echten Hilfsprozess, aber ``Invoke-CimMethod`` ist durch eine Attrappe ersetzt:
    Es findet KEIN CIM-Aufruf an Lenovo-Klassen statt (kein Hardwarezugriff, keine Adminrechte, kein Display nötig)."""

    ORIG = "$r = Invoke-CimMethod -InputObject (Get-Inst $cls) -MethodName $m -Arguments $prm"
    STUB = ("$r = [pscustomobject]@{ ReturnValue = ('STUB:' + $cls + '.' + $m + ':' + "
            "(($prm.GetEnumerator() | Sort-Object Name | ForEach-Object { $_.Name + '=' + $_.Value }) -join ',')) }")

    @classmethod
    def setUpClass(cls):
        exe = thermal.system_powershell_path()
        if exe is None:
            raise unittest.SkipTest("Windows-PowerShell nicht gefunden")
        script = thermal._PS_SCRIPT
        # genau die erwartete Zahl Stellen: eine Ersetzung, und danach kein Invoke-CimMethod/Get-Inst-Aufruf mehr
        assert script.count("Invoke-CimMethod") == 1, script.count("Invoke-CimMethod")
        assert script.count(cls.ORIG) == 1, script.count(cls.ORIG)
        assert script.count("Get-Inst $cls") == 1  # nur diese Zeile ruft Get-Inst mit der Klasse auf
        stubbed = script.replace(cls.ORIG, cls.STUB)
        assert stubbed != script and stubbed.count("STUB:") == 1
        assert "Invoke-CimMethod" not in stubbed
        assert "Get-Inst $cls" not in stubbed
        enc = base64.b64encode(stubbed.encode("utf-16-le")).decode("ascii")
        argv = [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-EncodedCommand", enc]
        cls.helper = thermal.PowerShellHelper(argv=argv, startup_timeout=30.0, timeout=15.0)
        try:
            cls.helper.start()
        except BaseException:
            cls.helper.close(grace=0.0)
            raise

    @classmethod
    def tearDownClass(cls):
        h = getattr(cls, "helper", None)
        if h is not None:
            h.close(grace=2.0)
            proc = h._proc
            if proc is not None and proc.poll() is None:
                proc.kill()

    def allowed(self, cmd, expect):
        out = self.helper.call(cmd)
        self.assertIn("STUB:" + expect, out, cmd)

    def rejected(self, cmd):
        with self.assertRaises(thermal.HelperRemoteError, msg=cmd) as cm:
            out = self.helper.call(cmd)
            self.fail(f"nicht abgelehnt: {cmd!r} -> {out!r}")
        self.assertIn("BadArgs", str(cm.exception), cmd)

    def test_allowed_calls_pass_through_to_the_stub(self):
        gz = "LENOVO_GAMEZONE_DATA"
        for m in ("GetCPUTemp", "GetFan1Speed", "GetFan2Speed", "GetFanMaxSpeed", "GetSmartFanMode"):
            self.allowed(f"CALL {gz} {m}", f"{gz}.{m}:")
        for fid in (0x04020000, 0x05040000, 0x04030001, 0x04030002):
            self.allowed(f"CALL LENOVO_OTHER_METHOD GetFeatureValue IDs={fid}",
                         f"LENOVO_OTHER_METHOD.GetFeatureValue:IDs={fid}")
        fan = thermal.FEATURE_ID_FAN_FULL_SPEED
        for v in (0, 1):
            self.allowed(f"CALL LENOVO_OTHER_METHOD SetFeatureValue IDs={fan} value={v}",
                         f"LENOVO_OTHER_METHOD.SetFeatureValue:IDs={fan},value={v}")
        # Reihenfolge der Parameter ist egal
        self.allowed(f"CALL LENOVO_OTHER_METHOD SetFeatureValue value=0 IDs={fan}",
                     f"LENOVO_OTHER_METHOD.SetFeatureValue:IDs={fan},value=0")

    def test_everything_else_is_rejected_before_any_call(self):
        fan = thermal.FEATURE_ID_FAN_FULL_SPEED
        bad = [
            # SmartFanMode schalten
            "CALL LENOVO_GAMEZONE_DATA SetSmartFanMode",
            "CALL LENOVO_GAMEZONE_DATA SetSmartFanMode Data=3",
            "CALL LENOVO_GAMEZONE_DATA SetSmartFanMode IDs=3 value=3",
            # SetFeatureValue mit fremder Kennung / falschem Wert
            "CALL LENOVO_OTHER_METHOD SetFeatureValue IDs=%d value=1" % thermal.FEATURE_ID_CPU_TEMP,
            "CALL LENOVO_OTHER_METHOD SetFeatureValue IDs=1 value=1",
            f"CALL LENOVO_OTHER_METHOD SetFeatureValue IDs={fan} value=2",
            f"CALL LENOVO_OTHER_METHOD SetFeatureValue IDs={fan} value=255",
            f"CALL LENOVO_OTHER_METHOD SetFeatureValue IDs={fan}",
            "CALL LENOVO_OTHER_METHOD SetFeatureValue value=1",
            # GetFeatureValue mit fremder Kennung
            "CALL LENOVO_OTHER_METHOD GetFeatureValue IDs=1",
            "CALL LENOVO_OTHER_METHOD GetFeatureValue IDs=67239937",
            "CALL LENOVO_OTHER_METHOD GetFeatureValue",
            # zusätzliche/doppelte Argumente
            f"CALL LENOVO_OTHER_METHOD GetFeatureValue IDs={fan} value=1",
            f"CALL LENOVO_OTHER_METHOD GetFeatureValue IDs={fan} IDs={fan}",
            f"CALL LENOVO_OTHER_METHOD SetFeatureValue IDs={fan} value=0 extra=1",
            f"CALL LENOVO_OTHER_METHOD SetFeatureValue IDs={fan} value=0 value=1",
            "CALL LENOVO_GAMEZONE_DATA GetCPUTemp IDs=1",
            "CALL LENOVO_GAMEZONE_DATA GetCPUTemp Data=1",
            # falsche Schreibweise (Klasse, Methode, Parameternamen)
            "CALL lenovo_gamezone_data GetCPUTemp",
            "CALL LENOVO_GAMEZONE_DATA getcputemp",
            "CALL LENOVO_GAMEZONE_DATA GETCPUTEMP",
            "CALL LENOVO_OTHER_METHOD setfeaturevalue IDs=%d value=1" % fan,
            "CALL LENOVO_OTHER_METHOD SETFEATUREVALUE IDs=%d value=1" % fan,
            f"CALL LENOVO_OTHER_METHOD SetFeatureValue ids={fan} value=1",
            f"CALL LENOVO_OTHER_METHOD SetFeatureValue IDS={fan} value=1",
            f"CALL LENOVO_OTHER_METHOD SetFeatureValue IDs={fan} VALUE=1",
            f"CALL LENOVO_OTHER_METHOD SetFeatureValue IDs={fan} Value=1",
            f"CALL LENOVO_OTHER_METHOD GetFeatureValue iDs={fan}",
            # andere Klassen / Methoden
            "CALL LENOVO_GAMEZONE_DATA GetSomethingElse",
            "CALL LENOVO_OTHER_METHOD SetSomethingElse IDs=1 value=1",
            "CALL LENOVO_FOO_BAR GetCPUTemp",
            "CALL Win32_Process Create",
        ]
        for cmd in bad:
            self.rejected(cmd)

    def test_stub_never_saw_a_rejected_call_and_helper_still_works(self):
        self.assertEqual(self.helper.call("PING"), "pong")
        self.rejected("CALL LENOVO_GAMEZONE_DATA SetSmartFanMode")
        self.assertEqual(self.helper.call("PING"), "pong")
        self.assertTrue(self.helper.alive)


if __name__ == "__main__":
    unittest.main()
