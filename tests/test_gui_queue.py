"""GUI-Tests der Auftragsliste mit echtem Tk (ohne Display übersprungen) und Attrappen-Converter.

Es laufen keine echten Konvertierungen und keine externen Programme: ``tools.check_tools`` ist ersetzt, die Quellen sind
leere Ordner mit MEDIAPRO.XML und Clip/, der Converter ist eine Attrappe, die Übersichts-Analyse ist abgeschaltet."""

import contextlib
import tempfile
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

from xdroot_converter import converter, gui, jobqueue as jq


def _ok_summary(name="C1"):
    s = converter.RunSummary()
    s.results = [converter.ClipResult(name, converter.ST_CONVERTED, src_size=1000, dst_size=400)]
    s.seconds = 3.0
    return s


class FakeFactory:
    """Attrappe für ``converter.Converter``: Verhalten je Quellordner-Name (ok | fatal | raise | block)."""

    def __init__(self, behaviors=None, events=None):
        self.behaviors = behaviors or {}
        self.events = events if events is not None else []
        self.started = threading.Event()
        self.gate = threading.Event()
        self.gate.set()
        self.opts_seen = []
        self.tokens = []
        self.after_job = None  # Rückruf nach jedem Auftrag (Name) - z. B. Nutzer-Abbruch zwischen zwei Aufträgen

    def __call__(self, opts, reporter=None, cancel=None):
        fac = self
        fac.tokens.append(cancel)
        if self.behaviors.get(Path(opts.source).name) == "ctor_raise":
            raise RuntimeError("Konstruktor kaputt")

        class Conv:
            def run(self_inner):
                name = Path(opts.source).name
                fac.events.append(("job", name))
                fac.opts_seen.append(opts)
                beh = fac.behaviors.get(name, "ok")
                if beh == "raise":
                    raise RuntimeError("Attrappe kaputt")
                if beh in ("thermal", "selfcancel"):  # wie Converter._thermal_abort: bricht den EIGENEN Token ab
                    cancel.cancel()
                    s = converter.RunSummary()
                    s.cancelled = True
                    if beh == "thermal":
                        s.abort_reason = "CPU kühlt nicht ab."
                    return s
                if beh == "fatal":
                    s = converter.RunSummary()
                    s.fatal = "Platte voll"
                    return s
                if beh == "block":  # wartet, bis abgebrochen wird
                    fac.started.set()
                    t0 = time.time()
                    while not cancel.cancelled and time.time() - t0 < 10:
                        time.sleep(0.01)
                    s = _ok_summary()
                    s.cancelled = cancel.cancelled
                    return s
                fac.started.set()
                fac.gate.wait(10)
                if fac.after_job:
                    fac.after_job(name)
                return _ok_summary(name)

        return Conv()


class GuiQueueBase(unittest.TestCase):
    def setUp(self):
        try:
            self.app = gui.App()
        except Exception as e:  # noqa: BLE001  (kein Display)
            self.skipTest(f"keine GUI möglich: {e}")
        app = self.app
        orig = app.tk.call("tk", "scaling")
        self.addCleanup(self._cleanup, orig)
        app.attributes("-alpha", 0.0)
        app._refresh_overview = lambda: None  # keine Übersichts-Analyse (kein ffprobe)
        app._analyze = lambda opts: 1000  # Schätzung im Hintergrund (keine echte Analyse)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.factory = FakeFactory()
        app._converter_factory = self.factory
        self.mb = {n: mock.MagicMock(return_value=True) for n in ("showinfo", "showwarning", "showerror", "askyesno")}
        for n, m in self.mb.items():
            p = mock.patch.object(gui.messagebox, n, m)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(gui.tools, "check_tools", lambda: None)
        p.start()
        self.addCleanup(p.stop)

    def _cleanup(self, orig):
        app = self.app
        try:
            app.tk.call("tk", "scaling", orig)  # prozessweit: zurückstellen
        except Exception:  # noqa: BLE001
            pass
        try:
            app.cancel_token and app.cancel_token.cancel()
            if app.worker is not None:
                app.worker.join(5)
            app.after_cancel(app._after_id)
        except Exception:  # noqa: BLE001
            pass
        try:
            app.destroy()
        except Exception:  # noqa: BLE001
            pass

    # ---- Hilfen
    def card(self, name):
        d = self.root / "src" / name
        (d / "Clip").mkdir(parents=True, exist_ok=True)
        (d / "MEDIAPRO.XML").write_text("x")
        return str(d)

    def add(self, name, dst=None, bitrate="50", est=None):
        a = self.app
        src = self.card(name)
        a.var_src.set(src)
        a.var_dst.set(dst or str(self.root / "dst" / name))
        a.var_bitrate.set(bitrate)
        if est is not None:  # Schätzung liegt schon vor (sonst wird sie im Hintergrund nachgetragen)
            a._ov_est[gui.overview_key(src, False, bitrate)] = est
        a._add_to_list()

    def pump(self, cond, timeout=10.0):
        t0 = time.time()
        while time.time() - t0 < timeout:
            self.app.update()
            if cond():
                return True
            time.sleep(0.01)
        return False

    def dialogs(self):
        return sum(self.mb[n].call_count for n in ("showinfo", "showwarning", "showerror"))

    def run_list_to_end(self):
        self.app._start_list()
        self.assertTrue(self.pump(lambda: not self.app._ui_running), "Listenlauf endet")
        self.pump(lambda: False, 0.3)  # das Abschlussfenster kommt per after(50)

    def statuses(self):
        return [j.status for j in self.app.jq.jobs()]

    def state(self, w):
        return str(w.cget("state"))


