"""Runde 6 (SPEC "Lüfter halten und Last drosseln statt pendeln"): Lüfter bleibt bis Laufende voll, Drosselung in Stufen
(100/75/50/35/25 %) über die Prozess-Affinität vor der Pause, Sperrzeiten, Pendel-Sperre, Notfall-Pause, Rückfall auf die
Pause ohne Affinität, Kernauswahl.  Alles mit Attrappen (Regler, Anbieter, pause_ctl); dazu EIN echter kurzer Windows-Test
mit einem selbst gestarteten Schlaf-Prozess (keine Last, keine fremden Prozesse)."""

import subprocess
import sys
import time
import unittest
from unittest import mock

from xdroot_converter import converter, gui, thermal, tools

M = thermal.Messung
STEPS = thermal.THROTTLE_STEPS


# --------------------------------------------------------------------------- Attrappen
class Prov(thermal.ThermalProvider):
    name = "Attrappe"

    def __init__(self, fan=True):
        self.fan = fan
        self.fulls = 0
        self.restores = 0
        self.next = None  # Exception, die die nächste Messung wirft

    def fan_control_info(self):
        return self.fan, "Attrappe"

    def fan_full(self):
        self.fulls += 1

    def fan_restore(self):
        self.restores += 1

    def measure(self):
        if self.next is not None:
            e, self.next = self.next, None
            raise e
        return M(50.0)

    def close(self):
        pass


class Ctl:
    """pause_ctl-Attrappe mit set_throttle/throttle_cores."""

    def __init__(self, total=32, set_ok=True, pause_ok=True):
        self.total = total
        self.set_ok = set_ok
        self.pause_ok = pause_ok
        self.calls = []  # set_throttle-Aufrufe (Anteil)
        self.events = []
        self.paused = False

    def pause(self):
        self.events.append("pause")
        self.paused = self.pause_ok
        return self.pause_ok

    def resume(self):
        self.events.append("resume")
        self.paused = False
        return True

    def set_throttle(self, fraction):
        self.calls.append(fraction)
        return self.set_ok

    def throttle_cores(self, fraction):
        return tools.throttle_core_count(self.total, fraction), self.total


class CtlNoCores(Ctl):
    throttle_cores = None  # kann die Kernzahl nicht nennen -> Anzeige in Prozent


class PauseOnly:
    """Alter pause_ctl ohne set_throttle."""

    def __init__(self):
        self.events = []

    def pause(self):
        self.events.append("pause")
        return True

    def resume(self):
        self.events.append("resume")
        return True


class Rig:
    def __init__(self, ctl=None, cfg=None, fan=True, **kw):
        self.prov = Prov(fan)
        self.ctl = Ctl() if ctl is None else ctl
        self.now = [0.0]
        self.notes = []
        self.statuses = []
        self.c = thermal.ThermalController(
            self.prov, cfg, self.ctl, lambda lv, tx: self.notes.append((lv, tx)),
            lambda tx, st: self.statuses.append((tx, st)), clock=lambda: self.now[0], **kw)
        self.c.setup()

    def tick(self, temp, dt=5.0):
        self.now[0] += dt
        self.c.tick(M(float(temp), (3000, 3100)))

    def fail(self, exc, dt=5.0):
        """Die nächste Messung des Anbieters wirft ``exc`` (Ausfall der Messung)."""
        self.now[0] += dt
        self.prov.next = exc
        self.c.tick()

    def run(self, temp, seconds, dt=5.0):
        n = int(round(seconds / dt))
        for _ in range(n):
            self.tick(temp, dt)

    def text(self, level=None):
        return "\n".join(t for lv, t in self.notes if level is None or lv == level)


# --------------------------------------------------------------------------- Lüfter halten
class FanHoldTests(unittest.TestCase):
    def test_fan_stays_full_after_cooling_until_finish(self):
        r = Rig()
        r.run(86, 10)  # 2 Messungen >= 85
        self.assertTrue(r.c.fan_full)
        self.assertEqual(r.prov.fulls, 1)
        r.run(50, 600)  # lange Abkühlung (weit unter der früheren Rückschalt-Schwelle 75)
        self.assertTrue(r.c.fan_full)
        self.assertEqual(r.prov.restores, 0)
        r.run(90, 30)  # wieder warm: kein zweites Schalten
        self.assertEqual(r.prov.fulls, 1)
        r.c.finish()
        self.assertEqual(r.prov.restores, 1)
        self.assertFalse(r.c.fan_full)

    def test_fan_stays_full_during_pause_and_resume(self):
        r = Rig(ctl=PauseOnly())
        r.run(99, 10)
        self.assertTrue(r.c.paused)
        r.run(60, 20)  # 3 Messungen unter 85 -> Pause beendet
        self.assertFalse(r.c.paused)
        self.assertTrue(r.c.fan_full)
        self.assertEqual(r.prov.restores, 0)

    def test_restore_in_every_end_path(self):
        # 1. finish (fertig/Fehler/Abbruch/Strg+C enden alle über finish)
        r = Rig()
        r.run(90, 10)
        r.c.finish()
        self.assertEqual(r.prov.restores, 1)
        # 2. Ausfall der Messung (Ausnahme)
        r = Rig()
        r.run(90, 10)
        r.fail(thermal.ProviderError("weg"))
        self.assertTrue(r.c.failed)
        self.assertEqual(r.prov.restores, 1)
        r.c.finish()
        self.assertEqual(r.prov.restores, 1)  # nicht doppelt
        # 3. unplausible Werte in Folge
        r = Rig()
        r.run(90, 10)
        r.run(0, 15)
        self.assertTrue(r.c.failed)
        self.assertEqual(r.prov.restores, 1)
        # 4. Hitze-Abbruch (Pause zu lang) mit Abbruch-Rückruf, danach finish
        aborts = []
        r = Rig(ctl=PauseOnly(), cfg=thermal.ThermalConfig(max_pause_s=100.0), on_abort=aborts.append)
        r.run(99, 10)
        self.assertTrue(r.c.paused)
        r.run(99, 120)
        self.assertTrue(aborts and r.c.aborted)
        r.c.finish()
        self.assertEqual(r.prov.restores, 1)
        # 5. Zurückstellen scheitert einmal: zweiter Versuch im selben Aufruf (bisheriges Verhalten bleibt)
        r = Rig()
        r.run(90, 10)
        orig = r.prov.fan_restore
        state = {"n": 0}

        def flaky():
            state["n"] += 1
            if state["n"] == 1:
                raise thermal.ProviderError("x")
            orig()

        r.prov.fan_restore = flaky
        r.c.finish()
        self.assertEqual(r.prov.restores, 1)

    def test_no_restore_when_never_switched(self):
        r = Rig()
        r.run(70, 60)
        r.c.finish()
        self.assertEqual((r.prov.fulls, r.prov.restores), (0, 0))

    def test_setup_text_has_no_fan_off_threshold(self):
        r = Rig()
        txt = r.text("info")
        self.assertIn("bleibt bis zum Laufende", txt)
        self.assertNotIn("zurück unter", txt)


