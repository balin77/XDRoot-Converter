import io
import subprocess
import sys
import threading
import time
import unittest
from contextlib import redirect_stdout
from fractions import Fraction
from unittest import mock

from xdroot_converter import cli, converter, tools, verify


class Helpers(unittest.TestCase):
    def test_date_arg(self):
        self.assertEqual(converter.mxf_date_to_arg("2023-05-29 16:46:52.000"), "2023-05-29T16:46:52:00")
        self.assertEqual(converter.mxf_date_to_arg("2023-05-29 16:46:52.500"), "2023-05-29T16:46:52:125")
        self.assertIsNone(converter.mxf_date_to_arg("garbage"))

    def test_estimate(self):
        # 31:35 min 50p, 8 Kanaele: ca. 12 GB Video + 2.2 GB Audio
        b = converter.estimate_clip_bytes(94760, Fraction(50), 8, 50)
        self.assertTrue(12e9 < b < 16e9, b)

    def test_format(self):
        self.assertEqual(converter.fmt_size(512), "512 B")
        self.assertEqual(converter.fmt_size(1536), "1.50 KB")
        self.assertEqual(converter.fmt_duration(75), "1:15")
        self.assertEqual(converter.fmt_duration(3725), "1:02:05")

    def test_constants_match_fx6_reference(self):
        self.assertEqual(verify.XAVCL_ESSENCE_TYPE, "AVC_High_422")
        self.assertTrue(verify.XAVCL_CODING_LABEL.endswith("04010202.01316001"))

    def test_summary_text(self):
        s = converter.RunSummary(results=[converter.ClipResult("a.MXF", converter.ST_CONVERTED, 1000, 250, 10, 1.0)])
        self.assertIn("umgewandelt: 1", s.text())

    def test_level_from_probe(self):
        self.assertEqual(converter.level_from_probe("42"), "4.2")
        self.assertEqual(converter.level_from_probe(41), "4.1")
        self.assertEqual(converter.level_from_probe("40"), "4.0")
        for bad in ("", None, "x", "0", "-3"):
            self.assertIsNone(converter.level_from_probe(bad))

    def test_level_from_fps(self):
        from fractions import Fraction
        for f, lv in ((Fraction(25), "4.1"), (Fraction(30000, 1001), "4.1"), (Fraction(30), "4.1"),
                      (Fraction(50), "4.2"), (Fraction(60000, 1001), "4.2")):
            self.assertEqual(converter.level_from_fps(f), lv)

    def test_make_expect_level_only_if_supported(self):
        kw = dict(frames=1, mp_uid="a", fp_uid="b", start_tc="00:00:00:00", n_audio=1, fps=Fraction(50))
        e = converter.make_expect(level="4.2", **kw)
        if "level" in verify.ClipExpect.__dataclass_fields__:
            self.assertEqual(e.level, "4.2")
        self.assertEqual(converter.make_expect(**kw).frames, 1)

    def test_incomplete_text(self):
        ok = converter.RunSummary(results=[converter.ClipResult("a.MXF", converter.ST_CONVERTED, 10, 5)])
        self.assertEqual(ok.incomplete_text(), "")
        bad = converter.RunSummary(results=[
            converter.ClipResult("a.MXF", converter.ST_FAILED, 10, 0),
            converter.ClipResult("b.MXF", converter.ST_FAILED, 10, 7),
        ])
        t = bad.incomplete_text()
        self.assertIn("ACHTUNG: Zielkarte unvollstaendig - 1 Clip(s) fehlen: a.MXF", t)
        self.assertIn("b.MXF", t)
        self.assertEqual(bad.failed, 2)
        self.assertEqual(converter.RunSummary(copy_errors=[("x", "y")]).failed, 1)


class CliTests(unittest.TestCase):
    def test_detail_hidden_without_verbose(self):
        for verbose, shown in ((False, False), (True, True)):
            buf = io.StringIO()
            with redirect_stdout(buf):
                r = cli.CliReporter(verbose=verbose)
                r.log("detail", "Einzelheit")
                r.log("info", "Info")
                r.log("warn", "Achtung")
            out = buf.getvalue()
            self.assertEqual("Einzelheit" in out, shown)
            self.assertIn("Info", out)
            self.assertIn("WARNUNG: Achtung", out)

    def test_verbose_option(self):
        self.assertTrue(cli.build_parser().parse_args(["a", "b", "--verbose"]).verbose)
        self.assertFalse(cli.build_parser().parse_args(["a", "b"]).verbose)


PY = sys.executable


class ToolsTests(unittest.TestCase):
    def test_run_capture_cancel_is_prompt(self):
        """V1: Abbruch wirkt auch, ohne dass der Prozess endet (kurze Wartezeiten statt proc.wait())."""
        cancel = tools.CancelToken()
        threading.Timer(0.5, cancel.cancel).start()
        t0 = time.time()
        with self.assertRaises(tools.Cancelled):
            tools.run_capture([PY, "-c", "import time; time.sleep(30)"], cancel=cancel)
        self.assertLess(time.time() - t0, 10)

    def test_run_capture_timeout(self):
        t0 = time.time()
        r = tools.run_capture([PY, "-c", "import time; time.sleep(30)"], timeout=0.5)
        self.assertTrue(r.timed_out)
        self.assertLess(time.time() - t0, 10)

    def test_run_capture_collects_output(self):
        r = tools.run_capture([PY, "-c", "import sys; print('a'); print('b', file=sys.stderr); sys.exit(3)"])
        self.assertEqual((r.returncode, r.stdout.strip(), r.stderr.strip(), r.timed_out), (3, "a", "b", False))

    def test_run_capture_does_not_hang_when_grandchild_keeps_pipe(self):
        code = ("import subprocess, sys; subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(20)']); print('x')")
        t0 = time.time()
        r = tools.run_capture([PY, "-c", code])
        self.assertLess(time.time() - t0, 15)
        self.assertEqual(r.returncode, 0)

    def test_kill_process_falls_back_to_kill_when_taskkill_fails(self):
        """V3: taskkill schlaegt fehl (Rueckgabecode != 0 oder nicht vorhanden) -> trotzdem proc.kill()."""
        for effect in (subprocess.CompletedProcess([], 1), FileNotFoundError("taskkill")):
            proc = mock.Mock()
            proc.poll.return_value = None
            proc.pid = 1234
            with mock.patch.object(tools.sys, "platform", "win32"), \
                    mock.patch.object(tools.subprocess, "run", side_effect=[effect] if isinstance(effect, Exception) else None,
                                      return_value=None if isinstance(effect, Exception) else effect):
                tools.kill_process(proc)
            proc.kill.assert_called_once()

    def test_kill_all_kills_registered(self):
        cancel = tools.CancelToken()
        p = subprocess.Popen([PY, "-c", "import time; time.sleep(30)"])
        cancel.register(p)
        cancel.kill_all()
        p.wait(timeout=10)
        self.assertIsNotNone(p.returncode)
        self.assertFalse(cancel.cancelled)  # kill_all setzt das Abbruch-Flag nicht, cancel() schon
        cancel.unregister(p)


if __name__ == "__main__":
    unittest.main()
