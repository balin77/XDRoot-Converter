"""Tests der Auftragsliste (reine Logik, ohne tkinter, ohne Medien, ohne externe Programme)."""

import os
import tempfile
import unittest
from pathlib import Path

from xdroot_converter import converter, jobqueue as jq


def _opts(src="Q1", dst="Z1", **kw):
    return converter.Options(source=src, target=dst, **kw)


def _summary(status="ok", **kw):
    s = converter.RunSummary()
    if status == "ok":
        s.results = [converter.ClipResult("C1", converter.ST_CONVERTED, src_size=1000, dst_size=400),
                     converter.ClipResult("C2", converter.ST_SKIPPED_DONE, src_size=500, dst_size=200),
                     converter.ClipResult("C3", converter.ST_COPIED, src_size=7, dst_size=7)]
        s.seconds = 10.0
    for k, v in kw.items():
        setattr(s, k, v)
    return s


def _fmt(n):
    return f"{n:.0f}B"


class SettingsTextTests(unittest.TestCase):
    def test_standard(self):
        o = _opts(thermal="auto", auto_verify=True)
        self.assertEqual(jq.settings_text(o), "50 Mbit/s, medium")

    def test_abweichungen(self):
        o = _opts(bitrate_mbps=42.5, preset="slow", unlisted="skip", thermal="off", threads=4, auto_verify=True,
                  auto_verify_deep=False)
        t = jq.settings_text(o)
        self.assertIn("42.5 Mbit/s, slow", t)
        for part in ("nicht gelistete überspringen", "ohne Temperaturüberwachung", "4 Kerne", "schnelle Prüfung"):
            self.assertIn(part, t)
        self.assertIn("ohne Abschlussprüfung", jq.settings_text(_opts(thermal="auto", auto_verify=False)))
        self.assertIn("Drosseln/Pause", jq.settings_text(_opts(thermal="auto", thermal_throttle=True, auto_verify=True)))


class ListTests(unittest.TestCase):
    def test_hinzufuegen_reihenfolge_ids(self):
        q = jq.JobQueue()
        a = q.add(_opts("A", "ZA"))
        b = q.add(_opts("B", "ZB"))
        self.assertEqual([j.id for j in q.jobs()], [a.id, b.id])
        self.assertNotEqual(a.id, b.id)
        self.assertEqual(len(q), 2)
        self.assertEqual(a.status, jq.ST_WAIT)

    def test_momentaufnahme(self):
        q = jq.JobQueue()
        o = _opts("A", "ZA", only=["x"])
        j = q.add(o, answers={"resume_existing": True})
        o.bitrate_mbps = 80.0
        o.target = "anders"
        o.only.append("y")
        self.assertEqual(j.options.bitrate_mbps, 50.0)
        self.assertEqual(j.target, "ZA")
        self.assertEqual(j.options.only, ["x"])
        self.assertEqual(j.answers, {"resume_existing": True})
        self.assertEqual(j.settings_text, "50 Mbit/s, medium, ohne Temperaturüberwachung, ohne Abschlussprüfung")

    def test_entfernen_und_leeren(self):
        q = jq.JobQueue()
        a, b, c = (q.add(_opts(x, "Z" + x)) for x in "ABC")
        self.assertTrue(q.remove(b.id))
        self.assertFalse(q.remove(b.id))
        self.assertEqual([j.source for j in q.jobs()], ["A", "C"])
        q.begin_run()
        run = q.claim_next().job  # A läuft
        self.assertFalse(q.remove(run.id))
        self.assertEqual(q.clear(), 1)  # nur C; der laufende bleibt
        self.assertEqual(q.jobs(), [run])
        q.finish(run, _summary())
        self.assertEqual(q.clear(), 1)
        self.assertEqual(len(q), 0)