# --------------------------------------------------------------------------- Stufen
class StepTests(unittest.TestCase):
    def test_step_down_sequence_with_30s_lock_then_pause_as_last_stage(self):
        r = Rig()
        r.tick(98)  # t=5: 1. Messung
        self.assertEqual(r.ctl.calls, [])
        r.tick(98)  # t=10: 2 Messungen in Folge -> 75 %
        self.assertEqual(r.ctl.calls, [0.75])
        r.run(98, 25)  # t=15..35: noch gesperrt (30 s)
        self.assertEqual(r.ctl.calls, [0.75])
        r.tick(98)  # t=40: 30 s seit dem Stufenwechsel -> 50 %
        self.assertEqual(r.ctl.calls, [0.75, 0.5])
        r.run(98, 30)  # t=45..70 -> 35 % bei t=70
        self.assertEqual(r.ctl.calls, [0.75, 0.5, 0.35])
        r.run(98, 30)  # t=100 -> 25 %
        self.assertEqual(r.ctl.calls, [0.75, 0.5, 0.35, 0.25])
        self.assertFalse(r.c.paused)  # auf der niedrigsten Stufe erst nach weiteren 30 s
        r.run(98, 25)  # t=105..125
        self.assertFalse(r.c.paused)
        r.tick(98)  # t=130: auf der niedrigsten Stufe 30 s weiter heiß -> Pause als letzte Stufe
        self.assertTrue(r.c.paused)
        self.assertEqual(r.ctl.events, ["pause"])
        self.assertEqual(r.c.stats.throttle_steps, 4)
        self.assertEqual(r.c.stats.pauses, 1)
        self.assertIn("Letzte Stufe", r.text("warn"))

    def test_single_hot_measurement_does_nothing(self):
        r = Rig()
        for t in (98, 80, 98, 80, 98, 80):
            r.tick(t)
        self.assertEqual(r.ctl.calls, [])
        self.assertFalse(r.c.paused)

    def test_threshold_is_inclusive(self):
        r = Rig()
        r.run(97.0, 10)
        self.assertEqual(r.ctl.calls, [0.75])

    def test_step_up_after_60s_below_threshold_minus_8(self):
        r = Rig()
        r.run(98, 40)  # -> 75 % (t=10) und 50 % (t=40)
        self.assertEqual(r.ctl.calls, [0.75, 0.5])
        r.tick(89)  # t=45: 89 = 97 - 8 gilt (Beginn der 60 s)
        r.run(85, 50)  # t=50..95: noch keine 60 s am Stück
        self.assertEqual(r.ctl.calls, [0.75, 0.5])
        r.tick(85)  # t=100: 55 s
        self.assertEqual(r.ctl.calls, [0.75, 0.5])
        r.tick(85)  # t=105: 60 s -> eine Stufe hoch (75 %)
        self.assertEqual(r.ctl.calls, [0.75, 0.5, 0.75])
        self.assertIn("Drosselung gelockert: 24 von 32 Kernen (85 °C)", r.text("info"))
        r.run(85, 55)  # weitere 55 s: je Schritt nur eine Stufe, nächste erst nach erneut 60 s
        self.assertEqual(r.ctl.calls, [0.75, 0.5, 0.75])
        r.tick(85)  # t=165
        self.assertEqual(r.ctl.calls, [0.75, 0.5, 0.75, 1.0])
        self.assertIn("Drosselung gelockert: 32 von 32 Kernen (85 °C)", r.text("info"))
        r.run(60, 300)  # bei 100 % bleibt es (kein weiterer Aufruf)
        self.assertEqual(len(r.ctl.calls), 4)

    def test_warm_measurement_resets_the_60s_timer(self):
        r = Rig()
        r.run(98, 10)  # 75 %
        r.run(85, 50)  # 50 s kühl
        r.tick(92)  # nicht mindestens 8 °C unter der Schwelle (> 89): Zeitmessung beginnt von vorn
        r.run(85, 55)
        self.assertEqual(r.ctl.calls, [0.75])
        r.run(85, 10)
        self.assertEqual(r.ctl.calls, [0.75, 1.0])

    def test_pendel_lock_blocks_the_higher_stage_after_quick_fall_back(self):
        r = Rig()
        r.run(98, 10)  # t=10: 75 %
        r.run(80, 65)  # t=15..75: 60 s kühl -> bei t=75 wieder 100 %
        self.assertEqual(r.ctl.calls, [0.75, 1.0])
        r.run(98, 30)  # t=80..105: heiß, 30 s nach dem Lockern (t=105) wieder runter, 30 s <= 120 s
        self.assertEqual(r.ctl.calls, [0.75, 1.0, 0.75])
        self.assertIn("nicht mehr versucht", r.text("info"))
        r.run(80, 300)  # lange kühl (aber < 15 min): 100 % wird NICHT versucht
        self.assertEqual(r.ctl.calls, [0.75, 1.0, 0.75])
        # tiefere Stufen dürfen weiter gelockert werden, nur nicht die gesperrte: erst runter auf 50 %, dann wieder 75 %
        r.run(98, 10)
        self.assertEqual(r.ctl.calls[-1], 0.5)
        r.run(80, 80)
        self.assertEqual(r.ctl.calls[-1], 0.75)
        r.run(80, 300)  # (die Sperre verfällt erst nach 15 min, siehe test_pendel_lock_expires_after_15_minutes)
        self.assertNotIn(1.0, r.ctl.calls[3:])

    def test_pendel_lock_expires_after_15_minutes(self):
        r = Rig()
        r.run(98, 10)  # t=10: 75 %
        r.run(80, 65)  # t=75: 100 %
        r.run(98, 10)  # t=85: sofort wieder runter -> Sperre von 100 % (Zeitpunkt t=85)
        self.assertEqual(r.ctl.calls, [0.75, 1.0, 0.75])
        r.run(80, 800)  # t=885: Sperre (900 s) noch nicht verfallen
        self.assertEqual(r.ctl.calls, [0.75, 1.0, 0.75])
        r.run(80, 170)  # t=1055: Sperre verfallen (t=985), die Stufe wird wieder versucht (t=1045)
        self.assertEqual(r.ctl.calls, [0.75, 1.0, 0.75, 1.0])
        self.assertIn("Pendel-Sperre", r.text("info"))
        # fällt sie erneut binnen 2 min zurück, gilt die Sperre wieder 15 min
        r.run(98, 10)  # t=1065
        self.assertEqual(r.ctl.calls[-1], 0.75)
        r.run(80, 600)  # t=1665
        self.assertEqual(r.ctl.calls[-1], 0.75)
        r.run(80, 400)  # t=2065: neue Sperre (ab t=1065) verfallen
        self.assertEqual(r.ctl.calls[-1], 1.0)

    def test_pendel_lock_is_free_again_after_one_hour(self):
        r = Rig()
        r.run(98, 10)
        r.run(80, 65)
        r.run(98, 10)  # Sperre von 100 %
        r.run(80, 3600)
        self.assertEqual(r.ctl.calls, [0.75, 1.0, 0.75, 1.0])

    def test_stairs_go_back_up_stepwise_after_one_hour_cool(self):
        r = Rig()
        r.run(98, 100)  # 75/50/35/25 %
        self.assertEqual(r.ctl.calls, [0.75, 0.5, 0.35, 0.25])
        r.run(60, 3600)
        self.assertEqual(r.ctl.calls[4:], [0.35, 0.5, 0.75, 1.0])

    def test_no_pendel_lock_when_fall_back_comes_late(self):
        r = Rig()
        r.run(98, 10)  # 75 %
        r.run(80, 65)  # 100 % bei t=75
        r.run(80, 125)  # t=200: lange stabil
        r.run(98, 30)  # t=230: mehr als 120 s nach dem Lockern -> normales Runterschalten, keine Sperre
        self.assertEqual(r.ctl.calls, [0.75, 1.0, 0.75])
        self.assertNotIn("nicht mehr versucht", r.text())
        r.run(80, 65)
        self.assertEqual(r.ctl.calls, [0.75, 1.0, 0.75, 1.0])  # darf wieder hoch

    def test_pause_shortly_after_step_up_also_blocks_it(self):
        r = Rig()
        r.run(98, 10)  # t=10: 75 %
        r.run(80, 65)  # t=75: wieder 100 %
        r.run(103, 10)  # Notfall-Pause binnen 120 s nach dem Lockern
        self.assertTrue(r.c.paused)
        r.run(60, 15)  # Pause endet
        self.assertFalse(r.c.paused)
        r.run(98, 40)  # wieder runter (75 %)
        r.run(80, 600)
        self.assertEqual(r.ctl.calls.count(1.0), 1)  # 100 % wird nicht mehr versucht

    def test_levels_with_equal_core_count_are_skipped(self):
        # 4 Kerne: 100 % = 4, 75 % = 3, 50 % = 2, 35 % und 25 % ebenfalls 2 -> 2 Kerne ist die niedrigste wirksame Stufe
        r = Rig(ctl=Ctl(total=4))
        r.run(98, 10)
        r.run(98, 30)
        self.assertEqual(r.ctl.calls, [0.75, 0.5])
        r.run(98, 25)
        self.assertFalse(r.c.paused)
        r.tick(98)  # 30 s nach der letzten wirksamen Stufe: Pause (nicht erst nach 35/25 %)
        self.assertTrue(r.c.paused)
        self.assertEqual(r.ctl.calls, [0.75, 0.5])


