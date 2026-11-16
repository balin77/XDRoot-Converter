"""Tests der Temperaturquellen-Kette des Legion-Anbieters (Nachtrag nach dem Gerätetest 16.11.2026: GetCPUTemp = 0).
Alles mit Attrappen, nur lesende Aufrufe, keine echte Hardware."""

import shutil
import tempfile
import unittest
from pathlib import Path

from xdroot_converter import thermal

_ROOT = tempfile.mkdtemp(prefix="xdroot-sources-")


def tearDownModule():
    shutil.rmtree(_ROOT, ignore_errors=True)


def new_store():
    return thermal.FanStateStore(tempfile.mkdtemp(dir=_ROOT))


class SourceHelper:
    """Attrappe des PowerShell-Hilfsprozesses.  ``gz``/``feat``: Werte (int) oder Ausnahme; ``acpi``/``perf``: Antworttext
    oder Ausnahme.  ``feat`` wird je Kennung nachgeschlagen (dict) oder gilt für die CPU-Kennung."""

    def __init__(self, gz=0, feat=None, acpi=None, perf=None, fan1=2000, fan2=2100, feat_fan=None, fanfull=0, smart=2):
        self.gz, self.feat, self.acpi, self.perf = gz, feat, acpi, perf
        self.fan1, self.fan2, self.feat_fan = fan1, fan2, feat_fan or {}
        self.fanfull, self.smart = fanfull, smart
        self.calls = []
        self.closed = False

    def start(self):
        pass

    def close(self, grace=2.0):
        self.closed = True

    @staticmethod
    def _val(v):
        if isinstance(v, Exception):
            raise v
        return v

    def call(self, line, timeout=None, lock_timeout=None):
        self.calls.append(line)
        t = line.split()
        if t[0] == "READ":
            r = self.acpi if t[1] == "ACPI" else self.perf
            if r is None:
                raise thermal.HelperRemoteError("NotSupported", "Nicht unterstützt")
            return self._val(r)
        assert t[0] == "CALL", line
        method = t[2]
        params = dict(x.split("=") for x in t[3:])
        if method == "GetCPUTemp":
            return f"Data={self._val(self.gz)}"
        if method == "GetFan1Speed":
            return f"Data={self.fan1}"
        if method == "GetFan2Speed":
            return f"Data={self.fan2}"
        if method == "GetFanMaxSpeed":
            return "Data=5500"
        if method == "GetSmartFanMode":
            return f"Data={self.smart}"
        if method == "GetFeatureValue":
            fid = int(params["IDs"])
            if fid == thermal.FEATURE_ID_CPU_TEMP:
                if self.feat is None:
                    raise thermal.HelperRemoteError("NotSupported", "Feature unbekannt")
                return f"IDs={fid} value={self._val(self.feat)}"
            if fid == thermal.FEATURE_ID_FAN_FULL_SPEED:
                return f"IDs={fid} value={self.fanfull}"
            if fid in self.feat_fan:
                return f"IDs={fid} value={self._val(self.feat_fan[fid])}"
            raise thermal.HelperRemoteError("NotSupported", "Feature unbekannt")
        if method == "SetFeatureValue":  # nur "Lüfter voll" mit 0/1 ist erlaubt
            assert int(params["IDs"]) == thermal.FEATURE_ID_FAN_FULL_SPEED and params["value"] in ("0", "1"), line
            self.fanfull = int(params["value"])
            return "ReturnValue=0"
        raise AssertionError(f"unerwarteter Aufruf: {line}")  # insbesondere kein SetSmartFanMode

    def writes(self):
        return [c for c in self.calls if " Set" in c]


def prov(helper, **kw):
    return thermal.LegionProvider(helper_factory=lambda: helper, platform="win32", state_store=new_store(), **kw)