class ConflictTests(unittest.TestCase):
    def setUp(self):
        self.q = jq.JobQueue()
        self.q.add(_opts(os.path.join("D:\\", "karte1") if os.name == "nt" else "/d/karte1",
                         os.path.join("E:\\", "ziel1") if os.name == "nt" else "/e/ziel1"))
        self.s1 = self.q.jobs()[0].source
        self.t1 = self.q.jobs()[0].target

    def test_gleiches_ziel(self):
        self.assertIn("steht schon in der Liste", self.q.find_conflict("X", self.t1))

    def test_ziel_verschachtelt(self):
        self.assertIn("steht schon in der Liste", self.q.find_conflict("X", os.path.join(self.t1, "unter")))
        self.assertIn("steht schon in der Liste", self.q.find_conflict("X", os.path.dirname(self.t1)))

    def test_quelle_ist_ziel_anderer(self):
        self.assertIn("Ziel eines anderen Auftrags", self.q.find_conflict(self.t1, "Zneu"))
        self.assertIn("Ziel eines anderen Auftrags", self.q.find_conflict("Q", "Zneu", source_root=self.t1))

    def test_ziel_ist_quelle_anderer(self):
        self.assertIn("Quelle eines anderen Auftrags", self.q.find_conflict("Q", self.s1))

    def test_gleiche_quelle_anderes_ziel_erlaubt(self):
        self.assertEqual(self.q.find_conflict(self.s1, os.path.join(os.path.dirname(self.t1), "ziel2")), "")

    def test_gross_klein_unter_windows(self):
        if os.name != "nt":
            self.skipTest("nur Windows")
        self.assertTrue(self.q.find_conflict("X", self.t1.upper()))

    def test_erledigte_zaehlen_mit(self):
        j = self.q.jobs()[0]
        j.status = jq.ST_DONE
        self.assertTrue(self.q.find_conflict("X", self.t1))


class SpaceTests(unittest.TestCase):
    @staticmethod
    def drive(p):
        return p.split(":")[0].upper() if ":" in p else "?"

    def test_summiert_wartende_je_laufwerk(self):
        q = jq.JobQueue()
        q.add(_opts("A", "D:/z1"), est_bytes=400)
        q.add(_opts("B", "E:/z2"), est_bytes=900)  # anderes Laufwerk: zählt nicht
        done = q.add(_opts("C", "D:/z3"), est_bytes=5000)
        done.status = jq.ST_DONE  # erledigt: zählt nicht
        key, need = jq.space_needed(q.jobs(), "D:/z4", 300, self.drive)
        self.assertEqual((key, need), ("D", 700))

    def test_abgebrochene_und_laufende_zaehlen_mit(self):
        q = jq.JobQueue()
        a = q.add(_opts("A", "D:/z1"), est_bytes=100)
        b = q.add(_opts("B", "D:/z2"), est_bytes=200)
        c = q.add(_opts("C", "D:/z3"), est_bytes=400)
        a.status, b.status = jq.ST_CANCELLED, jq.ST_RUN
        self.assertEqual(jq.space_needed(q.jobs(), "D:/z4", 1000, self.drive), ("D", 1700))

    def test_warnung(self):
        q = jq.JobQueue()
        q.add(_opts("A", "D:/z1"), est_bytes=600)
        w = q.space_warning("D:/z2", 500, _fmt, free_fn=lambda p: 1000, drive_of=self.drive)
        self.assertIn("1100B", w)
        self.assertIn("1000B", w)
        self.assertIn("auf D", w)
        self.assertIn("trotzdem", w)

    def test_keine_warnung(self):
        q = jq.JobQueue()
        q.add(_opts("A", "D:/z1"), est_bytes=600)
        self.assertEqual(q.space_warning("D:/z2", 400, _fmt, free_fn=lambda p: 1000, drive_of=self.drive), "")
        self.assertEqual(q.space_warning("D:/z2", None, _fmt, free_fn=lambda p: 1, drive_of=self.drive), "")
        self.assertEqual(q.space_warning("D:/z2", 10 ** 9, _fmt, free_fn=lambda p: None, drive_of=self.drive), "")

    def test_echte_funktionen(self):
        with tempfile.TemporaryDirectory() as d:
            sub = os.path.join(d, "noch", "nicht", "da")
            self.assertEqual(jq.drive_key(sub), jq.drive_key(d))
            self.assertGreater(jq.free_bytes(sub), 0)