class ListHandlingTests(GuiQueueBase):
    def test_liste_anfangs_unsichtbar_dann_sichtbar(self):
        a = self.app
        a.update()
        self.assertFalse(a.frm_list.winfo_ismapped())
        self.assertEqual(self.state(a.btn_runall), "disabled")
        self.add("A")
        a.update()
        self.assertTrue(a.frm_list.winfo_ismapped())
        self.assertEqual(len(a.tree.get_children()), 1)
        self.assertEqual(self.state(a.btn_runall), "normal")
        a.tree.selection_set(a.tree.get_children()[0])
        a.update()
        self.assertEqual(self.state(a.btn_remove), "normal")
        a._remove_job()
        a.update()
        self.assertEqual(len(a.jq), 0)
        self.assertFalse(a.frm_list.winfo_ismapped())

    def test_leeren(self):
        a = self.app
        self.add("A")
        self.add("B")
        self.assertEqual(len(a.tree.get_children()), 2)
        a._clear_jobs()
        a.update()
        self.assertEqual(len(a.tree.get_children()), 0)
        self.assertEqual(len(a.jq), 0)

    def test_momentaufnahme_der_einstellungen(self):
        a = self.app
        a.var_preset.set("slow")
        self.add("A", bitrate="42")
        a.var_preset.set("fast")
        a.var_threads.set("3")
        self.add("B", bitrate="60")
        j1, j2 = a.jq.jobs()
        self.assertEqual((j1.options.bitrate_mbps, j1.options.preset, j1.options.threads), (42.0, "slow", None))
        self.assertEqual((j2.options.bitrate_mbps, j2.options.preset, j2.options.threads), (60.0, "fast", 3))
        a.var_preset.set("veryslow")  # spätere Änderung wirkt nicht auf bestehende Aufträge
        self.assertEqual(a.jq.jobs()[0].options.preset, "slow")
        self.assertIn("42 Mbit/s, slow", j1.settings_text)
        row = a.tree.item(str(j1.id), "values")
        self.assertEqual(row[3], jq.ST_WAIT)
        self.assertIn("42 Mbit/s, slow", row[2])
        self.assertTrue(self.pump(lambda: j1.est_bytes == 1000 and j2.est_bytes == 1000))  # im Hintergrund nachgetragen

    def test_ungueltige_eingaben_fuegen_nichts_hinzu(self):
        a = self.app
        a.var_src.set("")
        a.var_dst.set("")
        a._add_to_list()
        a.var_src.set(self.card("A"))
        a.var_dst.set(str(Path(self.card("A")) / "sub"))  # Ziel in der Quelle
        a._add_to_list()
        self.assertEqual(len(a.jq), 0)
        self.assertEqual(self.mb["showwarning"].call_count, 2)

    def test_duplikate_werden_abgelehnt(self):
        a = self.app
        self.add("A", dst=str(self.root / "dst" / "gemeinsam"))
        self.add("B", dst=str(self.root / "dst" / "gemeinsam"))          # gleiches Ziel
        self.assertEqual(len(a.jq), 1)
        a.var_src.set(self.card("C"))
        a.var_dst.set(self.card("A"))                                     # Ziel = Quelle von Auftrag 1 (Ordner existiert)
        a._add_to_list()
        self.assertEqual(len(a.jq), 1)
        a.var_src.set(str(self.root / "dst" / "gemeinsam"))               # Quelle = Ziel von Auftrag 1
        a.var_dst.set(str(self.root / "dst" / "anders"))
        a._add_to_list()                                                  # (Quelle ist kein XDROOT -> ebenfalls abgelehnt)
        self.assertEqual(len(a.jq), 1)
        self.assertGreaterEqual(self.mb["showwarning"].call_count, 3)
        msgs = " ".join(c.args[1] for c in self.mb["showwarning"].call_args_list)
        self.assertIn("steht schon in der Liste", msgs)

    def test_quelle_ist_ziel_eines_anderen_auftrags(self):
        a = self.app
        t = self.root / "dst" / "X"
        self.add("A", dst=str(t))
        (t / "XDROOT" / "Clip").mkdir(parents=True)
        (t / "XDROOT" / "MEDIAPRO.XML").write_text("x")
        a.var_src.set(str(t))
        a.var_dst.set(str(self.root / "dst" / "Y"))
        a._add_to_list()
        self.assertEqual(len(a.jq), 1)
        self.assertIn("Ziel eines anderen Auftrags", self.mb["showwarning"].call_args_list[-1].args[1])

    def test_platzwarnung_mit_rueckfrage(self):
        a = self.app
        self.add("A", est=1000)
        with mock.patch.object(jq, "free_bytes", lambda p: 500):
            self.mb["askyesno"].return_value = False
            self.add("B", est=1000)
            self.assertEqual(len(a.jq), 1)  # abgelehnt
            self.assertEqual(self.mb["askyesno"].call_count, 1)
            self.assertIn("geschätzte Zielgröße", self.mb["askyesno"].call_args.args[1])
            self.mb["askyesno"].return_value = True
            self.add("B", est=1000)
            self.assertEqual(len(a.jq), 2)  # trotzdem hinzugefügt (kein hartes Verbot)

    def test_rueckfrage_antwort_wird_gespeichert(self):
        a = self.app
        dst = self.root / "dst" / "R"
        (dst / "XDROOT").mkdir(parents=True)
        (dst / "XDROOT" / "MEDIAPRO.XML").write_text("x")
        self.add("A", dst=str(dst))
        self.assertEqual(self.mb["askyesno"].call_count, 1)
        self.assertEqual(a.jq.jobs()[0].answers, {"resume_existing": True})