# --------------------------------------------------------------------------- Pause: Notfall und Rückfall
class PauseTests(unittest.TestCase):
    def test_emergency_pause_at_threshold_plus_5_ignores_the_stage(self):
        r = Rig()
        r.tick(102)
        self.assertFalse(r.c.paused)  # eine Messung allein genügt nicht
        r.tick(102)
        self.assertTrue(r.c.paused)
        self.assertEqual(r.ctl.calls, [])  # keine Drosselung davor
        self.assertIn("Notfall", r.text("warn"))

    def test_just_below_emergency_throttles_instead(self):
        r = Rig()
        r.run(101.9, 10)
        self.assertFalse(r.c.paused)
        self.assertEqual(r.ctl.calls, [0.75])

    def test_emergency_pause_while_throttled(self):
        r = Rig()
        r.run(98, 10)  # 75 %
        r.run(103, 10)  # Notfall trotz Sperre und obwohl noch nicht die niedrigste Stufe
        self.assertTrue(r.c.paused)
        self.assertEqual(r.ctl.calls, [0.75])

    def test_resume_and_throttle_stays(self):
        r = Rig()
        r.run(98, 130)
        self.assertTrue(r.c.paused)
        r.run(80, 15)
        self.assertFalse(r.c.paused)
        self.assertEqual(r.ctl.events, ["pause", "resume"])
        self.assertEqual(r.ctl.calls, [0.75, 0.5, 0.35, 0.25])  # Stufe bleibt nach der Pause
        self.assertIn("gedrosselt auf 8 von 32 Kernen", r.statuses[-1][0])

    def test_affinity_not_settable_falls_back_to_old_pause_behaviour(self):
        r = Rig(ctl=Ctl(set_ok=False))
        r.tick(98)
        r.tick(98)  # 1. Fehlschlag (t=10): keine Pause in dieser Messung, nächster Versuch mit der nächsten
        self.assertFalse(r.c.paused)
        r.tick(98)  # 2. Fehlschlag
        self.assertFalse(r.c.paused)
        self.assertEqual(r.ctl.calls, [0.75, 0.75])
        r.tick(98)  # 3. Fehlschlag in Folge: Drosselung aus, unbedingt zurück auf 100 %, altes Pausenverhalten
        self.assertTrue(r.c.paused)
        self.assertEqual(r.ctl.calls, [0.75, 0.75, 0.75, 1.0, 1.0])  # Aufheben (Attrappe scheitert) einmal wiederholt
        self.assertEqual(r.text("warn").count("nicht drosseln"), 1)
        self.assertEqual(r.text("warn").count("nicht bei allen Prozessen aufgehoben"), 1)
        n = len(r.ctl.calls)
        r.run(60, 15)  # Pause endet
        r.run(98, 10)  # erneut heiß: sofort Pause, kein weiterer Drossel-Versuch
        self.assertTrue(r.c.paused)
        self.assertEqual(r.text("warn").count("nicht drosseln"), 1)
        self.assertEqual([c for c in r.ctl.calls[n:] if c < 1.0], [])

    def test_pause_ctl_without_set_throttle_pauses_immediately(self):
        r = Rig(ctl=PauseOnly())
        r.tick(98)
        r.tick(98)
        self.assertTrue(r.c.paused)
        self.assertEqual(r.ctl.events, ["pause"])
        self.assertNotIn("gedrosselt", r.statuses[-1][0])

    def test_cancel_token_without_affinity_support_reports_false(self):
        with mock.patch.object(tools, "available_cpus", lambda: None):
            self.assertFalse(tools.CancelToken().set_throttle(0.5))
            self.assertIsNone(tools.CancelToken().throttle_cores(0.5))

    def test_no_measurement_never_throttles(self):
        r = Rig()
        r.fail(thermal.ProviderError("weg"))
        self.assertTrue(r.c.failed)
        r.tick(98)
        r.tick(98)
        self.assertEqual(r.ctl.calls, [1.0])  # nur das unbedingte Aufheben, danach nie wieder ein Drosselschritt
        self.assertFalse(r.c.paused)


# --------------------------------------------------------------------------- Ausfall hebt die Drosselung auf
class FailureTests(unittest.TestCase):
    def test_measurement_failure_lifts_throttle_and_ends_pause(self):
        r = Rig()
        r.run(98, 40)  # 50 %
        self.assertEqual(r.ctl.calls, [0.75, 0.5])
        r.fail(thermal.ProviderError("weg"))
        self.assertTrue(r.c.failed)
        self.assertEqual(r.ctl.calls[-1], 1.0)
        self.assertEqual(r.prov.restores, 1)
        r.run(98, 60)  # danach wird nie wieder gedrosselt
        self.assertEqual(r.ctl.calls.count(1.0), 1)
        self.assertEqual(r.ctl.calls[:-1], [0.75, 0.5])

    def test_failure_while_paused_lifts_everything(self):
        r = Rig()
        r.run(98, 130)
        self.assertTrue(r.c.paused)
        r.run(0, 15)  # drei unplausible Werte
        self.assertTrue(r.c.failed)
        self.assertFalse(r.c.paused)
        self.assertEqual(r.ctl.events, ["pause", "resume"])
        self.assertEqual(r.ctl.calls[-1], 1.0)
        self.assertEqual(r.prov.restores, 1)

    def test_finish_lifts_throttle_and_stats_stay(self):
        r = Rig()
        r.run(98, 40)
        r.c.finish()
        self.assertEqual(r.ctl.calls[-1], 1.0)
        self.assertEqual(r.prov.restores, 1)
        n = len(r.ctl.calls)
        r.c.finish()  # idempotent
        self.assertEqual(len(r.ctl.calls), n)
        self.assertEqual(r.c.stats.throttle_steps, 2)

    def test_set_throttle_raising_is_contained(self):
        class Bad(Ctl):
            def set_throttle(self, fraction):
                raise OSError("kaputt")

        r = Rig(ctl=Bad())
        r.run(98, 20)  # 3 Fehlschläge in Folge (t=10, 15, 20)
        self.assertTrue(r.c.paused)  # Rückfall auf die Pause, kein Fehler im Regler
        self.assertEqual(r.c.failed, "")