class RunTests(unittest.TestCase):
    def _q(self, n=3):
        q = jq.JobQueue()
        for i in range(n):
            q.add(_opts(f"Q{i}", f"Z{i}"))
        return q

    def test_reihenfolge_und_nummerierung(self):
        q = self._q()
        self.assertEqual(q.begin_run(), 3)
        seen = []
        while True:
            c = q.claim_next()
            if c is None:
                break
            seen.append((c.job.source, c.n, c.total))
            self.assertEqual(q.running_job(), c.job)
            q.finish(c.job, _summary())
        self.assertEqual(seen, [("Q0", 1, 3), ("Q1", 2, 3), ("Q2", 3, 3)])
        self.assertEqual([j.status for j in q.jobs()], [jq.ST_DONE] * 3)
        self.assertEqual(len(q.run_jobs()), 3)

    def test_waehrend_des_laufs_hinzugefuegt_wird_mit_erfasst(self):
        q = self._q(2)
        q.begin_run()
        c = q.claim_next()
        q.add(_opts("Q9", "Z9"))
        q.finish(c.job, _summary())
        c2 = q.claim_next()
        self.assertEqual((c2.n, c2.total), (2, 3))
        q.finish(c2.job, _summary())
        c3 = q.claim_next()
        self.assertEqual((c3.job.source, c3.n, c3.total), ("Q9", 3, 3))

    def test_fehler_stoppt_nicht(self):
        q = self._q()
        q.begin_run()
        q.finish(q.claim_next().job, _summary(fatal="Platte voll"))
        q.fail(q.claim_next().job, RuntimeError("kaputt"))
        c = q.claim_next()
        q.finish(c.job, _summary())
        self.assertEqual([j.status for j in q.jobs()], [jq.ST_ERROR, jq.ST_ERROR, jq.ST_DONE])
        self.assertIn("RuntimeError: kaputt", q.jobs()[1].result_line())

    def test_fortsetzen(self):
        q = self._q()
        q.begin_run()
        q.finish(q.claim_next().job, _summary())                     # Q0 fertig
        q.finish(q.claim_next().job, _summary(cancelled=True))       # Q1 abgebrochen
        self.assertEqual([j.status for j in q.jobs()], [jq.ST_DONE, jq.ST_CANCELLED, jq.ST_WAIT])
        self.assertTrue(q.has_runnable())
        self.assertEqual(q.begin_run(), 2)  # abgebrochener wird wieder aufgenommen
        order = []
        while True:
            c = q.claim_next()
            if c is None:
                break
            order.append(c.job.source)
            q.finish(c.job, _summary())
        self.assertEqual(order, ["Q1", "Q2"])  # Q0 nicht erneut
        self.assertFalse(q.has_runnable())

    def test_fehlerhafte_werden_nicht_wiederholt(self):
        q = self._q(1)
        q.begin_run()
        q.fail(q.claim_next().job, ValueError("x"))
        self.assertFalse(q.has_runnable())
        self.assertEqual(q.begin_run(), 0)

    def test_abandon(self):
        q = self._q(1)
        q.begin_run()
        q.claim_next()
        q.abandon_running()
        self.assertEqual(q.jobs()[0].status, jq.ST_ERROR)
        self.assertIsNone(q.running_job())


class ClassifyTests(unittest.TestCase):
    def test_status(self):
        V = converter.VerifyOutcome
        self.assertEqual(jq.classify(_summary()), jq.ST_DONE)
        self.assertEqual(jq.classify(_summary(fatal="x")), jq.ST_ERROR)
        self.assertEqual(jq.classify(_summary(cancelled=True)), jq.ST_CANCELLED)
        self.assertEqual(jq.classify(_summary(cancelled=True, abort_reason="CPU kühlt nicht ab")), jq.ST_ERROR)
        s = _summary()
        s.results.append(converter.ClipResult("C9", converter.ST_FAILED))
        self.assertEqual(jq.classify(s), jq.ST_ERROR)
        self.assertEqual(jq.classify(_summary(verify=V(started=True, complete=True, deviations=["d"]))), jq.ST_DEVIATION)
        self.assertEqual(jq.classify(_summary(verify=V(started=True, complete=True))), jq.ST_DONE)

    def test_hitze_abbruch_zeile(self):
        q = jq.JobQueue()
        j = q.add(_opts())
        q.begin_run()
        q.claim_next()
        q.finish(j, _summary(cancelled=True, abort_reason="CPU kühlt nicht ab."))
        self.assertIn("CPU kühlt nicht ab", j.result_line())
        self.assertTrue(j.result_line().startswith("Ergebnis:"))