class ListRunTests(GuiQueueBase):
    def test_reihenfolge_fehler_und_ausnahme_halten_die_liste_nicht_an(self):
        a = self.app
        for n in ("A", "B", "C", "D"):
            self.add(n)
        self.factory.behaviors = {"B": "fatal", "C": "raise"}
        self.run_list_to_end()
        self.assertEqual([e[1] for e in self.factory.events], ["A", "B", "C", "D"])
        self.assertEqual(self.statuses(), [jq.ST_DONE, jq.ST_ERROR, jq.ST_ERROR, jq.ST_DONE])
        self.assertIn("RuntimeError: Attrappe kaputt", a.jq.jobs()[2].error)
        # EIN Abschlussfenster, am Ende, mit allen Aufträgen und den Summen
        self.assertEqual(self.dialogs(), 1)
        self.assertEqual(self.mb["showwarning"].call_count, 1)
        title, text = self.mb["showwarning"].call_args.args
        self.assertIn("mit Problemen", title)
        self.assertEqual(text.count("Ergebnis:"), 4)
        self.assertIn("Platte voll", text)
        self.assertIn("Summen:", text)
        self.assertEqual(self.mb["askyesno"].call_count, 0)
        log = a.txt.get("1.0", "end")
        self.assertIn("Auftrag 1 von 4:", log)
        self.assertIn("Auftrag 4 von 4:", log)
        self.assertIn("Unerwarteter Fehler", log)

    def test_alles_gut_ein_infofenster_und_erledigte_bleiben(self):
        a = self.app
        self.add("A")
        self.add("B")
        self.run_list_to_end()
        self.assertEqual(self.dialogs(), 1)
        self.assertEqual(self.mb["showinfo"].call_count, 1)
        self.assertEqual(self.statuses(), [jq.ST_DONE, jq.ST_DONE])
        self.assertEqual(len(a.tree.get_children()), 2)  # bleiben stehen
        self.assertEqual(a.tree.item(a.tree.get_children()[0], "values")[3], jq.ST_DONE)
        self.assertEqual(self.state(a.btn_runall), "disabled")  # nichts mehr zu tun
        self.assertEqual(self.state(a.btn_start), "normal")
        # erneut: erledigte werden nicht noch einmal gestartet
        a._start_list()
        self.pump(lambda: False, 0.3)
        self.assertEqual(len(self.factory.events), 2)

    def test_abbrechen_laesst_uebrige_wartend_und_fortsetzen(self):
        a = self.app
        for n in ("A", "B", "C"):
            self.add(n)
        self.factory.behaviors = {"B": "block"}
        a._start_list()
        self.assertTrue(self.pump(lambda: self.factory.started.is_set() and ("job", "B") in self.factory.events))
        self.assertEqual(self.state(a.btn_cancel), "normal")
        a._cancel()
        self.assertTrue(self.pump(lambda: not a._ui_running))
        self.pump(lambda: False, 0.3)
        self.assertEqual([e[1] for e in self.factory.events], ["A", "B"])
        self.assertEqual(self.statuses(), [jq.ST_DONE, jq.ST_CANCELLED, jq.ST_WAIT])
        self.assertEqual(self.dialogs(), 1)
        self.assertIn("abgebrochen", self.mb["showwarning"].call_args.args[0])
        self.assertIn("Nicht gestartet: 1", self.mb["showwarning"].call_args.args[1])
        # Fortsetzen: A (erledigt) nicht erneut; B (abgebrochen) wird wieder aufgenommen, dann C
        self.factory.behaviors = {}
        self.factory.events.clear()
        self.run_list_to_end()
        self.assertEqual([e[1] for e in self.factory.events], ["B", "C"])
        self.assertEqual(self.statuses(), [jq.ST_DONE] * 3)
        self.assertEqual(self.dialogs(), 2)

    def test_sperren_waehrend_des_laufs_und_hinzufuegen_am_ende_mitgemacht(self):
        a = self.app
        self.add("A")
        self.add("B")
        self.factory.gate.clear()
        a._start_list()
        self.assertTrue(self.pump(lambda: self.factory.started.is_set()))
        self.assertEqual(self.state(a.btn_start), "disabled")
        self.assertEqual(self.state(a.btn_runall), "disabled")
        self.assertEqual(self.state(a.btn_add), "normal")
        # Quelle, Ziel, Durchsuchen und Einstellungen bleiben im Lauf bedienbar (echte Widget-Zustände, nicht nur StringVars)
        locked = [str(w.cget("state")) for w, _normal in a._locked_widgets]
        self.assertTrue(locked)
        self.assertNotIn("disabled", locked)
        self.assertEqual(self.state(a._temp_fields[0]), "normal")
        self.assertEqual(self.state(a._throttle_fields[0]), "normal")
        self.assertEqual(self.state(a._verify_fields[0]), "normal")
        self.assertEqual(self.state(a.btn_cancel), "normal")
        self.assertTrue(a.frm_list.winfo_ismapped())
        self.assertTrue(self.pump(lambda: a.var_status.get() == "Läuft … Auftrag 1 von 2"), a.var_status.get())
        # laufender Auftrag: nicht entfernbar; wartender: entfernbar-fähig
        run = a.jq.running_job()
        a.tree.selection_set(str(run.id))
        a.update()
        self.assertEqual(self.state(a.btn_remove), "disabled")
        a._remove_job()
        self.assertEqual(len(a.jq), 2)
        a._start()  # gesperrt: kein zweiter Lauf
        a._start_list()
        # während des Laufs hinzufügen
        self.add("C")
        self.assertEqual(len(a.jq), 3)
        self.factory.gate.set()
        self.assertTrue(self.pump(lambda: not a._ui_running))
        self.pump(lambda: False, 0.3)
        self.assertEqual([e[1] for e in self.factory.events], ["A", "B", "C"])
        self.assertEqual(self.dialogs(), 1)
        self.assertEqual(self.mb["askyesno"].call_count, 0)

    def test_energiesparmodus_umschliesst_den_ganzen_listenlauf(self):
        a = self.app
        self.add("A")
        self.add("B")
        events = self.factory.events

        @contextmanager
        def spy(log=None):
            events.append(("awake", "enter"))
            try:
                yield True
            finally:
                events.append(("awake", "exit"))

        with mock.patch.object(gui.keepawake, "keep_awake", spy):
            self.run_list_to_end()
        self.assertEqual(events, [("awake", "enter"), ("job", "A"), ("job", "B"), ("awake", "exit")])

    def test_einzellauf_unveraendert_ein_abschlussfenster(self):
        a = self.app
        a.var_src.set(self.card("S"))
        a.var_dst.set(str(self.root / "dst" / "S"))
        a._start()
        self.assertTrue(self.pump(lambda: not a._ui_running))
        self.pump(lambda: False, 0.3)
        self.assertEqual([e[1] for e in self.factory.events], ["S"])
        self.assertEqual(self.dialogs(), 1)
        self.assertEqual(self.mb["showinfo"].call_count, 1)
        self.assertEqual(len(a.jq), 0)
        self.assertFalse(a.frm_list.winfo_ismapped())
        self.assertEqual(self.state(a.btn_start), "normal")

    def test_optionen_des_auftrags_erreichen_den_converter(self):
        a = self.app
        a.var_preset.set("slow")
        self.add("A", bitrate="45")
        a.var_preset.set("fast")
        self.run_list_to_end()
        o = self.factory.opts_seen[0]
        self.assertEqual((o.bitrate_mbps, o.preset), (45.0, "slow"))