# --------------------------------------------------------------------------- Neue Prozesse erben die Stufe
class TokenTests(unittest.TestCase):
    def setUp(self):
        self.applied = []

        def fake_set(proc, cpus):
            self.applied.append((proc, list(cpus)))
            return True

        self.patches = [mock.patch.object(tools, "available_cpus", lambda: list(range(32))),
                        mock.patch.object(tools, "set_process_affinity", fake_set)]
        for p in self.patches:
            p.start()
            self.addCleanup(p.stop)

    @staticmethod
    def proc():
        p = mock.Mock()
        p.poll.return_value = None
        return p

    def token(self):
        """Token mit einstellbarer Zeit (kein echtes Warten): ``self.clk[0]`` vorstellen."""
        self.clk = [100.0]
        return tools.CancelToken(clock=lambda: self.clk[0])

    def test_new_process_starts_unthrottled_and_gets_the_stage_after_the_delay(self):
        tok = self.token()
        p1 = self.proc()
        tok.register(p1)
        self.assertEqual(self.applied, [])  # ungedrosselt: Affinität wird nicht angefasst
        self.assertTrue(tok.set_throttle(0.5))  # Prozess jünger als 3 s: kein Fehlschlag, kommt später dran
        self.assertEqual(self.applied, [])
        self.assertEqual(tok.throttle, 0.5)
        self.clk[0] += tools.THROTTLE_START_DELAY_S - 0.1
        self.assertTrue(tok.apply_throttle())
        self.assertEqual(self.applied, [])  # noch keine 3 s
        self.clk[0] += 0.1
        self.assertTrue(tok.apply_throttle())
        self.assertEqual(self.applied, [(p1, list(range(0, 32, 2)))])
        for _ in range(3):  # kein mehrfaches Setzen derselben Stufe
            tok.apply_throttle()
        self.assertEqual(len(self.applied), 1)
        p2 = self.proc()
        tok.register(p2)  # nächster Clip / Abschlussprüfung: erst die Verzögerung, dann die Stufe
        tok.apply_throttle()
        self.assertEqual(len(self.applied), 1)
        self.clk[0] += tools.THROTTLE_START_DELAY_S
        tok.apply_throttle()
        self.assertEqual(self.applied[-1], (p2, list(range(0, 32, 2))))
        self.assertEqual(len(self.applied), 2)
        self.applied.clear()
        self.assertTrue(tok.set_throttle(1.0))
        self.assertEqual({id(p) for p, _c in self.applied}, {id(p1), id(p2)})
        self.assertTrue(all(c == list(range(32)) for _p, c in self.applied))

    def test_stage_change_resets_only_changed_processes(self):
        tok = self.token()
        p1 = self.proc()
        tok.register(p1)
        self.clk[0] += 5
        tok.set_throttle(0.75)
        tok.set_throttle(0.75)  # gleiche Stufe: nicht erneut
        self.assertEqual(len(self.applied), 1)
        tok.set_throttle(0.5)
        self.assertEqual([len(c) for _p, c in self.applied], [24, 16])

    def test_unthrottled_young_process_is_not_touched_by_lift(self):
        tok = self.token()
        p1 = self.proc()
        tok.register(p1)
        self.clk[0] += 5
        tok.set_throttle(0.5)
        p2 = self.proc()
        tok.register(p2)  # jung, nie gedrosselt
        self.applied.clear()
        self.assertTrue(tok.set_throttle(1.0))
        self.assertEqual([p for p, _c in self.applied], [p1])

    def test_unregistered_and_finished_processes_are_not_touched(self):
        tok = self.token()
        p1, p2 = self.proc(), self.proc()
        tok.register(p1)
        tok.register(p2)
        tok.unregister(p1)
        p2.poll.return_value = 0  # beendet
        self.clk[0] += 5
        self.assertTrue(tok.set_throttle(0.25))  # kein laufender Prozess: Stufe gilt trotzdem für spätere
        self.assertEqual(self.applied, [])
        p3 = self.proc()
        tok.register(p3)
        self.clk[0] += 5
        self.assertTrue(tok.apply_throttle())
        self.assertEqual(self.applied, [(p3, list(range(0, 32, 4)))])

    def test_all_failed_reports_false_partial_reports_true(self):
        tok = self.token()
        p1, p2 = self.proc(), self.proc()
        tok.register(p1)
        tok.register(p2)
        self.clk[0] += 5
        with mock.patch.object(tools, "set_process_affinity", lambda p, c: False):
            self.assertFalse(tok.set_throttle(0.5))
        with mock.patch.object(tools, "set_process_affinity", lambda p, c: p is p1):
            self.assertTrue(tok.set_throttle(0.5))

    def test_pause_and_throttle_work_together(self):
        tok = self.token()
        p1 = self.proc()
        with mock.patch.object(tools, "suspend_process", lambda p: True), \
                mock.patch.object(tools, "resume_process", lambda p: True):
            tok.register(p1)
            self.clk[0] += 5
            tok.set_throttle(0.75)
            self.assertTrue(tok.pause())
            p2 = self.proc()
            tok.register(p2)  # während der Pause gestartet: sofort angehalten (wie bisher), gedrosselt erst nach der Verzögerung
            self.clk[0] += 5
            tok.apply_throttle()
            self.assertNotIn(p2, [q for q, _c in self.applied])  # angehaltene Prozesse werden nicht gedrosselt
            self.assertTrue(tok.resume())
            self.clk[0] += 3
            tok.apply_throttle()
            self.assertEqual(self.applied[-1][0], p2)

    def test_controller_with_real_token_applies_level_to_new_process_after_delay(self):
        tok = self.token()
        r = Rig(ctl=tok)
        r.run(98, 10)
        self.assertEqual(tok.throttle, 0.75)
        p = self.proc()
        tok.register(p)
        r.tick(98)  # Messung direkt nach dem Start des Prozesses: noch ungedrosselt
        self.assertEqual(self.applied, [])
        self.clk[0] += 3.0
        r.tick(98)  # nächste Messung nach >= 3 s: Stufe wird nachgezogen
        self.assertEqual(len(self.applied[-1][1]), 24)
        n = len(self.applied)
        r.tick(98)
        self.assertEqual(len(self.applied), n)  # nicht bei jeder Messung erneut
        self.assertIn("gedrosselt auf 24 von 32 Kernen", r.statuses[-1][0])

    def test_controller_tolerates_single_apply_failure_and_disables_after_three(self):
        class Tok(Ctl):
            def __init__(self):
                super().__init__()
                self.apply_ok = [True]
                self.applies = 0

            def apply_throttle(self):
                self.applies += 1
                return self.apply_ok[0]

        r = Rig(ctl=Tok())
        r.run(98, 10)
        self.assertEqual(r.ctl.calls, [0.75])
        r.ctl.apply_ok[0] = False
        r.tick(90)  # (90 °C: weder heiß noch kühl genug zum Lockern, die Stufe bleibt)
        r.tick(90)
        self.assertEqual(r.ctl.calls, [0.75])  # 2 Fehlschläge: noch toleriert
        r.ctl.apply_ok[0] = True
        r.tick(90)  # Erfolg setzt den Zähler zurück
        r.ctl.apply_ok[0] = False
        r.tick(90)
        r.tick(90)
        self.assertEqual(r.ctl.calls, [0.75])
        r.tick(90)  # 3 in Folge: Drosselung aus (unbedingt zurück auf 100 %)
        self.assertEqual(r.ctl.calls, [0.75, 1.0])
        self.assertIn("nicht drosseln", r.text("warn"))

    def test_controller_without_apply_throttle_is_supported(self):
        r = Rig()  # Ctl hat kein apply_throttle
        r.run(98, 20)
        self.assertEqual(r.ctl.calls[0], 0.75)


