"""Tests der Schlussrunde zur Temperaturüberwachung: Kennung nach gescheitertem Zurückstellen, Programmende während der
Wiederherstellung, technische Ursache in der Verfügbarkeit, Diagnose (Urteile, Ausgabedatei), CLI-Meldungen.
Nichts schreibt in den echten Programmordner oder nach %LOCALAPPDATA%."""

import io
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from xdroot_converter import cli, converter, thermal

try:
    from test_thermal import FakeLegionHelper, PY, legion, new_store
except ImportError:  # pragma: no cover
    from tests.test_thermal import FakeLegionHelper, PY, legion, new_store


def dead_owner_file(store, prev=0, attempts=0):
    child = subprocess.Popen([PY, "-c", "pass"])
    child.wait()
    store.dir.mkdir(parents=True, exist_ok=True)
    store.path.write_text(json.dumps({"version": 2, "pid": child.pid, "pstart": converter._proc_start_time(child.pid),
                                      "token": "t", "feature_id": thermal.FEATURE_ID_FAN_FULL_SPEED, "prev": prev,
                                      "attempts": attempts}), encoding="utf-8")


class ReleaseAndRestoreTests(unittest.TestCase):
    def test_failed_restore_does_not_lock_next_provider_in_same_process(self):
        store = new_store()
        h = FakeLegionHelper(feature=0)
        a = legion(h, state_store=store)
        a.open()
        a.fan_full()
        h.ignore_writes = True  # Zurückstellen scheitert (Rücklesen stimmt nicht)
        with self.assertRaises(thermal.ProviderError):
            a.fan_restore()
        a.close()
        self.assertTrue(store.path.exists())
        h2 = FakeLegionHelper(feature=1)
        b = legion(h2, state_store=store)
        b.open()  # Besitzer gilt als tot: wiederherstellen
        self.assertEqual(h2.feature, 0)
        self.assertTrue(b.fan_control_info()[0])
        self.assertFalse(store.path.exists())
        self.assertIn("wiederhergestellt", " ".join(t for _l, t in b.take_messages()))

    def test_abort_during_restore_does_not_count_attempt(self):
        store = new_store()
        dead_owner_file(store, prev=0, attempts=1)
        h = FakeLegionHelper(feature=1, ignore_writes=True)
        p = legion(h, state_store=store)
        h.on_set = p.abort  # Programmende mitten in der Wiederherstellung
        p.open()
        self.assertFalse(p.fan_control_info()[0])
        data = json.loads(store.path.read_text(encoding="utf-8"))
        self.assertEqual(data["attempts"], 1, "kein Fehlversuch bei Programmende")

    def test_real_failures_still_count_and_discard_after_three(self):
        store = new_store()
        dead_owner_file(store, prev=0, attempts=2)
        h = FakeLegionHelper(feature=1, ignore_writes=True)
        p = legion(h, state_store=store)
        p.open()
        self.assertFalse(store.path.exists(), "nach drei Fehlversuchen verworfen")
        self.assertIn("verworfen", " ".join(t for l, t in p.take_messages() if l == "warn"))

    def test_status_reason_has_no_path_or_counter(self):
        store = new_store()
        dead_owner_file(store, prev=0, attempts=0)
        p = legion(FakeLegionHelper(feature=1, ignore_writes=True), state_store=store)
        p.open()
        note = p.fan_control_info()[1]
        self.assertEqual(note, "Lüftersteuerung gesperrt (vorheriger Lauf nicht sauber beendet)")
        self.assertNotIn(str(store.dir), note)
        self.assertIn(str(store.path), " ".join(t for _l, t in p.take_messages()), "Pfad steht im Protokoll")

    def test_measure_stops_between_partial_calls_when_closing(self):
        h = FakeLegionHelper()
        p = legion(h)
        p.open()
        orig = p._data
        seen = []

        def data(method, timeout=None):
            seen.append(method)
            r = orig(method, timeout)
            if method == "GetFan1Speed":
                p.begin_close()
            return r

        p._data = data
        with self.assertRaises(thermal.HelperError):
            p._measure_once()
        self.assertNotIn("GetFan2Speed", seen)