class ListRunRobustnessTests(GuiQueueBase):
    def test_hitze_abbruch_beendet_nur_den_auftrag(self):
        a = self.app
        for n in ("A", "B", "C"):
            self.add(n)
        self.factory.behaviors = {"A": "thermal"}
        self.run_list_to_end()
        self.assertEqual([e[1] for e in self.factory.events], ["A", "B", "C"])
        self.assertEqual(self.statuses(), [jq.ST_ERROR, jq.ST_DONE, jq.ST_DONE])
        t = self.factory.tokens
        self.assertEqual(len(set(map(id, t))), 3)  # je Auftrag ein eigener, frischer Token
        self.assertTrue(t[0].cancelled)
        self.assertFalse(t[1].cancelled or t[2].cancelled)
        self.assertEqual(self.dialogs(), 1)
        self.assertIn("mit Problemen", self.mb["showwarning"].call_args.args[0])

    def test_anderer_selbst_ausgeloester_abbruch_ist_fehler(self):
        a = self.app
        for n in ("A", "B"):
            self.add(n)
        self.factory.behaviors = {"A": "selfcancel"}
        self.run_list_to_end()
        self.assertEqual(self.statuses(), [jq.ST_ERROR, jq.ST_DONE])

    def test_abbruch_vor_dem_ersten_auftrag(self):
        a = self.app
        for n in ("A", "B", "C"):
            self.add(n)

        @contextmanager
        def spy(log=None):
            a._request_cancel()  # Nutzer bricht ab, bevor der erste Auftrag beginnt
            yield True

        with mock.patch.object(gui.keepawake, "keep_awake", spy):
            self.run_list_to_end()
        self.assertEqual(self.factory.events, [])
        self.assertEqual(self.statuses(), [jq.ST_WAIT] * 3)
        self.assertIn("Listenlauf abgebrochen", a.var_status.get())
        self.assertIn("3 Aufträge nicht gestartet", a.var_status.get())
        self.assertEqual(self.dialogs(), 0)  # kein Auftrag gelaufen: kein Abschlussfenster
        self.assertEqual(self.state(a.btn_runall), "normal")

    def test_abbruch_zwischen_zwei_auftraegen(self):
        a = self.app
        for n in ("A", "B", "C"):
            self.add(n)
        self.factory.after_job = lambda name: a._request_cancel() if name == "A" else None
        self.run_list_to_end()
        self.assertEqual([e[1] for e in self.factory.events], ["A"])
        self.assertEqual(self.statuses(), [jq.ST_DONE, jq.ST_WAIT, jq.ST_WAIT])
        self.assertIn("Listenlauf abgebrochen", a.var_status.get())
        self.assertEqual(self.dialogs(), 1)
        title, text = self.mb["showwarning"].call_args.args
        self.assertEqual(title, "Listenlauf abgebrochen")
        self.assertIn("Nicht gestartet: 2 Aufträge warten noch", text)
        self.mb["showinfo"].assert_not_called()

    def test_schliessen_im_listenlauf(self):
        a = self.app
        self.add("A")
        self.add("B")
        self.factory.behaviors = {"A": "block"}
        a._start_list()
        self.assertTrue(self.pump(lambda: ("job", "A") in self.factory.events))
        a._on_close()  # askyesno -> True: abbrechen und beenden
        self.assertTrue(a._closing)
        t0 = time.time()
        while not a._destroyed and time.time() - t0 < 10:
            a.update()
            time.sleep(0.01)
        self.assertTrue(a._destroyed)
        self.assertEqual([e[1] for e in self.factory.events], ["A"])  # B wurde nicht mehr gestartet
        self.assertEqual(self.dialogs(), 0)
        self.assertEqual(self.statuses()[1], jq.ST_WAIT)

    def test_ausnahme_im_converter_konstruktor(self):
        a = self.app
        for n in ("A", "B", "C"):
            self.add(n)
        self.factory.behaviors = {"B": "ctor_raise"}
        self.run_list_to_end()
        self.assertEqual([e[1] for e in self.factory.events], ["A", "C"])
        self.assertEqual(self.statuses(), [jq.ST_DONE, jq.ST_ERROR, jq.ST_DONE])
        self.assertIn("Konstruktor kaputt", a.jq.jobs()[1].error)
        self.assertEqual(self.dialogs(), 1)

    def test_doppelte_quelle_rueckfrage(self):
        a = self.app
        self.add("A", dst=str(self.root / "dst" / "A1"))
        self.mb["askyesno"].return_value = False
        self.add("A", dst=str(self.root / "dst" / "A2"))
        self.assertEqual(len(a.jq), 1)
        self.assertIn("Dieselbe Quelle steht schon in der Liste", self.mb["askyesno"].call_args.args[1])
        self.mb["askyesno"].return_value = True
        self.add("A", dst=str(self.root / "dst" / "A2"))
        self.assertEqual(len(a.jq), 2)
        self.assertEqual(a.jq.jobs()[1].answers.get("duplicate_source"), True)

    def test_geaenderte_quelle_wird_nicht_gestartet(self):
        a = self.app
        self.add("A")
        self.add("B")
        # andere Karte im Kartenleser: Clip-Liste der Quelle A ändert sich
        (Path(self.card("A")) / "Clip" / "NEU.MXF").write_bytes(b"anderer Inhalt")
        self.run_list_to_end()
        self.assertEqual([e[1] for e in self.factory.events], ["B"])  # A wurde nicht gestartet
        self.assertEqual(self.statuses(), [jq.ST_ERROR, jq.ST_DONE])
        self.assertIn("Die Quelle hat sich seit dem Hinzufügen geändert", a.jq.jobs()[0].error)
        self.assertIn("geändert", self.mb["showwarning"].call_args.args[1])

    def test_direkter_start_lehnt_ziel_in_quelle_der_liste_ab(self):
        a = self.app
        self.add("A")
        a.var_src.set(self.card("C"))
        a.var_dst.set(str(Path(self.card("A")) / "ausgabe"))
        a._start()
        self.assertEqual(self.factory.events, [])
        self.assertFalse(a._ui_running)
        self.assertIn("Quelle eines Auftrags", self.mb["showwarning"].call_args.args[1])

    def test_schaetzung_im_hintergrund_ohne_doppelte_analyse(self):
        a = self.app
        calls = []
        gate = threading.Event()

        def slow(opts):
            calls.append(opts.source)
            gate.wait(10)
            return 1234

        a._analyze = slow
        src = self.card("A")
        a.var_src.set(src)
        a.var_dst.set(str(self.root / "dst" / "A1"))
        t0 = time.time()
        a._add_to_list()
        self.assertLess(time.time() - t0, 2.0)  # blockiert die Oberfläche nicht
        j1 = a.jq.jobs()[0]
        self.assertIsNone(j1.est_bytes)
        self.mb["askyesno"].return_value = True  # dieselbe Quelle: Rückfrage bestätigen
        a.var_dst.set(str(self.root / "dst" / "A2"))
        a._add_to_list()
        j2 = a.jq.jobs()[1]
        self.assertEqual(len(a.jq), 2)
        gate.set()
        self.assertTrue(self.pump(lambda: j1.est_bytes == 1234 and j2.est_bytes == 1234))
        self.assertEqual(len(calls), 1)  # nur EINE Analyse für dieselbe Karte