# --------------------------------------------------------------------------- Kernauswahl
class SelectionTests(unittest.TestCase):
    def test_core_counts(self):
        cpus = list(range(32))
        self.assertEqual([len(tools.select_cpus(cpus, s / 100)) for s in STEPS], [32, 24, 16, 11, 8])
        self.assertEqual([tools.throttle_core_count(4, s / 100) for s in STEPS], [4, 3, 2, 2, 2])
        self.assertEqual([tools.throttle_core_count(2, s / 100) for s in STEPS], [2, 2, 2, 2, 2])
        self.assertEqual([tools.throttle_core_count(1, s / 100) for s in STEPS], [1, 1, 1, 1, 1])
        self.assertEqual(tools.throttle_core_count(0, 0.5), 0)

    def test_even_thinning_32_cores(self):
        cpus = list(range(32))
        self.assertEqual(tools.select_cpus(cpus, 1.0), cpus)
        self.assertEqual(tools.select_cpus(cpus, 0.75), [0, 1, 2, 4, 5, 6, 8, 9, 10, 12, 13, 14, 16, 17, 18, 20, 21, 22,
                                                          24, 25, 26, 28, 29, 30])
        self.assertEqual(tools.select_cpus(cpus, 0.5), list(range(0, 32, 2)))
        self.assertEqual(tools.select_cpus(cpus, 0.35), [0, 2, 5, 8, 11, 14, 17, 20, 23, 26, 29])
        self.assertEqual(tools.select_cpus(cpus, 0.25), list(range(0, 32, 4)))

    def test_deterministic_sorted_distinct_subset(self):
        for n in (2, 3, 4, 6, 8, 12, 16, 20, 24, 32, 64):
            cpus = list(range(n))
            for s in STEPS:
                a = tools.select_cpus(cpus, s / 100)
                self.assertEqual(a, tools.select_cpus(cpus, s / 100))
                self.assertEqual(a, sorted(set(a)))
                self.assertTrue(set(a) <= set(cpus))
                self.assertEqual(len(a), tools.throttle_core_count(n, s / 100))
                self.assertGreaterEqual(len(a), min(n, 2))

    def test_four_cores_keep_at_least_two(self):
        self.assertEqual(tools.select_cpus([0, 1, 2, 3], 0.25), [0, 2])
        self.assertEqual(tools.select_cpus([0, 1, 2, 3], 0.75), [0, 1, 2])

    def test_non_contiguous_core_list(self):
        self.assertEqual(tools.select_cpus([0, 2, 4, 6, 8, 10, 12, 14], 0.5), [0, 4, 8, 12])

    def test_available_cpus_and_unsupported_cases(self):
        cpus = tools.available_cpus()
        if cpus is not None:
            self.assertEqual(cpus, sorted(cpus))
            self.assertGreaterEqual(len(cpus), 1)
        if sys.platform == "win32":
            with mock.patch.object(tools.os, "cpu_count", lambda: 128):
                self.assertIsNone(tools.available_cpus())  # > 64 logische Kerne: nicht unterstützt

    def test_set_affinity_never_raises(self):
        p = mock.Mock()
        p.poll.return_value = None
        p._handle = "kein Handle"
        self.assertFalse(tools.set_process_affinity(p, [0, 1]))
        self.assertFalse(tools.set_process_affinity(p, []))
        p.poll.return_value = 0
        self.assertFalse(tools.set_process_affinity(p, [0]))


# --------------------------------------------------------------------------- Anzeige, Protokoll, Zusammenfassung
class TextTests(unittest.TestCase):
    def test_status_line_with_throttle(self):
        m = M(93, (7100, 7100))
        self.assertEqual(thermal.format_status(m, True, False, None, "confirmed", "", "gedrosselt auf 24 von 32 Kernen"),
                         "CPU 93 °C · Lüfter 7100/7100 U/min · Lüfter voll · gedrosselt auf 24 von 32 Kernen")
        self.assertEqual(thermal.format_status(M(70)), "CPU 70 °C")

    def test_format_throttle(self):
        self.assertEqual(thermal.format_throttle(75, (24, 32)), "gedrosselt auf 24 von 32 Kernen")
        self.assertEqual(thermal.format_throttle(75, (24, 32), short=True), "24 von 32 Kernen")
        self.assertEqual(thermal.format_throttle(75), "gedrosselt auf 75 % der Kerne")

    def test_log_lines_per_stage_change_and_status(self):
        r = Rig()
        r.run(97, 10)
        self.assertIn("Last gedrosselt: 24 von 32 Kernen (97 °C)", r.text("warn"))
        self.assertIn("gedrosselt auf 24 von 32 Kernen", r.statuses[-1][0])
        self.assertIn("Lüfter 3000/3100 U/min", r.statuses[-1][0])
        r.run(97, 30)
        self.assertIn("Last gedrosselt: 16 von 32 Kernen (97 °C)", r.text("warn"))
        r.run(80, 60)
        r.tick(80)
        self.assertIn("Drosselung gelockert: 24 von 32 Kernen (80 °C)", r.text("info"))
        self.assertEqual(sum(1 for _l, t in r.notes if t.startswith(("Last gedrosselt", "Drosselung gelockert"))), 3)
        r.run(80, 65)
        self.assertNotIn("gedrosselt", r.statuses[-1][0])  # wieder 100 %: nichts mehr angezeigt

    def test_status_without_core_info_shows_percent(self):
        r = Rig(ctl=CtlNoCores())
        r.run(98, 10)
        self.assertIn("gedrosselt auf 75 % der Kerne", r.statuses[-1][0])
        self.assertIn("Last gedrosselt: 75 % der Kerne (98 °C)", r.text("warn"))

    def test_pause_status_unchanged(self):
        r = Rig(ctl=PauseOnly())
        r.run(98, 10)
        self.assertEqual(r.statuses[-1][0], "Pause wegen Temperatur (98 °C), weiter unter 85 °C")

    def test_throttle_and_pause_seconds_in_stats(self):
        r = Rig()
        r.run(98, 40)  # gedrosselt ab t=10
        self.assertEqual(r.c.stats.throttle_seconds, 30.0)
        r.run(98, 90)  # Pause bei t=130
        self.assertTrue(r.c.paused)
        self.assertEqual(r.c.stats.throttle_seconds, 120.0)
        r.run(98, 45)  # in der Pause: nicht als gedrosselt doppelt gezählt
        self.assertEqual(r.c.stats.throttle_seconds, 120.0)
        r.run(60, 15)  # Pause endet bei t=190
        self.assertFalse(r.c.paused)
        self.assertEqual(r.c.stats.pause_seconds, 60.0)
        r.run(60, 10)
        self.assertEqual(r.c.stats.throttle_seconds, 130.0)

    def test_summary_text_and_completion_window(self):
        s = converter.RunSummary(throttle_steps=3, throttle_seconds=252, pauses=1, pause_seconds=60, max_cpu_c=98.0)
        s.seconds = 1000
        txt = s.text()
        self.assertIn("Last gedrosselt: gesamt 4:12 (3 Stufenwechsel)", txt)
        self.assertIn("Temperaturpausen: 1, gesamt", txt)
        text = gui.completion_message(s)[2]
        self.assertIn("Last gedrosselt: gesamt 4 min 12 s", text)
        self.assertIn("Temperaturpausen: 1, gesamt", text)
        s2 = converter.RunSummary(max_cpu_c=70.0)
        self.assertNotIn("gedrosselt", s2.text())
        self.assertNotIn("gedrosselt", gui.completion_message(s2)[2])

    def test_hint_and_help_texts_describe_the_new_behaviour(self):
        self.assertIn("bis zum Ende voll", gui.HINT_THERMAL)
        self.assertIn("zuerst die Last gedrosselt", gui.HINT_THERMAL)
        from xdroot_converter import cli

        helptext = cli.build_parser().format_help() if hasattr(cli, "build_parser") else ""
        if helptext:
            self.assertNotIn("zurück 10", helptext)
            self.assertIn("bleibt bis zum Laufende voll", helptext)


# --------------------------------------------------------------------------- Echter Windows-Test (ohne Last)
@unittest.skipUnless(sys.platform == "win32", "Affinität über die Windows-API")
class RealAffinityTests(unittest.TestCase):
    def test_set_half_read_back_restore_all(self):
        cpus = tools.available_cpus()
        if not cpus or len(cpus) < 2:
            self.skipTest("keine Affinität (zu wenige Kerne oder mehrere Prozessorgruppen)")
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(5)"], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=tools.CREATE_NO_WINDOW)
        try:
            self.assertEqual(tools.get_process_cpus(proc), cpus)  # Kind erbt die Maske des eigenen Prozesses
            half = tools.select_cpus(cpus, 0.5)
            self.assertLess(len(half), len(cpus))
            self.assertTrue(tools.set_process_affinity(proc, half))
            self.assertEqual(tools.get_process_cpus(proc), half)
            self.assertTrue(tools.set_process_affinity(proc, cpus))
            self.assertEqual(tools.get_process_cpus(proc), cpus)
        finally:
            proc.kill()
            proc.wait(timeout=10)
        self.assertFalse(tools.set_process_affinity(proc, cpus))  # beendeter Prozess: False, keine Ausnahme


# --------------------------------------------------------------------------- Fix-Runde nach dem Review
class FlakyCtl(Ctl):
    """set_throttle scheitert die ersten ``fail_n`` Aufrufe (z. B. Prozess gerade beendet, Fehler 5)."""

    def __init__(self, fail_n, **kw):
        super().__init__(**kw)
        self.fail_n = fail_n

    def set_throttle(self, fraction):
        self.calls.append(fraction)
        if self.fail_n > 0:
            self.fail_n -= 1
            return False
        return True