class ChainTests(unittest.TestCase):
    def test_a_is_used_when_plausible(self):
        h = SourceHelper(gz=64, feat=50)
        p = prov(h)
        p.open()
        self.assertEqual(p.temp_source, "gamezone")
        self.assertEqual(p.measure().cpu_temp_c, 64.0)
        self.assertFalse(any("READ" in c or "IDs=%d" % thermal.FEATURE_ID_CPU_TEMP in c for c in h.calls))

    def test_b_when_a_returns_zero(self):
        h = SourceHelper(gz=0, feat=58)
        p = prov(h)
        p.open()
        self.assertEqual(p.temp_source, "feature")
        self.assertIn("unbestätigt", p.temp_source_label)
        self.assertEqual(p.measure().cpu_temp_c, 58.0)
        self.assertEqual(h.writes(), [])

    def test_c_when_a_and_b_fail(self):
        h = SourceHelper(gz=0, feat=None, acpi="CurrentTemperature=3000,3231")  # 26.85 / 50.0 Grad
        p = prov(h)
        p.open()
        self.assertEqual(p.temp_source, "acpi")
        self.assertIn("grob", p.temp_source_label)
        self.assertAlmostEqual(p.measure().cpu_temp_c, 323.1 - 273.15, places=2)

    def test_d_perf_counter_fields(self):
        h = SourceHelper(gz=0, feat=0, acpi="CurrentTemperature=", perf="Temperature=310,320 HighPrecisionTemperature=")
        p = prov(h)
        p.open()
        self.assertEqual(p.temp_source, "perf")
        self.assertAlmostEqual(p.measure().cpu_temp_c, 320 - 273.15, places=2)
        h = SourceHelper(gz=0, feat=0, perf="Temperature= HighPrecisionTemperature=3101,3002")
        p = prov(h)
        p.open()
        self.assertAlmostEqual(p.measure().cpu_temp_c, 310.1 - 273.15, places=2)

    def test_nothing_plausible_is_unavailable_and_writes_nothing(self):
        h = SourceHelper(gz=0, feat=0, acpi="CurrentTemperature=", perf="Temperature= HighPrecisionTemperature=")
        p = prov(h, diagnose=True)
        with self.assertRaises(thermal.ProviderUnavailable) as cm:
            p.open()
        self.assertEqual(cm.exception.code, "implausible")
        self.assertEqual(h.writes(), [])
        self.assertTrue(h.closed)
        self.assertTrue(p.candidate_report)  # Liste bleibt trotzdem erhalten
        av = thermal.check_availability(lambda: prov(SourceHelper(gz=0)))
        self.assertFalse(av.ok)

    def test_exception_in_one_source_does_not_stop_the_others(self):
        h = SourceHelper(gz=RuntimeError("kaputt"), feat=thermal.ProviderError("x"), acpi=ValueError("y"),
                         perf="Temperature=305 HighPrecisionTemperature=")
        p = prov(h)
        p.open()
        self.assertEqual(p.temp_source, "perf")

    def test_source_is_fixed_for_the_run_no_silent_switch(self):
        h = SourceHelper(gz=0, feat=55)
        p = prov(h)
        p.open()
        h.feat = thermal.HelperRemoteError("Failed", "weg")
        h.gz = 70  # wäre jetzt plausibel: darf NICHT still umgeschaltet werden
        with self.assertRaises(thermal.HelperRemoteError):
            p.measure()
        self.assertEqual(p.temp_source, "feature")

    def test_conversion_tenth_kelvin(self):
        self.assertAlmostEqual(thermal.kelvin10_to_c(3000), 26.85)
        self.assertAlmostEqual(thermal.kelvin10_to_c(3731.5), 100.0)
        self.assertAlmostEqual(thermal.kelvin_to_c(300), 26.85)
        self.assertEqual(thermal.best_plausible([0.0, -5.0, 40.0, 61.5, 300.0]), 61.5)
        self.assertIsNone(thermal.best_plausible([0.0, 300.0]))

    def test_highest_plausible_zone_wins(self):
        h = SourceHelper(gz=0, feat=0, acpi="CurrentTemperature=2732,3000,3500,9999")  # 0, 26.85, 76.85, 726 Grad
        p = prov(h)
        p.open()
        self.assertAlmostEqual(p.measure().cpu_temp_c, 76.85, places=2)

    def test_controller_names_source_once(self):
        h = SourceHelper(gz=0, feat=60)
        av = thermal.check_availability(lambda: prov(h))
        self.assertTrue(av.ok)
        notes = []
        ctl = thermal.ThermalController(av.provider, None, None, lambda lvl, txt: notes.append(txt), None)
        ctl.setup()
        self.assertEqual(sum(1 for n in notes if n.startswith("Temperaturquelle:")), 1)
        ctl.finish()