class BatchTests(unittest.TestCase):
    def _done(self, q, specs):
        q.begin_run()
        for status in specs:
            c = q.claim_next()
            if status == "ok":
                q.finish(c.job, _summary())
            elif status == "cancel":
                q.finish(c.job, _summary(cancelled=True))
            elif status == "fatal":
                q.finish(c.job, _summary(fatal="Platte voll"))
            else:
                q.fail(c.job, RuntimeError("boom"))

    def test_alles_gut(self):
        q = jq.JobQueue()
        for i in range(2):
            q.add(_opts(f"Q{i}", f"Z{i}"))
        self._done(q, ["ok", "ok"])
        kind, title, text = jq.batch_message(q.run_jobs(), 0, _fmt, lambda s: f"{s:.0f}s")
        self.assertEqual(kind, "info")
        self.assertEqual(title, "Auftragsliste fertig")
        self.assertIn("1. Z0 – fertig", text)
        self.assertIn("2. Z1 – fertig", text)
        self.assertEqual(text.count("Ergebnis:"), 2)
        self.assertIn("4 Clips", text)            # je Auftrag 2 (umgewandelt + bereits fertig)
        self.assertIn("3000B -> 1200B", text)     # (1000+500)*2 -> (400+200)*2
        self.assertIn("20s", text)
        self.assertIn("Empfehlung", text)

    def test_mit_problemen(self):
        q = jq.JobQueue()
        for i in range(3):
            q.add(_opts(f"Q{i}", f"Z{i}"))
        self._done(q, ["fatal", "boom", "ok"])
        kind, title, text = jq.batch_message(q.run_jobs(), 0, _fmt, lambda s: "x")
        self.assertEqual(kind, "warning")
        self.assertIn("mit Problemen", title)
        self.assertIn("Platte voll", text)
        self.assertIn("RuntimeError: boom", text)
        self.assertNotIn("Empfehlung", text)
        self.assertIn("davon 1 ohne Beanstandung", text)

    def test_abgebrochen(self):
        q = jq.JobQueue()
        for i in range(3):
            q.add(_opts(f"Q{i}", f"Z{i}"))
        self._done(q, ["ok", "cancel"])
        waiting = len(q.waiting())
        kind, title, text = jq.batch_message(q.run_jobs(), waiting, _fmt, lambda s: "x")
        self.assertEqual(kind, "warning")
        self.assertIn("abgebrochen", title)
        self.assertIn("Nicht gestartet: 1", text)
        self.assertIn("Alle konvertieren", text)
        self.assertIn("abgebrochen", jq.batch_status(q.run_jobs(), waiting))
        self.assertIn("1 Auftrag nicht gestartet", jq.batch_status(q.run_jobs(), waiting))

    def test_status_text(self):
        q = jq.JobQueue()
        q.add(_opts())
        self._done(q, ["ok"])
        self.assertEqual(jq.batch_status(q.run_jobs()), "Liste fertig: 1 Auftrag.")


class TextTests(unittest.TestCase):
    def test_einzahl_mehrzahl(self):
        self.assertEqual(jq.plural_jobs(1), "1 Auftrag")
        self.assertEqual(jq.plural_jobs(0), "0 Aufträge")
        self.assertEqual(jq.plural_jobs(3), "3 Aufträge")

    def test_abbruch_vor_dem_ersten_auftrag(self):
        st = jq.batch_status([], 3, user_cancelled=True)
        self.assertIn("Listenlauf abgebrochen", st)
        self.assertIn("3 Aufträge nicht gestartet", st)
        self.assertNotIn("fertig", st)
        self.assertIn("1 Auftrag nicht gestartet", jq.batch_status([], 1, user_cancelled=True))

    def test_abbruch_zwischen_zwei_auftraegen(self):
        q = jq.JobQueue()
        for i in range(3):
            q.add(_opts(f"Q{i}", f"Z{i}"))
        q.begin_run()
        q.finish(q.claim_next().job, _summary())  # Q0 fertig, danach Nutzerabbruch ohne weiteren Auftrag
        jobs, waiting = q.run_jobs(), len(q.waiting())
        kind, title, text = jq.batch_message(jobs, waiting, user_cancelled=True)
        self.assertEqual((kind, title), ("warning", "Listenlauf abgebrochen"))
        self.assertIn("Nicht gestartet: 2 Aufträge warten noch", text)
        self.assertNotIn("Empfehlung", text)
        self.assertIn("Listenlauf abgebrochen", jq.batch_status(jobs, waiting, user_cancelled=True))
        self.assertIn("1 Auftrag bearbeitet", text)
        self.assertIn("Nicht gestartet: 1 Auftrag wartet noch", jq.batch_message(jobs, 1, user_cancelled=True)[2])

    def test_hinweis_bei_fehler(self):
        q = jq.JobQueue()
        q.add(_opts())
        q.begin_run()
        q.finish(q.claim_next().job, _summary(fatal="x"))
        text = jq.batch_message(q.run_jobs())[2]
        self.assertIn("nicht automatisch wiederholt", text)
        self.assertIn("entfernen und erneut hinzufügen", text)

    def test_eigener_abbruch_ist_fehler_nicht_abgebrochen(self):
        self.assertEqual(jq.classify(_summary(cancelled=True), user_cancelled=False), jq.ST_ERROR)
        self.assertEqual(jq.classify(_summary(cancelled=True), user_cancelled=True), jq.ST_CANCELLED)