class FailureHandlingTests(unittest.TestCase):
    """G1: gescheiterte Drosselschritte."""

    def test_single_failure_is_retried_with_next_measurement_without_pause(self):
        r = Rig(ctl=FlakyCtl(1))
        r.run(98, 10)  # t=10: Schritt scheitert, KEINE Pause in dieser Messung
        self.assertFalse(r.c.paused)
        self.assertEqual(r.ctl.calls, [0.75])
        self.assertEqual(r.statuses[-1][0].count("gedrosselt"), 0)
        r.tick(98)  # t=15: erneuter Versuch klappt
        self.assertEqual(r.ctl.calls, [0.75, 0.75])
        self.assertFalse(r.c.paused)
        self.assertIn("gedrosselt auf 24 von 32 Kernen", r.statuses[-1][0])
        self.assertNotIn("nicht drosseln", r.text())

    def test_failure_counter_resets_after_success(self):
        r = Rig(ctl=FlakyCtl(2))
        r.run(98, 15)  # Fehler, Fehler, Erfolg (t=10, 15, 20?) -> noch nicht 3 in Folge
        r.tick(98)
        self.assertEqual(r.ctl.calls[:3], [0.75, 0.75, 0.75])
        self.assertFalse(r.c.paused)
        self.assertNotIn("nicht drosseln", r.text())

    def test_emergency_pause_still_works_while_step_fails(self):
        r = Rig(ctl=FlakyCtl(5))
        r.run(103, 10)
        self.assertTrue(r.c.paused)

    def test_finish_and_failure_lift_unconditionally(self):
        r = Rig()
        r.run(70, 20)
        r.c.finish()
        self.assertEqual(r.ctl.calls, [1.0])  # auch bei Stufe 0 des Reglers
        r = Rig()
        r.run(70, 10)
        r.fail(thermal.ProviderError("weg"))
        self.assertEqual(r.ctl.calls, [1.0])

    def test_unsupported_platform_switches_off_at_once(self):
        class NoSupport(Ctl):
            def set_throttle(self, fraction):
                self.calls.append(fraction)
                return False

            def throttle_cores(self, fraction):
                return None

        r = Rig(ctl=NoSupport())
        r.run(98, 10)
        self.assertTrue(r.c.paused)  # sofort altes Pausenverhalten
        self.assertEqual(r.ctl.calls, [0.75, 1.0, 1.0])  # (Aufheben scheitert in dieser Attrappe ebenfalls: einmal wiederholt)
        self.assertIn("nicht bei allen Prozessen aufgehoben", r.text("warn"))

    def test_token_total_failure_leaves_no_stage_behind(self):
        clk = [0.0]
        tok = tools.CancelToken(clock=lambda: clk[0])
        p1 = mock.Mock()
        p1.poll.return_value = None
        applied = []
        with mock.patch.object(tools, "available_cpus", lambda: list(range(32))), \
                mock.patch.object(tools, "set_process_affinity", lambda p, c: False):
            tok.register(p1)
            clk[0] += 5
            self.assertFalse(tok.set_throttle(0.75))
            self.assertEqual(tok.throttle, 1.0)  # Stufe zurück
        with mock.patch.object(tools, "available_cpus", lambda: list(range(32))), \
                mock.patch.object(tools, "set_process_affinity", lambda p, c: applied.append((p, c)) or True):
            p2 = mock.Mock()
            p2.poll.return_value = None
            tok.register(p2)  # neuer Prozess bekommt die Stufe NICHT
            clk[0] += 5
            self.assertTrue(tok.apply_throttle())
            self.assertEqual(applied, [])
            self.assertTrue(tok.set_throttle(1.0))

    def test_token_lift_after_failed_step_returns_to_full(self):
        clk = [0.0]
        tok = tools.CancelToken(clock=lambda: clk[0])
        p1 = mock.Mock()
        p1.poll.return_value = None
        with mock.patch.object(tools, "available_cpus", lambda: list(range(32))), \
                mock.patch.object(tools, "set_process_affinity", lambda p, c: True):
            tok.register(p1)
            clk[0] += 5
            self.assertTrue(tok.set_throttle(0.5))
        with mock.patch.object(tools, "set_process_affinity", lambda p, c: False):
            self.assertFalse(tok.set_throttle(1.0))  # Aufheben scheitert: 100 % gilt trotzdem für neue Prozesse
        self.assertEqual(tok.throttle, 1.0)

    def test_just_finished_process_is_not_a_failure(self):
        clk = [0.0]
        tok = tools.CancelToken(clock=lambda: clk[0])
        p1 = mock.Mock()
        p1.poll.return_value = None

        def dying(p, c):
            p.poll.return_value = 0  # Prozess endet genau jetzt
            return False

        with mock.patch.object(tools, "available_cpus", lambda: list(range(32))), \
                mock.patch.object(tools, "set_process_affinity", dying):
            tok.register(p1)
            clk[0] += 5
            self.assertTrue(tok.set_throttle(0.5))
            self.assertEqual(tok.throttle, 0.5)  # Stufe gemerkt

    def test_available_cpus_is_cached_per_token(self):
        n = []
        with mock.patch.object(tools, "available_cpus", lambda: n.append(1) or list(range(8))):
            tok = tools.CancelToken()
            for _ in range(5):
                tok.set_throttle(0.5)
                tok.throttle_cores(0.5)
            tok.register(mock.Mock())
        self.assertEqual(len(n), 1)


class ResumeAndLockTests(unittest.TestCase):
    def test_after_pause_continue_one_stage_lower_and_restart_lock(self):
        r = Rig()
        r.run(98, 10)  # 75 %
        r.run(103, 10)  # Notfall-Pause
        self.assertTrue(r.c.paused)
        r.run(60, 15)  # Fortsetzen -> eine Stufe tiefer (50 %)
        self.assertFalse(r.c.paused)
        self.assertEqual(r.ctl.calls, [0.75, 0.5])
        self.assertIn("Last gedrosselt: 16 von 32 Kernen", r.text("warn"))
        r.run(98, 20)  # Sperre ab dem Fortsetzen: noch keine weitere Stufe
        self.assertEqual(r.ctl.calls, [0.75, 0.5])
        r.run(98, 15)
        self.assertEqual(r.ctl.calls, [0.75, 0.5, 0.35])

    def test_after_pause_on_lowest_stage_only_restarts_lock(self):
        r = Rig()
        r.run(98, 130)
        self.assertTrue(r.c.paused)
        r.run(60, 15)
        self.assertEqual(r.ctl.calls, [0.75, 0.5, 0.35, 0.25])
        # keine tiefere Stufe mehr -> KEINE Sperre nach der Pause: bei erneut 2 Messungen an der Schwelle sofort wieder pausieren
        r.tick(98)
        self.assertFalse(r.c.paused)  # (1 Messung)
        r.tick(98)
        self.assertTrue(r.c.paused)
        self.assertEqual(r.c.stats.pauses, 2)

    def test_lock_applies_only_after_down_steps(self):
        r = Rig()
        r.run(98, 10)  # t=10: 75 %
        r.run(80, 65)  # t=75: 100 %
        self.assertEqual(r.ctl.calls, [0.75, 1.0])
        r.run(98, 10)  # t=85: 2 Messungen an der Schwelle -> SOFORT wieder runter (keine 30-s-Sperre nach dem Hochschalten)
        self.assertEqual(r.ctl.calls, [0.75, 1.0, 0.75])
        self.assertIn("nicht mehr versucht", r.text("info"))  # Pendel-Sperre gilt weiter

    def test_pause_status_shows_throttle(self):
        r = Rig()
        r.run(98, 10)
        r.run(103, 10)
        self.assertTrue(r.c.paused)
        self.assertEqual(r.statuses[-1][0],
                         "Pause wegen Temperatur (103 °C), weiter unter 85 °C · gedrosselt auf 24 von 32 Kernen")