class FanSourceTests(unittest.TestCase):
    def test_fallback_to_feature_when_getfan_is_zero(self):
        h = SourceHelper(gz=60, fan1=0, fan2=0, feat_fan={thermal.FEATURE_ID_FAN1_SPEED: 3100,
                                                          thermal.FEATURE_ID_FAN2_SPEED: 3300})
        p = prov(h)
        p.open()
        self.assertEqual(p.measure().fan_rpm, (3100, 3300))
        self.assertEqual(h.writes(), [])

    def test_no_fallback_when_getfan_plausible(self):
        h = SourceHelper(gz=60, fan1=2500, fan2=2600, feat_fan={thermal.FEATURE_ID_FAN1_SPEED: 3100})
        p = prov(h)
        p.open()
        self.assertEqual(p.measure().fan_rpm, (2500, 2600))

    def test_no_rpm_at_all_stays_optional(self):
        h = SourceHelper(gz=60, fan1=0, fan2=0)
        p = prov(h)
        p.open()
        m = p.measure()
        self.assertEqual(thermal.ThermalController._top_rpm(m), None)


class DiagnoseTests(unittest.TestCase):
    def test_lists_all_candidates(self):
        h = SourceHelper(gz=0, feat=57, acpi="CurrentTemperature=3100", perf="Temperature=305 HighPrecisionTemperature=3052",
                         fan1=0, fan2=0, feat_fan={thermal.FEATURE_ID_FAN1_SPEED: 3000}, fanfull=1, smart=3)
        lines = []
        rc = thermal.run_thermal_test(lines.append, provider_factory=lambda: prov(h, diagnose=True), sleep=lambda s: None,
                                      full_seconds=0.0, samples=1, input_fn=lambda *_: "")
        text = "\n".join(lines)
        for part in ("a) Lenovo GetCPUTemp", "b) Lenovo GetFeatureValue 0x05040000 (unbestätigt)", "c) ACPI-Thermalzone (grob",
                     "d) Windows-Leistungszähler", "Rohwert 0 ->", "plausibel: nein", "plausibel: ja", "GetFan1Speed",
                     "GetFan2Speed", "0x04030001 (unbestätigt): Rohwert 3000, plausibel: ja",
                     "0x04030002 (unbestätigt): FEHLER",
                     "SmartFanMode (GetSmartFanMode, nur gelesen, wird NIE geschaltet): 3",
                     "GetFeatureValue 0x04020000, nur gelesen): Rohwert 1", "Rohwert 3100 (Zehntel-Kelvin)",
                     "Temperaturquelle: Lenovo GetFeatureValue"):
            self.assertIn(part, text)
        self.assertTrue(all("SetFeatureValue IDs=67239936 value=" in w for w in h.writes()))  # nur Lüfter voll
        self.assertNotIn("SetSmartFanMode", " ".join(h.calls))
        self.assertEqual(rc, 0, text)
        self.assertTrue([x for x in lines if x.startswith("ERGEBNIS:")], text)

    def test_unavailable_still_lists_candidates_and_switches_nothing(self):
        h = SourceHelper(gz=0, feat=0, fan1=0, fan2=0)
        lines = []
        rc = thermal.run_thermal_test(lines.append, provider_factory=lambda: prov(h, diagnose=True), sleep=lambda s: None,
                                      input_fn=lambda *_: "")
        text = "\n".join(lines)
        self.assertEqual(rc, 1)
        self.assertIn("ERGEBNIS: nicht verfügbar - es wurde nichts geschaltet", text)
        for part in ("a) Lenovo GetCPUTemp", "b) ", "c) ", "d) ", "FEHLER", "SmartFanMode"):
            self.assertIn(part, text)
        self.assertEqual(h.writes(), [])

    def test_exception_in_a_source_is_reported_not_fatal(self):
        h = SourceHelper(gz=RuntimeError("boom"), feat=61)
        p = prov(h, diagnose=True)
        p.open()
        rep = "\n".join(p.candidate_report)
        self.assertIn("FEHLER RuntimeError", rep)
        self.assertIn("d) ", rep)  # alle weiteren wurden trotzdem ausgewertet
        self.assertEqual(p.temp_source, "feature")

    def test_logged_file_has_candidates(self):
        h = SourceHelper(gz=0, feat=0)
        path = Path(_ROOT) / "tt.txt"
        thermal.run_thermal_test_logged(lambda s="": None, log_path=path,
                                        provider_factory=lambda: prov(h, diagnose=True), sleep=lambda s: None,
                                        input_fn=lambda *_: "")
        self.assertIn("a) Lenovo GetCPUTemp", path.read_text(encoding="utf-8"))