def _make_card(root, name, clip_bytes=b"x"):
    d = os.path.join(root, name)
    os.makedirs(os.path.join(d, "Clip"), exist_ok=True)
    with open(os.path.join(d, "MEDIAPRO.XML"), "wb") as f:
        f.write(b"<x/>")
    with open(os.path.join(d, "Clip", "C0001.MXF"), "wb") as f:
        f.write(clip_bytes)
    return d


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_stabil_und_aenderung(self):
        d = _make_card(self.tmp.name, "K")
        fp = jq.source_fingerprint(d)
        self.assertTrue(fp)
        self.assertEqual(jq.source_fingerprint(d), fp)
        with open(os.path.join(d, "Clip", "C0002.MXF"), "wb") as f:
            f.write(b"y")  # zusätzlicher Clip
        self.assertNotEqual(jq.source_fingerprint(d), fp)
        fp2 = jq.source_fingerprint(d)
        with open(os.path.join(d, "Clip", "C0002.MXF"), "wb") as f:
            f.write(b"yy")  # andere Größe
        self.assertNotEqual(jq.source_fingerprint(d), fp2)
        fp3 = jq.source_fingerprint(d)
        with open(os.path.join(d, "MEDIAPRO.XML"), "wb") as f:
            f.write(b"<y/>")  # andere MEDIAPRO.XML
        self.assertNotEqual(jq.source_fingerprint(d), fp3)

    def test_liest_keine_clips(self):
        d = _make_card(self.tmp.name, "K")
        real_open = open
        seen = []

        def spy(file, *a, **k):
            seen.append(str(file))
            return real_open(file, *a, **k)

        from unittest import mock
        with mock.patch("builtins.open", spy), mock.patch.object(type(__import__("pathlib").Path()), "read_bytes",
                                                                 lambda self_: seen.append(str(self_)) or b"<x/>"):
            jq.source_fingerprint(d)
        self.assertFalse([x for x in seen if x.upper().endswith(".MXF")], seen)

    def test_source_changed(self):
        d = _make_card(self.tmp.name, "K")
        q = jq.JobQueue()
        j = q.add(_opts(d, "Z"), fingerprint=jq.source_fingerprint(d))
        self.assertEqual(jq.source_changed(j), "")
        _make_card(self.tmp.name, "K", clip_bytes=b"anderer Inhalt laenger")
        self.assertIn("Die Quelle hat sich seit dem Hinzufügen geändert", jq.source_changed(j))
        j2 = q.add(_opts(d, "Z2"))  # ohne Fingerabdruck keine Prüfung
        self.assertEqual(jq.source_changed(j2), "")
        j.fingerprint = jq.source_fingerprint(d)
        import shutil
        shutil.rmtree(d)  # Karte entfernt
        self.assertTrue(jq.source_changed(j))

    def test_fail_mit_text(self):
        q = jq.JobQueue()
        j = q.add(_opts())
        q.begin_run()
        q.claim_next()
        q.fail(j, jq.SOURCE_CHANGED_MSG)
        self.assertEqual(j.status, jq.ST_ERROR)
        self.assertTrue(j.result_line().startswith("Ergebnis: Fehler – Die Quelle hat sich"))