# --------------------------------------------------------------------------- Zweite Fix-Runde
class StepFailsCtl(Ctl):
    """apply_throttle meldet immer True ("nichts zu tun"), aber set_throttle(0.5) scheitert (nach erfolgreichem 0.75)."""

    last_apply = (0, 0)

    def apply_throttle(self):
        return True

    def set_throttle(self, fraction):
        self.calls.append(fraction)
        return fraction != 0.5


class NoPersistentFailureTests(unittest.TestCase):
    """G-A: dauerhaft scheiternde Schritte nach unten dürfen nicht vom Nachzieh-Aufruf maskiert werden."""

    def test_persistent_step_failure_disables_and_pauses_within_three_measurements(self):
        r = Rig(ctl=StepFailsCtl())
        r.run(98, 10)  # t=10: 75 % klappt
        self.assertEqual(r.ctl.calls, [0.75])
        r.run(98, 25)  # t=15..35: Sperre
        self.assertFalse(r.c.paused)
        r.tick(98)  # t=40: Fehlschlag 1 (kein Zurücksetzen durch apply_throttle)
        r.tick(98)  # Fehlschlag 2
        self.assertFalse(r.c.paused)
        r.tick(98)  # Fehlschlag 3: abschalten UND in derselben Messung pausieren
        self.assertTrue(r.c.paused)
        self.assertEqual(r.ctl.calls, [0.75, 0.5, 0.5, 0.5, 1.0])
        self.assertEqual(r.text("warn").count("nicht drosseln"), 1)

    def test_same_with_real_token_and_mocked_affinity(self):
        clk = [100.0]
        tok = tools.CancelToken(clock=lambda: clk[0])
        p = mock.Mock()
        p.poll.return_value = None
        state = {"ok": 1}  # die erste Affinitätsänderung klappt, danach scheitert jede (Prozess läuft noch)

        def aff(proc, cpus):
            if state["ok"] > 0:
                state["ok"] -= 1
                return True
            return False

        with mock.patch.object(tools, "available_cpus", lambda: list(range(32))), \
                mock.patch.object(tools, "set_process_affinity", aff), \
                mock.patch.object(tools, "suspend_process", lambda q: True), \
                mock.patch.object(tools, "resume_process", lambda q: True):
            tok.register(p)
            clk[0] += 5
            r = Rig(ctl=tok)
            r.run(98, 10)
            self.assertEqual(tok.throttle, 0.75)
            r.run(98, 25)
            self.assertFalse(r.c.paused)
            r.run(98, 10)  # t=40, 45: zwei Fehlschläge (Token bleibt auf 0.75)
            self.assertEqual(tok.throttle, 0.75)
            self.assertFalse(r.c.paused)
            r.tick(98)  # dritter Fehlschlag: aus + Pause in derselben Messung
            self.assertTrue(r.c.paused)
            self.assertEqual(tok.throttle, 1.0)
            self.assertIn("nicht bei allen Prozessen aufgehoben", r.text("warn"))  # Aufheben scheiterte (Mock: nur 1 Erfolg)
            # M-2: der Rest wird mit den nächsten Messungen aufgehoben, sobald es wieder geht
            self.assertEqual(tok.throttled_count(), 1)
            state["ok"] = 1
            r.tick(98)
            self.assertEqual(tok.throttled_count(), 0)


class ResidualLiftTests(unittest.TestCase):
    def test_partial_lift_is_retried_at_stage_zero(self):
        clk = [100.0]
        tok = tools.CancelToken(clock=lambda: clk[0])
        p1, p2 = mock.Mock(), mock.Mock()
        p1.poll.return_value = p2.poll.return_value = None
        with mock.patch.object(tools, "available_cpus", lambda: list(range(32))), \
                mock.patch.object(tools, "set_process_affinity", lambda p, c: True):
            tok.register(p1)
            tok.register(p2)
            clk[0] += 5
            self.assertTrue(tok.set_throttle(0.5))
        self.assertEqual(tok.throttled_count(), 2)
        with mock.patch.object(tools, "available_cpus", lambda: list(range(32))), \
                mock.patch.object(tools, "set_process_affinity", lambda p, c: p is p1):
            self.assertTrue(tok.set_throttle(1.0))  # teilweise gescheitert: p2 bleibt gedrosselt
            self.assertEqual(tok.throttled_count(), 1)
            r = Rig(ctl=tok)  # Regler bei Stufe 0
            self.assertEqual(r.c._level, 0)
        with mock.patch.object(tools, "available_cpus", lambda: list(range(32))), \
                mock.patch.object(tools, "set_process_affinity", lambda p, c: True):
            r.tick(70)  # Stufe 0, aber noch ein gedrosselter Prozess: wird aufgehoben
            self.assertEqual(tok.throttled_count(), 0)
        n = tok.throttled_count()
        r.tick(70)
        self.assertEqual(tok.throttled_count(), n)

    def test_failed_lift_to_full_keeps_display_and_token_consistent(self):
        class UpFails(Ctl):
            def __init__(self):
                super().__init__()
                self.fail_full = 1

            def set_throttle(self, fraction):
                self.calls.append(fraction)
                if fraction >= 1.0 and self.fail_full > 0:
                    self.fail_full -= 1
                    return False
                return True

        r = Rig(ctl=UpFails())
        r.run(98, 10)
        self.assertIn("gedrosselt", r.statuses[-1][0])
        r.run(80, 65)  # Hochschalten auf 100 % scheitert einmal
        self.assertNotIn("gedrosselt", r.statuses[-1][0])  # kein "gedrosselt", wenn der Token schon auf 1.0 steht
        self.assertEqual(r.c._level, 0)


class PartialLiftCtl(Ctl):
    """set_throttle(1.0) liefert True (Teilerfolg), aber ``throttled_count`` meldet danach noch gedrosselte Prozesse."""

    def __init__(self, residuals):
        super().__init__()
        self.res_seq = list(residuals)  # Rest nach dem 1., 2., ... Aufheben
        self.residual = 0

    def set_throttle(self, fraction):
        self.calls.append(fraction)
        if fraction >= 1.0:
            self.residual = self.res_seq.pop(0) if self.res_seq else 0
        return True

    def throttled_count(self):
        return self.residual


class PartialLiftTests(unittest.TestCase):
    def test_partial_lift_repeated_once_then_warns(self):
        r = Rig(ctl=PartialLiftCtl([1, 1]))
        r.run(98, 10)
        r.c.finish()
        self.assertEqual(r.ctl.calls, [0.75, 1.0, 1.0])
        self.assertIn("nicht bei allen Prozessen aufgehoben", r.text("warn"))

    def test_partial_lift_repeat_succeeds_without_warning(self):
        r = Rig(ctl=PartialLiftCtl([1, 0]))
        r.run(98, 10)
        r.c.finish()
        self.assertEqual(r.ctl.calls, [0.75, 1.0, 1.0])
        self.assertNotIn("nicht bei allen Prozessen aufgehoben", r.text("warn"))