class MinimumAndMixedListTests(unittest.TestCase):
    def test_flags_are_no_temperatures(self):
        for feat in (1, 2):
            h = SourceHelper(gz=0, feat=feat)
            p = prov(h, diagnose=True)
            with self.assertRaises(thermal.ProviderUnavailable) as cm:
                p.open()
            self.assertEqual(cm.exception.code, "implausible", feat)
            self.assertEqual(h.writes(), [])
            self.assertIn("Schalter/Flag", "\n".join(p.candidate_report))

    def test_below_minimum_is_not_a_source_but_run_limit_unchanged(self):
        with self.assertRaises(thermal.ProviderUnavailable):
            prov(SourceHelper(gz=15)).open()
        self.assertTrue(thermal.plausible(15))  # im Lauf gilt weiter die alte Grenze
        p = prov(SourceHelper(gz=20))
        p.open()
        self.assertEqual(p.temp_source, "gamezone")

    def test_mixed_list_with_blank_entry(self):
        h = SourceHelper(gz=0, feat=0, acpi="CurrentTemperature=,3010")
        p = prov(h)
        p.open()
        self.assertEqual(p.temp_source, "acpi")
        self.assertAlmostEqual(p.measure().cpu_temp_c, 301.0 - 273.15, places=2)


class CoarseSourceTests(unittest.TestCase):
    def test_status_texts(self):
        M = thermal.Messung
        self.assertEqual(thermal.format_status(M(48.0, (), None, "acpi")), "Thermalzone 48 °C (grob, nicht CPU)")
        self.assertEqual(thermal.format_status(M(48.0, (), None, "perf")), "Thermalzone 48 °C (grob, nicht CPU)")
        self.assertEqual(thermal.format_status(M(71.0, (), None, "feature")), "CPU 71 °C (Quelle unbestätigt)")
        self.assertEqual(thermal.format_status(M(71.0, (), None, "gamezone")), "CPU 71 °C")

    def test_measure_carries_source(self):
        p = prov(SourceHelper(gz=0, feat=0, acpi="CurrentTemperature=3231"))
        p.open()
        self.assertEqual(p.measure().source, "acpi")

    def _ctl(self, key, label="x", statuses=None):
        class P(thermal.ThermalProvider):
            name = "t"
            temp_source = key
            temp_source_label = label

            def close(self):
                pass

        notes = []
        st = (lambda t, s: statuses.append((t, s))) if statuses is not None else None
        ctl = thermal.ThermalController(P(), None, None, lambda lvl, txt: notes.append((lvl, txt)), st)
        return ctl, notes

    def test_setup_warns_for_coarse_zone_only(self):
        ctl, notes = self._ctl("acpi", "ACPI-Thermalzone (grob, oft träge)")
        ctl.setup()
        warn = [t for lv, t in notes if lv == "warn" and t.startswith("Temperaturquelle:")]
        self.assertEqual(len(warn), 1)
        self.assertIn("Schwellen gelten für die CPU; die Zone liegt meist darunter", warn[0])
        self.assertIn("Schutz eingeschränkt, ggf. CPU-Kerne begrenzen", warn[0])
        ctl, notes = self._ctl("feature", "Lenovo ... (unbestätigt)")
        ctl.setup()
        self.assertFalse([1 for lv, t in notes if lv == "warn" and t.startswith("Temperaturquelle:")])

    def test_constant_guard_fails_b_c_d(self):
        for key in thermal.CONSTANT_GUARD_SOURCES:
            ctl, _ = self._ctl(key)
            for _ in range(thermal.CONSTANT_GUARD_COUNT - 1):
                ctl.tick(thermal.Messung(50.0, (), None, key))
            self.assertEqual(ctl.failed, "", key)
            ctl.tick(thermal.Messung(50.0, (), None, key))
            self.assertEqual(ctl.failed, "Temperaturquelle liefert einen konstanten Wert", key)

    def test_changing_value_resets_guard(self):
        ctl, _ = self._ctl("perf")
        n = thermal.CONSTANT_GUARD_COUNT
        for i in range(3 * n):
            ctl.tick(thermal.Messung(50.0 + (i // (n - 1)) * 0.5, (), None, "perf"))
        self.assertEqual(ctl.failed, "")

    def test_guard_not_for_source_a(self):
        ctl, _ = self._ctl("gamezone")
        for _ in range(3 * thermal.CONSTANT_GUARD_COUNT):
            ctl.tick(thermal.Messung(50.0, (), None, "gamezone"))
        self.assertEqual(ctl.failed, "")

    def test_failed_guard_status_text(self):
        statuses = []
        ctl, _ = self._ctl("acpi", statuses=statuses)
        for _ in range(thermal.CONSTANT_GUARD_COUNT):
            ctl.tick(thermal.Messung(50.0, (), None, "acpi"))
        self.assertEqual(statuses[-1], ("Temperatur nicht verfügbar: Temperaturquelle liefert einen konstanten Wert",
                                        "unavailable"))

    def test_diagnose_verdict_for_coarse_zone(self):
        h = SourceHelper(gz=0, feat=0, acpi="CurrentTemperature=3231", fan1=0, fan2=0)
        lines = []
        thermal.run_thermal_test(lines.append, provider_factory=lambda: prov(h, diagnose=True), sleep=lambda s: None,
                                 full_seconds=0.0, samples=1, input_fn=lambda *_: "")
        text = "\n".join(lines)
        self.assertIn("nur grobe Thermalzone", text)
        self.assertTrue(any(x.startswith("ERGEBNIS:") and "nur grobe Thermalzone - Schutz eingeschränkt" in x
                            for x in lines), text)


class HelperFailureTests(unittest.TestCase):
    def test_timeout_in_b_still_tries_c(self):
        h = SourceHelper(gz=0, feat=thermal.HelperTimeout("keine Antwort"), acpi="CurrentTemperature=3231")
        p = prov(h)
        p.open()
        self.assertEqual(p.temp_source, "acpi")

    def test_timeout_in_fan_fallback_keeps_provider_available(self):
        h = SourceHelper(gz=60, fan1=0, fan2=0, feat_fan={thermal.FEATURE_ID_FAN1_SPEED: thermal.HelperTimeout("x"),
                                                          thermal.FEATURE_ID_FAN2_SPEED: thermal.HelperTimeout("y")})
        av = thermal.check_availability(lambda: prov(h))
        self.assertTrue(av.ok)
        self.assertEqual(av.provider.temp_source, "gamezone")
        av.provider.close()

    def test_diagnose_lists_failing_source_and_continues(self):
        h = SourceHelper(gz=0, feat=thermal.HelperTimeout("x"), acpi="CurrentTemperature=3231")
        p = prov(h, diagnose=True)
        p.open()
        rep = "\n".join(p.candidate_report)
        self.assertIn("FEHLER HelperTimeout", rep)
        self.assertIn("c) ACPI", rep)
        self.assertIn("SmartFanMode", rep)


class ScriptAllowlistTests(unittest.TestCase):
    script = thermal._PS_SCRIPT

    def test_ids_match_constants(self):
        for v in (thermal.FEATURE_ID_FAN_FULL_SPEED, thermal.FEATURE_ID_CPU_TEMP, thermal.FEATURE_ID_FAN1_SPEED,
                  thermal.FEATURE_ID_FAN2_SPEED):
            self.assertIn(str(v), self.script)

    def test_fixed_list_instead_of_pattern(self):
        # (das alte Muster bleibt als erste Stufe, die feste Liste kommt zusätzlich und entscheidet)
        self.assertNotIn("SetSmartFanMode", self.script)
        for m in ("GetCPUTemp", "GetFan1Speed", "GetFan2Speed", "GetFanMaxSpeed", "GetSmartFanMode"):
            self.assertIn(f"'{m}'", self.script)
        self.assertEqual(self.script.count("SetFeatureValue"), 1)
        self.assertIn("-ceq 'LENOVO_GAMEZONE_DATA'", self.script)
        # die Ablehnung steht VOR dem Aufruf
        self.assertLess(self.script.index("if (-not $ok) { throw 'BadArgs' }"), self.script.index("Invoke-CimMethod"))

    def test_set_only_fan_full_with_0_or_1(self):
        i = self.script.index("-ceq 'SetFeatureValue'")
        seg = self.script[i:i + 500]
        self.assertIn(f"-eq {thermal.FEATURE_ID_FAN_FULL_SPEED}", seg)
        self.assertIn("@(0, 1)", seg)


if __name__ == "__main__":
    unittest.main()
