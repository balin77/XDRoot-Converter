"""Tests fuer keepawake (Energiesparmodus waehrend des Laufs verhindern)."""
import tempfile
import threading
import unittest
from unittest import mock

from xdroot_converter import converter, keepawake, tools

CONT = keepawake.ES_CONTINUOUS
SYS = keepawake.ES_SYSTEM_REQUIRED


class Fake:
    def __init__(self, ret=CONT):
        self.calls = []  # (flags, thread-id)
        self.ret = ret

    def __call__(self, flags):
        self.calls.append((flags, threading.get_ident()))
        return self.ret


def patched(fake, platform="win32"):
    return (mock.patch.object(keepawake, "_set_thread_execution_state", fake),
            mock.patch.object(keepawake.sys, "platform", platform))


class KeepAwakeTests(unittest.TestCase):
    def run_with(self, fake, platform="win32", body=None, log=None):
        p1, p2 = patched(fake, platform)
        with p1, p2:
            with keepawake.keep_awake(log) as active:
                if body:
                    body()
        return active

    def test_flags_constants(self):
        self.assertEqual(CONT, 0x80000000)
        self.assertEqual(SYS, 0x00000001)

    def test_enter_and_exit_flags(self):
        f, msgs = Fake(), []
        self.assertTrue(self.run_with(f, log=msgs.append))
        self.assertEqual([c[0] for c in f.calls], [CONT | SYS, CONT])
        self.assertEqual(msgs, [keepawake.MSG_ACTIVE])

    def test_reset_on_exception(self):
        f = Fake()
        with self.assertRaises(ValueError):
            self.run_with(f, body=lambda: (_ for _ in ()).throw(ValueError("x")))
        self.assertEqual([c[0] for c in f.calls], [CONT | SYS, CONT])

    def test_not_windows_no_call(self):
        f, msgs = Fake(), []
        self.assertFalse(self.run_with(f, platform="linux", log=msgs.append))
        self.assertEqual(f.calls, [])
        self.assertTrue(msgs[0].startswith("Hinweis: Energiesparmodus konnte nicht verhindert werden"))

    def test_return_zero_is_hint_not_error(self):
        f, msgs = Fake(ret=0), []
        self.assertFalse(self.run_with(f, log=msgs.append))
        self.assertEqual([c[0] for c in f.calls], [CONT | SYS])  # nichts zurueckzunehmen
        self.assertTrue(msgs[0].startswith("Hinweis:"))

    def test_exception_is_hint_not_error(self):
        def boom(flags):
            raise AttributeError("kein windll")
        msgs = []
        with mock.patch.object(keepawake, "_set_thread_execution_state", boom), \
                mock.patch.object(keepawake.sys, "platform", "win32"):
            with keepawake.keep_awake(msgs.append) as active:
                pass
        self.assertFalse(active)
        self.assertTrue(msgs[0].startswith("Hinweis:"))

    def test_failing_log_callback_is_ignored(self):
        def bad(_):
            raise RuntimeError("log kaputt")
        self.assertTrue(self.run_with(Fake(), log=bad))

    def test_nesting_only_outer_acts(self):
        f, msgs = Fake(), []
        p1, p2 = patched(f)
        with p1, p2:
            with keepawake.keep_awake(msgs.append):
                with keepawake.keep_awake(msgs.append):
                    pass
                self.assertEqual(len(f.calls), 1)  # inneres Verlassen setzt nichts zurueck
        self.assertEqual([c[0] for c in f.calls], [CONT | SYS, CONT])
        self.assertEqual(len(msgs), 1)
        # danach wieder frei nutzbar
        with p1, p2:
            with keepawake.keep_awake():
                pass
        self.assertEqual(len(f.calls), 4)

    def test_converter_run_same_thread(self):
        f = Fake()
        res = {}
        with tempfile.TemporaryDirectory() as d:
            opts = converter.Options(source=d + "/nichtda", target=d + "/ziel")

            def work():
                res["tid"] = threading.get_ident()
                res["summary"] = converter.Converter(opts, None, tools.CancelToken()).run()

            p1, p2 = patched(f)
            with p1, p2, mock.patch.object(tools, "check_tools", side_effect=RuntimeError("kein ffmpeg")):
                t = threading.Thread(target=work)
                t.start()
                t.join(30)
        self.assertTrue(res["summary"].fatal)  # Fehler -> trotzdem zurueckgesetzt
        self.assertEqual([c[0] for c in f.calls], [CONT | SYS, CONT])
        self.assertEqual({c[1] for c in f.calls}, {res["tid"]})
        self.assertNotEqual(res["tid"], threading.main_thread().ident)

    def test_converter_testrun_does_not_touch(self):
        f = Fake()
        with tempfile.TemporaryDirectory() as d:
            opts = converter.Options(source=d + "/nichtda", target=d + "/ziel", max_frames=5)
            p1, p2 = patched(f)
            with p1, p2, mock.patch.object(tools, "check_tools", side_effect=RuntimeError("x")):
                converter.Converter(opts, None, tools.CancelToken()).run()
        self.assertEqual(f.calls, [])


@unittest.skipUnless(keepawake.sys.platform == "win32", "nur Windows")
class RealWindowsTest(unittest.TestCase):
    def test_real_call(self):
        first = keepawake._set_thread_execution_state(CONT | SYS)
        try:
            self.assertNotEqual(first, 0)
        finally:
            prev = keepawake._set_thread_execution_state(CONT)
        self.assertEqual(prev & (CONT | SYS), CONT | SYS)  # vorheriger Zustand war gesetzt
        self.assertEqual(keepawake._set_thread_execution_state(CONT) & SYS, 0)  # nach dem Zuruecksetzen ohne SYSTEM

    def test_real_context_manager(self):
        with keepawake.keep_awake() as active:
            self.assertTrue(active)
        self.assertEqual(keepawake._set_thread_execution_state(CONT) & SYS, 0)


class IsActiveTests(unittest.TestCase):
    def test_is_active_nested_and_released(self):
        with mock.patch.object(keepawake, "_set_thread_execution_state", return_value=1),                 mock.patch.object(keepawake.sys, "platform", "win32"):
            self.assertFalse(keepawake.is_active())
            with keepawake.keep_awake():
                self.assertTrue(keepawake.is_active())
                with keepawake.keep_awake() as inner:  # verschachtelt (Listenlauf + Converter.run)
                    self.assertTrue(inner)
                    self.assertTrue(keepawake.is_active())
            self.assertFalse(keepawake.is_active())


if __name__ == "__main__":
    unittest.main()