class PauseDuringApplyTests(unittest.TestCase):
    def test_no_throttle_in_the_middle_of_a_pause(self):
        clk = [100.0]
        applied = []
        tok = tools.CancelToken(clock=lambda: clk[0])
        p = mock.Mock()
        p.poll.return_value = None
        with mock.patch.object(tools, "available_cpus", lambda: list(range(32))), \
                mock.patch.object(tools, "set_process_affinity", lambda q, c: applied.append(q) or True), \
                mock.patch.object(tools, "suspend_process", lambda q: True), \
                mock.patch.object(tools, "resume_process", lambda q: True):
            tok.register(p)
            clk[0] += 1.0  # 1 s gelaufen
            tok.set_throttle(0.5)
            self.assertTrue(tok.pause())
            for _ in range(6):  # lange Pause mit mehreren Nachzieh-Aufrufen (Regler-Takt)
                clk[0] += 10.0
                self.assertTrue(tok.apply_throttle())
            self.assertEqual(applied, [])
            tok.resume()
            tok.apply_throttle()
            self.assertEqual(applied, [])  # erst nach insgesamt 3 s Laufzeit
            clk[0] += 1.9
            tok.apply_throttle()
            self.assertEqual(applied, [])
            clk[0] += 0.1
            tok.apply_throttle()
            self.assertEqual(applied, [p])

    def test_lifting_is_allowed_for_suspended_processes(self):
        clk = [100.0]
        applied = []
        tok = tools.CancelToken(clock=lambda: clk[0])
        p = mock.Mock()
        p.poll.return_value = None
        with mock.patch.object(tools, "available_cpus", lambda: list(range(32))), \
                mock.patch.object(tools, "set_process_affinity", lambda q, c: applied.append(len(c)) or True), \
                mock.patch.object(tools, "suspend_process", lambda q: True), \
                mock.patch.object(tools, "resume_process", lambda q: True):
            tok.register(p)
            clk[0] += 10.0
            tok.set_throttle(0.5)
            tok.pause()
            tok.set_throttle(1.0)
            self.assertEqual(applied, [16, 32])

    def test_exception_while_applying_restores_previous_stage(self):
        tok = tools.CancelToken(clock=lambda: 100.0)
        p = mock.Mock()
        p.poll.return_value = None

        def boom(q, c):
            raise RuntimeError("x")

        with mock.patch.object(tools, "available_cpus", lambda: list(range(32))), \
                mock.patch.object(tools, "set_process_affinity", boom):
            tok.register(p)
            tok._reg_t[p] = 0.0
            self.assertFalse(tok.set_throttle(0.5))
            self.assertEqual(tok.throttle, 1.0)


class StartDelayRuntimeTests(unittest.TestCase):
    """M-1: Die Startverzögerung zählt nur Laufzeit, keine Pausenzeit."""

    def setUp(self):
        self.applied = []
        for p in (mock.patch.object(tools, "available_cpus", lambda: list(range(32))),
                  mock.patch.object(tools, "set_process_affinity", lambda p, c: self.applied.append(p) or True),
                  mock.patch.object(tools, "suspend_process", lambda p: True),
                  mock.patch.object(tools, "resume_process", lambda p: True)):
            p.start()
            self.addCleanup(p.stop)
        self.clk = [100.0]
        self.tok = tools.CancelToken(clock=lambda: self.clk[0])

    @staticmethod
    def proc():
        p = mock.Mock()
        p.poll.return_value = None
        return p

    def test_pause_time_does_not_count_towards_the_delay(self):
        tok = self.tok
        p = self.proc()
        tok.register(p)
        self.clk[0] += 1.0  # nur 1 s gelaufen
        tok.set_throttle(0.5)
        self.assertTrue(tok.pause())
        self.clk[0] += 60.0  # lange Pause
        self.assertTrue(tok.resume())
        tok.apply_throttle()
        self.assertEqual(self.applied, [])  # direkt nach dem Fortsetzen nicht gedrosselt
        self.clk[0] += 1.9
        tok.apply_throttle()
        self.assertEqual(self.applied, [])  # erst 1 + 1.9 s Laufzeit
        self.clk[0] += 0.1
        tok.apply_throttle()
        self.assertEqual(self.applied, [p])

    def test_process_that_already_ran_long_enough_is_throttled_right_after_resume(self):
        tok = self.tok
        p = self.proc()
        tok.register(p)
        tok.set_throttle(0.5)  # Prozess noch jung: wartet
        self.clk[0] += 2.0
        self.assertTrue(tok.pause())  # nach 2 s Laufzeit angehalten
        self.clk[0] += 30.0
        tok.resume()
        tok.apply_throttle()
        self.assertEqual(self.applied, [])  # erst 2 s Laufzeit
        self.clk[0] += 1.0
        tok.apply_throttle()
        self.assertEqual(self.applied, [p])

    def test_process_started_during_pause_counts_from_resume(self):
        tok = self.tok
        p1 = self.proc()
        tok.register(p1)
        self.clk[0] += 10.0
        tok.set_throttle(0.5)  # p1 hat genug Laufzeit: sofort gedrosselt
        self.assertEqual(self.applied, [p1])
        tok.pause()
        p2 = self.proc()
        tok.register(p2)  # während der Pause gestartet (und angehalten)
        self.clk[0] += 50.0
        tok.resume()
        tok.apply_throttle()
        self.assertEqual(self.applied, [p1])  # p2 hat noch keine Laufzeit
        self.clk[0] += 3.0
        tok.apply_throttle()
        self.assertEqual(self.applied, [p1, p2])


# --------------------------------------------------------------------------- Lauf-Integrationstest mit Drosselung
try:  # unter "discover -s tests" ist tests/ der Suchpfad, sonst das Paket "tests"
    from test_run import RunTestBase, Rep
    from test_thermal import FakeProvider, wait_for
except ImportError:  # pragma: no cover
    from tests.test_run import RunTestBase, Rep
    from tests.test_thermal import FakeProvider, wait_for


class ThrottleRunTests(RunTestBase):
    def test_second_clip_process_inherits_the_stage(self):
        applied = []
        procs = {}
        seen = {}
        prov = FakeProvider([99])  # dauerhaft heiß (>= 97 °C, aber unter dem Notfallwert 102)
        rep = Rep()
        conv = converter.Converter(converter.Options(
            source=str(self.src), target=str(self.tgt), thermal="auto", thermal_interval_s=0.01,
            thermal_provider=lambda: prov, thermal_throttle=True), rep)
        clk = [1000.0]  # eingefrorene Uhr des Tokens: die Startverzögerung (3 s Laufzeit) läuft nur, wenn der Test sie vorstellt
        conv.cancel = tools.CancelToken(clock=lambda: clk[0])
        fake = converter.Converter._transcode

        def tr(c, plan, fps, frames, v_path, wavs):
            p = mock.Mock()
            p.poll.return_value = None
            procs[plan.name] = p
            c.cancel.register(p)
            if plan.name == "A.MXF":  # warten, bis der Regler gedrosselt hat (echter CancelToken, gemockte Affinität)
                seen["a"] = wait_for(lambda: c.cancel.throttle < 1.0, 10)
            n0 = prov.i
            wait_for(lambda: prov.i >= n0 + 3, 10)  # mindestens 3 Regler-Messungen bei eingefrorener Uhr
            seen[plan.name + "_young"] = not any(q is p for q, _c in applied)
            clk[0] += tools.THROTTLE_START_DELAY_S  # 3 s Laufzeit vergangen
            seen[plan.name] = wait_for(lambda: any(q is p for q, _c in applied), 10)
            c.cancel.unregister(p)
            return fake(c, plan, fps, frames, v_path, wavs)

        with mock.patch.object(tools, "available_cpus", lambda: list(range(32))), \
                mock.patch.object(tools, "set_process_affinity", lambda p, c: applied.append((p, list(c))) or True), \
                mock.patch.object(converter.Converter, "_transcode", tr):
            s = conv.run()
        self.assertTrue(seen.get("a") and seen.get("A.MXF") and seen.get("B.MXF"))
        self.assertTrue(seen.get("A.MXF_young") and seen.get("B.MXF_young"),
                        "ein neuer Prozess darf in den ersten 3 s noch nicht gedrosselt sein")
        self.assertEqual((s.count(converter.ST_CONVERTED), s.failed, s.pauses), (2, 0, 0))
        by_proc = {}
        for p, c in applied:
            by_proc.setdefault(id(p), []).append(len(c))
        self.assertEqual(by_proc[id(procs["A.MXF"])][0], 24)  # laufender Prozess beim Stufenwechsel (nach der Verzögerung)
        self.assertEqual(by_proc[id(procs["B.MXF"])][0], 24)  # nächster Clip bekommt die Stufe nach der Startverzögerung
        self.assertGreaterEqual(s.throttle_steps, 1)
        self.assertIn("Last gedrosselt: gesamt", s.text())
        self.assertIn("Last gedrosselt: 24 von 32 Kernen", rep.text("warn"))


if __name__ == "__main__":
    unittest.main()