class FakeOverview:
    def __init__(self, est=777, text="Kamera: Attrappe"):
        self.est_target_bytes = est
        self._t = text

    def text(self):
        return self._t


class ListRound10Tests(GuiQueueBase):
    def _real_overview(self, behaviour):
        """Echtes _refresh_overview wieder einsetzen, converter.overview durch eine Attrappe ersetzen."""
        a = self.app
        a._refresh_overview = gui.App._refresh_overview.__get__(a)
        p = mock.patch.object(gui.converter, "overview", behaviour)
        p.start()
        self.addCleanup(p.stop)

    def _snapshot(self):
        a = self.app
        return (a.var_status.get(), a.pb_total["value"], self.state(a.btn_start), self.state(a.btn_runall),
                self.state(a.btn_cancel), a._ui_running, a._run_mode)

    def test_abbruch_waehrend_der_fingerabdruck_pruefung_startet_keinen_converter(self):
        a = self.app
        self.add("A")
        self.add("B")
        orig = jq.source_changed

        def slow(job, fp_fn=None):
            a._request_cancel()  # Nutzer bricht ab, während die Prüfung läuft
            return orig(job, fp_fn)

        with mock.patch.object(jq, "source_changed", slow):
            self.run_list_to_end()
        self.assertEqual(self.factory.events, [])
        self.assertEqual(self.factory.tokens, [])  # Converter wurde gar nicht erzeugt
        self.assertEqual(self.statuses(), [jq.ST_WAIT, jq.ST_WAIT])
        self.assertIn("Listenlauf abgebrochen", a.var_status.get())
        self.assertEqual(self.dialogs(), 0)
        self.assertEqual(self.state(a.btn_runall), "normal")

    def test_uebersicht_laeuft_schon_nur_eine_analyse(self):
        a = self.app
        gate = threading.Event()
        calls = []
        analyze_calls = []

        def ov(opts, cancel=None):
            calls.append(opts.source)
            gate.wait(10)
            return FakeOverview(est=4321)

        a._analyze = lambda opts: analyze_calls.append(opts.source) or 1
        self._real_overview(ov)
        src = self.card("A")
        a.var_src.set(src)
        a._refresh_overview()  # Übersichts-Analyse läuft (blockiert)
        self.assertTrue(self.pump(lambda: len(calls) == 1))
        a.var_dst.set(str(self.root / "dst" / "A1"))
        a._add_to_list()
        j = a.jq.jobs()[0]
        self.assertIsNone(j.est_bytes)
        gate.set()
        self.assertTrue(self.pump(lambda: j.est_bytes == 4321))
        self.assertEqual(len(calls), 1)       # genau EIN converter.overview-Aufruf
        self.assertEqual(analyze_calls, [])   # keine zweite Analyse

    def test_uebersicht_scheitert_eigene_analyse_springt_ein(self):
        a = self.app
        gate = threading.Event()

        def ov(opts, cancel=None):
            gate.wait(10)
            raise FileNotFoundError("weg")

        a._analyze = lambda opts: 99
        self._real_overview(ov)
        a.var_src.set(self.card("A"))
        a._refresh_overview()
        self.pump(lambda: False, 0.2)
        a.var_dst.set(str(self.root / "dst" / "A1"))
        a._add_to_list()
        j = a.jq.jobs()[0]
        gate.set()
        self.assertTrue(self.pump(lambda: j.est_bytes == 99))

    def test_einzellauf_prueft_hinzufuegen_gegen_laufenden_lauf(self):
        a = self.app
        src, dst = self.card("S"), str(self.root / "dst" / "S")
        a.var_src.set(src)
        a.var_dst.set(dst)
        self.factory.gate.clear()
        a._start()
        self.assertTrue(self.pump(lambda: self.factory.started.is_set()))
        # Felder ändern: geprüft wird gegen die beim Start gemerkten Werte
        a.var_src.set(self.card("T"))
        a.var_dst.set(dst)  # gleiches Ziel wie der laufende Lauf
        a._add_to_list()
        self.assertEqual(len(a.jq), 0)
        self.assertIn("laufender Lauf", self.mb["showwarning"].call_args.args[1])
        a.var_dst.set(str(Path(src) / "ausgabe"))  # Ziel in der Quelle des laufenden Laufs
        a._add_to_list()
        self.assertEqual(len(a.jq), 0)
        a.var_src.set(dst)  # Quelle = Ziel des laufenden Laufs
        a.var_dst.set(str(self.root / "dst" / "X"))
        a._add_to_list()
        self.assertEqual(len(a.jq), 0)
        self.assertEqual(self.mb["showwarning"].call_count, 3)
        self.add("U")  # unabhängiger Auftrag ist weiter möglich
        self.assertEqual(len(a.jq), 1)
        self.factory.gate.set()
        self.assertTrue(self.pump(lambda: not a._ui_running))
        self.pump(lambda: False, 0.3)
        self.assertIsNone(a._single_opts)

    def test_uebersicht_waehrend_listenlauf_aendert_den_lauf_nicht(self):
        a = self.app
        self.add("A")
        self.add("B")
        self.factory.gate.clear()

        def ov(opts, cancel=None):
            if "KAPUTT" in opts.source:
                raise FileNotFoundError("weg")
            return FakeOverview()

        self._real_overview(ov)
        a._start_list()
        self.assertTrue(self.pump(lambda: self.factory.started.is_set()))
        self.assertTrue(self.pump(lambda: a.var_status.get().startswith("Läuft … Auftrag 1")))
        a.pb_total["value"] = 420
        before = self._snapshot()
        a.var_src.set(self.card("NEU"))
        a._refresh_overview()
        self.assertTrue(self.pump(lambda: a.var_overview.get().startswith("Kamera"), 3))
        a.var_src.set(str(self.root / "KAPUTT"))
        a._refresh_overview()
        self.assertTrue(self.pump(lambda: "nicht nutzbar" in a.var_overview.get(), 3))
        a.var_bitrate.set("60")
        self.pump(lambda: False, 1.0)
        self.assertEqual(before, self._snapshot())
        # Knöpfe/Tastatur starten keinen zweiten Lauf
        a.btn_start.invoke()
        a.btn_runall.invoke()
        a._start()
        a._start_list()
        self.factory.gate.set()
        self.assertTrue(self.pump(lambda: not a._ui_running))
        self.pump(lambda: False, 0.3)
        self.assertEqual([e[1] for e in self.factory.events], ["A", "B"])
        self.assertEqual(self.dialogs(), 1)

    def test_uebersicht_waehrend_einzellauf_aendert_den_lauf_nicht(self):
        a = self.app
        self._real_overview(lambda opts, cancel=None: FakeOverview())
        a.var_src.set(self.card("S"))
        a.var_dst.set(str(self.root / "dst" / "S"))
        self.factory.gate.clear()
        a._start()
        self.assertTrue(self.pump(lambda: self.factory.started.is_set()))
        self.assertTrue(self.pump(lambda: a._ui_running))
        a.pb_total["value"] = 300
        before = self._snapshot()
        a.var_src.set(self.card("T"))
        a._refresh_overview()
        self.assertTrue(self.pump(lambda: a.var_overview.get().startswith("Kamera"), 3))
        self.assertEqual(before, self._snapshot())
        self.assertEqual(a._run_mode, "single")
        a.btn_start.invoke()
        a.btn_runall.invoke()
        self.factory.gate.set()
        self.assertTrue(self.pump(lambda: not a._ui_running))
        self.pump(lambda: False, 0.3)
        self.assertEqual([Path(o.source).name for o in self.factory.opts_seen], ["S"])  # Start-Werte, nicht die Felder
        self.assertEqual(self.dialogs(), 1)


class ListLayoutTests(GuiQueueBase):
    def test_mindesthoehe_waechst_mit_liste_ohne_abschneiden(self):
        a = self.app
        a.update()
        h0 = a.minsize()[1]
        self.add("A")
        a.tree.selection_set(a.tree.get_children()[0])
        for _ in range(5):
            a.update()
            time.sleep(0.05)
        self.assertGreater(a.minsize()[1], h0)
        minw, minh = a.minsize()
        a.geometry(f"{minw}x{minh}")
        for _ in range(5):
            a.update()
            time.sleep(0.05)
        h = a.winfo_height()
        bottom = lambda w: w.winfo_rooty() - a.winfo_rooty() + w.winfo_height()  # noqa: E731
        self.assertLessEqual(bottom(a.frm_list), h)
        self.assertLessEqual(bottom(a.lbl_thermal), h)
        lbl = a.lbl_job_detail
        self.assertGreaterEqual(lbl.winfo_height(), lbl.winfo_reqheight(), "Detailtext nicht abgeschnitten")
        self.assertIn(a.jq.jobs()[0].source, a.var_job_detail.get())  # voller Pfad steht lesbar da


if __name__ == "__main__":
    unittest.main()