class SameSourceAndDirectTests(unittest.TestCase):
    def test_gleiche_quelle(self):
        q = jq.JobQueue()
        a = q.add(_opts("QA", "ZA"))
        self.assertEqual(q.same_source_jobs("QA"), [a])
        self.assertEqual(q.same_source_jobs("QB"), [])
        a.status = jq.ST_DONE
        self.assertEqual(q.same_source_jobs("QA"), [])  # nur wartende/abgebrochene

    def test_direkter_start_gegen_quellen_der_liste(self):
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "karte")
            os.makedirs(src)
            q = jq.JobQueue()
            q.add(_opts(src, os.path.join(d, "ziel")))
            self.assertIn("Quelle eines Auftrags", q.find_source_conflict(os.path.join(src, "unter")))
            self.assertIn("Quelle eines Auftrags", q.find_source_conflict(src))
            self.assertEqual(q.find_source_conflict(os.path.join(d, "anders")), "")


class ResolvedPathTests(unittest.TestCase):
    def setUp(self):
        self.out = Path(__file__).resolve().parent.parent / "_test_out" / "lokal"
        self.out.mkdir(parents=True, exist_ok=True)
        self.tmp = tempfile.mkdtemp(dir=str(self.out))
        self.addCleanup(self._cleanup)
        self.links = []

    def _cleanup(self):
        import shutil
        for lk in self.links:
            try:
                os.rmdir(lk)  # entfernt nur die Verknüpfung, nicht ihr Ziel
            except OSError:
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_junction_in_der_quellkarte(self):
        if os.name != "nt":
            self.skipTest("nur Windows")
        import subprocess
        src = os.path.join(self.tmp, "quellkarte")
        os.makedirs(src)
        link = os.path.join(self.tmp, "verknuepfung")
        r = subprocess.run(["cmd", "/c", "mklink", "/J", link, src], capture_output=True, text=True)
        if r.returncode != 0 or not os.path.isdir(link):
            self.skipTest("Junction nicht erzeugbar: " + r.stdout + r.stderr)
        self.links.append(link)
        q = jq.JobQueue()
        q.add(_opts(src, os.path.join(self.tmp, "ziel1")))
        # Ziel eines neuen Auftrags liegt über die Junction IN der Quellkarte des ersten
        self.assertTrue(q.find_conflict("anderequelle", os.path.join(link, "ziel2")))
        self.assertTrue(q.find_source_conflict(os.path.join(link, "ziel2")))
        self.assertTrue(jq.paths_related(link, src))

    def test_kurzname_8_3(self):
        if os.name != "nt":
            self.skipTest("nur Windows")
        import ctypes
        long_dir = os.path.join(self.tmp, "ein sehr langer Ordnername fuer den Test")
        os.makedirs(long_dir)
        buf = ctypes.create_unicode_buffer(520)
        n = ctypes.windll.kernel32.GetShortPathNameW(long_dir, buf, 520)
        short = buf.value if n else ""
        if not short or short.lower() == long_dir.lower():
            self.skipTest("Kurznamen (8.3) auf diesem Laufwerk nicht verfügbar")
        self.assertEqual(jq.norm_path(short), jq.norm_path(long_dir))
        q = jq.JobQueue()
        q.add(_opts("Q", long_dir))
        self.assertIn("steht schon in der Liste", q.find_conflict("X", short))

    def test_langpfad_praefix(self):
        if os.name != "nt":
            self.skipTest("nur Windows")
        d = os.path.join(self.tmp, "praefix")
        os.makedirs(d)
        self.assertEqual(jq.norm_path("\\\\?\\" + d), jq.norm_path(d))
        q = jq.JobQueue()
        q.add(_opts("Q", d))
        self.assertTrue(q.find_conflict("X", "\\\\?\\" + d))

    def test_nachgestellte_punkte_und_leerzeichen_bei_nicht_existierendem_ziel(self):
        if os.name != "nt":
            self.skipTest("nur Windows")
        base = os.path.join(self.tmp, "ausgabe")  # existiert nicht auf der Platte
        q = jq.JobQueue()
        q.add(_opts("Q", base + "A"))
        for variant in (base + "A.", base + "A..", base + "A .", base + "A.\\sub"):
            self.assertIn("steht schon in der Liste", q.find_conflict("X", variant), variant)
        self.assertEqual(q.find_conflict("X", base + "B"), "")

    def test_ruckfall_bei_fehler(self):
        from unittest import mock
        with mock.patch.object(Path, "resolve", side_effect=OSError("kaputt")):
            self.assertEqual(jq.norm_path("a"), os.path.normcase(os.path.abspath("a")))


if __name__ == "__main__":
    unittest.main()