class AvailabilityTests(unittest.TestCase):
    def test_detail_from_cause_and_messages(self):
        h = FakeLegionHelper(denied=("", "komisch"))
        av = thermal.check_availability(lambda: legion(h))
        self.assertFalse(av.ok)
        self.assertTrue(av.detail.startswith("HelperRemoteError"), av.detail)
        self.assertNotIn("HelperRemoteError", av.reason)

    def test_session_logs_provider_messages_of_unavailable(self):
        class Prov(thermal.ThermalProvider):
            def open(self):
                self._m = [("warn", "Datei verworfen")]
                raise thermal.ProviderUnavailable("nicht nutzbar", "other")

            def take_messages(self):
                m, self._m = getattr(self, "_m", []), []
                return m

        items = []
        s = thermal.ThermalSession(thermal.ThermalConfig(), None, notify=lambda l, t: items.append((l, t)),
                                   provider_factory=Prov)
        s.start()
        s._thread.join(5)
        self.assertIn(("warn", "Datei verworfen"), items)

    def test_no_doubled_text_in_unavailable_warning(self):
        class P(thermal.ThermalProvider):
            def fan_control_info(self):
                return False, "Lüftersteuerung auf diesem Gerät nicht verfügbar"

            def take_messages(self):
                return []

        items = []
        c = thermal.ThermalController(P(), thermal.ThermalConfig(), None, lambda l, t: items.append((l, t)))
        c.setup()
        txt = "\n".join(t for _l, t in items)
        self.assertNotIn("nicht verfügbar: Lüftersteuerung", txt)


class DiagnosisTests(unittest.TestCase):
    def run_test(self, factory, **kw):
        lines = []
        details = []
        rc = thermal.run_thermal_test(lines.append, provider_factory=factory, sleep=lambda s: None, full_seconds=0.01,
                                      samples=2, input_fn=lambda p="": "", detail=details.append, **kw)
        return rc, lines, details

    def test_busy_fan_control_names_reason_in_result_line(self):
        store = new_store()
        dead_owner_file(store)
        child_alive = subprocess.Popen([PY, "-c", "import time; time.sleep(30)"])
        try:
            data = json.loads(store.path.read_text(encoding="utf-8"))
            data["pid"], data["pstart"] = child_alive.pid, converter._proc_start_time(child_alive.pid)
            store.path.write_text(json.dumps(data), encoding="utf-8")
            h = FakeLegionHelper()
            rc, lines, _d = self.run_test(lambda: legion(h, state_store=store))
        finally:
            child_alive.kill()
            child_alive.wait()
        self.assertEqual(rc, 0)  # nur Messung möglich; die Ergebniszeile nennt den Grund
        self.assertTrue(lines[-1].startswith("ERGEBNIS: nur Messung möglich"), lines[-1])
        self.assertIn("anderen Lauf", lines[-1])
        self.assertEqual(h.writes(), [])

    def test_switch_error_gives_not_switched_and_no_restored_claim(self):
        h = FakeLegionHelper(ignore_writes=True)  # Rücklesen stimmt nicht
        rc, lines, _d = self.run_test(lambda: legion(h))
        self.assertEqual(rc, 1)
        self.assertTrue(lines[-1].startswith("ERGEBNIS: nicht geschaltet - "), lines[-1])
        self.assertNotIn("KEINE Wirkung", lines[-1])
        self.assertFalse(any("wiederhergestellt: geschrieben" in x for x in lines))

    def test_restore_failure_is_in_final_line(self):
        h = FakeLegionHelper(feature=0, boost=True)
        p = legion(h)

        def restore():
            raise thermal.ProviderError("kaputt")

        p.fan_restore = restore
        rc, lines, _d = self.run_test(lambda: p)
        self.assertEqual(rc, 1)
        self.assertIn("ACHTUNG: Die Lüfter konnten nicht zurückgestellt werden - bitte in Lenovo Vantage prüfen oder den "
                      "Test erneut starten.", lines[-1])

    def test_ctrl_c_reports_actual_restore_result(self):
        for fails in (False, True):
            h = FakeLegionHelper(feature=0)
            p = legion(h)
            real = p.measure
            n = [0]

            def measure(real=real, n=n):
                n[0] += 1
                if n[0] == 4:
                    raise KeyboardInterrupt
                return real()

            p.measure = measure
            if fails:
                def restore():
                    raise thermal.ProviderError("kaputt")

                p.fan_restore = restore
            rc, lines, _d = self.run_test(lambda: p)
            self.assertEqual(rc, 1)
            if fails:
                self.assertIn("ACHTUNG", lines[-1])
                self.assertNotIn("wurden zurückgestellt", lines[-1])
            else:
                self.assertIn("wurden zurückgestellt", lines[-1])

    def test_implausible_temperatures_with_readable_rpm_not_called_unreadable(self):
        h = FakeLegionHelper(cpu=0)  # unplausibel; Drehzahlen lesbar
        p = legion(h)
        p.open = lambda: None
        p._fan_mode = "feature"
        p._helper = h
        rc, lines, _d = self.run_test(lambda: p)
        out = "\n".join(lines)
        self.assertNotIn("Drehzahlen nicht lesbar", out)
        self.assertIn("VORHER: 2000/2100", out)

    def test_other_exception_in_open_is_short_error_not_traceback(self):
        class Boom(thermal.ThermalProvider):
            def open(self):
                raise RuntimeError("kaputt")

        rc, lines, details = self.run_test(Boom)
        self.assertEqual(rc, 1)
        self.assertTrue(lines[-1].startswith("ERGEBNIS: Fehler - RuntimeError: kaputt"), lines[-1])
        self.assertFalse(any("Traceback" in x for x in lines))
        self.assertTrue(any("Traceback" in x for x in details))

    def test_unavailable_shows_technical_cause_only_in_details(self):
        h = FakeLegionHelper(denied=("", "komisch"))
        rc, lines, details = self.run_test(lambda: legion(h))
        self.assertTrue(any("HelperRemoteError" in x for x in details))
        self.assertFalse(any("HelperRemoteError" in x for x in lines))


class DiagnosisFileTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp(prefix="xdroot-t3-"))
        self.addCleanup(shutil.rmtree, self.d, True)

    def test_output_is_also_written_to_file_utf8_and_overwritten(self):
        path = self.d / "thermal_test.txt"
        path.write_text("ALTE-DATEI", encoding="utf-8")
        lines = []
        h = FakeLegionHelper(feature=0, boost=True)
        rc = thermal.run_thermal_test_logged(lines.append, log_path=path, provider_factory=lambda: legion(h),
                                             sleep=lambda s: None, full_seconds=0.01, samples=2, input_fn=lambda p="": "")
        txt = path.read_text(encoding="utf-8")
        self.assertNotIn("ALTE-DATEI", txt)
        self.assertIn("Temperatur-Diagnose", txt)
        self.assertIn("ERGEBNIS", txt)
        self.assertIn("Lüfter", txt)
        self.assertIn(f"Diese Ausgabe steht auch in {path} - bitte diese Datei an den Autor schicken.", lines[-1])
        self.assertEqual(rc, 0)

    def test_file_is_written_on_abort_and_error(self):
        path = self.d / "t.txt"

        def boom():
            raise RuntimeError("kaputt")

        thermal.run_thermal_test_logged(lambda s="": None, log_path=path, provider_factory=boom)
        self.assertIn("ERGEBNIS: Fehler - RuntimeError: kaputt", path.read_text(encoding="utf-8"))

    def test_unwritable_file_does_not_disturb_the_test(self):
        lines = []
        h = FakeLegionHelper(feature=0, boost=True)
        rc = thermal.run_thermal_test_logged(lines.append, log_path=self.d / "gibt" / "es" / "nicht.txt",
                                             provider_factory=lambda: legion(h), sleep=lambda s: None, full_seconds=0.01,
                                             samples=2, input_fn=lambda p="": "")
        self.assertEqual(rc, 0)
        self.assertTrue(any(x.startswith("ERGEBNIS") for x in lines))

    def test_default_location_is_redirected_in_tests(self):
        self.assertNotEqual(thermal.THERMAL_TEST_LOG_DIR, None, "Tests dürfen nicht in den Programmordner schreiben")


class CliTextTests(unittest.TestCase):
    def test_help_does_not_break_between_number_and_unit(self):
        out = io.StringIO()
        with mock.patch("sys.stdout", out), mock.patch.dict("os.environ", {"COLUMNS": "60"}), self.assertRaises(SystemExit):
            cli.build_parser().parse_args(["--help"])
        txt = out.getvalue()
        self.assertNotRegex(txt, r"\d\s*\n\s*°C")
        self.assertIn("Lüfter voll ab", txt)

    def test_mxf_info_on_folder_is_german_message(self):
        d = tempfile.mkdtemp(prefix="xdroot-t4-")
        self.addCleanup(shutil.rmtree, d, True)
        err = io.StringIO()
        with mock.patch("sys.stderr", err), mock.patch("sys.stdout", io.StringIO()):
            self.assertEqual(cli.main(["--mxf-info", d]), 2)
        self.assertIn("Ordner", err.getvalue())
        self.assertNotIn("Permission", err.getvalue())

    def test_os_messages_are_german(self):
        self.assertEqual(cli._os_message(FileNotFoundError(2, "No such file"), "x.mxf"), "Datei nicht gefunden: x.mxf")
        self.assertEqual(cli._os_message(PermissionError(13, "Permission denied"), "x"), "Kein Zugriff auf: x")


if __name__ == "__main__":
    unittest.main()
