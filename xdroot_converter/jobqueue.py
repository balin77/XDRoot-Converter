"""Auftragsliste: mehrere Karten nacheinander umwandeln (reine Logik, ohne tkinter).

Ein Auftrag (``Job``) ist eine Momentaufnahme: Optionen (Quelle, Ziel, alle Einstellungen), die beim Hinzufügen gegebenen
Antworten auf Rückfragen, ein Kurztext der Einstellungen und die geschätzte Zielgröße.  ``JobQueue`` hält die Liste
(thread-sicher: die Oberfläche ändert sie, der Arbeits-Thread des Listenlaufs wählt und beendet Aufträge), prüft Duplikate und
Überschneidungen von Quelle und Ziel, summiert den Platzbedarf je Laufwerk und baut die Sammel-Zusammenfassung.

Status je Auftrag: wartet -> läuft -> fertig / fertig mit Abweichungen / Fehler / abgebrochen.
"""
from __future__ import annotations

import copy
import dataclasses
import os
import shutil
import threading
import hashlib
from pathlib import Path
from dataclasses import dataclass, field
from typing import Callable, Optional

ST_WAIT = "wartet"
ST_RUN = "läuft"
ST_DONE = "fertig"
ST_DEVIATION = "fertig mit Abweichungen"
ST_ERROR = "Fehler"
ST_CANCELLED = "abgebrochen"

MAX_ERROR_CHARS = 200


# =========================================================================== Pfade, Konflikte, Platz

def _strip_long_prefix(p: str) -> str:
    """Langpfad-Präfix (Backslash-Backslash-Fragezeichen-Backslash, auch mit UNC) entfernen, damit beide Schreibweisen
    gleich vergleichen."""
    pre = "\\" * 2 + "?" + "\\"
    if p.startswith(pre + "UNC" + "\\"):
        return "\\" * 2 + p[len(pre) + 4:]
    if p.startswith(pre) or p.startswith("\\" * 2 + "." + "\\"):
        return p[4:]
    return p


def norm_path(p: str) -> str:
    """Vergleichbare Form eines Pfads: vollständig aufgelöst (``Path.resolve()``: Kurznamen 8.3, Verknüpfungen/Junctions und
    Langpfad-Präfix), bei einem Fehler Rückfall auf ``abspath``; Groß-/Kleinschreibung wie das Betriebssystem."""
    raw = _strip_long_prefix(str(p).strip())
    try:
        # abspath zuerst: es entfernt nachgestellte Punkte/Leerzeichen (Windows legt „ziel.“ als „ziel“ an), die resolve()
        # bei nicht existierenden Pfaden stehen lässt
        return os.path.normcase(_strip_long_prefix(str(Path(os.path.abspath(raw)).resolve())))
    except (OSError, RuntimeError, ValueError):
        return os.path.normcase(os.path.abspath(raw))


def paths_related(a: str, b: str) -> bool:
    """Gleich oder einer liegt im anderen."""
    na, nb = norm_path(a), norm_path(b)
    if na == nb:
        return True
    try:
        return os.path.commonpath([na, nb]) in (na, nb)
    except ValueError:  # verschiedene Laufwerke
        return False


def drive_key(path: str) -> str:
    """Schlüssel des Laufwerks, auf dem ``path`` liegt (Windows: Laufwerksbuchstabe/UNC-Freigabe, sonst Geräte-Nummer)."""
    p = norm_path(path)
    drive = os.path.splitdrive(p)[0]
    if drive:
        return drive.upper()
    q = p
    while q and not os.path.exists(q):
        parent = os.path.dirname(q)
        if parent == q:
            break
        q = parent
    try:
        return f"dev{os.stat(q).st_dev}"
    except OSError:
        return ""


def free_bytes(path: str) -> Optional[int]:
    """Freier Platz auf dem Laufwerk von ``path`` (nächster vorhandener Elternordner); None, wenn nicht ermittelbar."""
    q = norm_path(path)
    while q and not os.path.exists(q):
        parent = os.path.dirname(q)
        if parent == q:
            break
        q = parent
    try:
        return int(shutil.disk_usage(q).free)
    except OSError:
        return None


def settings_text(o) -> str:
    """Kurztext der Einstellungen, z. B. „50 Mbit/s, medium“; Abweichungen vom Standard werden angehängt."""
    parts = [f"{float(o.bitrate_mbps):g} Mbit/s", str(o.preset)]
    if getattr(o, "unlisted", "convert") == "skip":
        parts.append("nicht gelistete überspringen")
    if getattr(o, "thermal", "off") != "auto":
        parts.append("ohne Temperaturüberwachung")
    elif getattr(o, "thermal_throttle", False):
        parts.append("Drosseln/Pause")
    if getattr(o, "threads", None):
        parts.append(f"{o.threads} Kerne")
    if not getattr(o, "auto_verify", False):
        parts.append("ohne Abschlussprüfung")
    elif not getattr(o, "auto_verify_deep", True):
        parts.append("schnelle Prüfung")
    return ", ".join(parts)


def snapshot_options(o):
    """Unabhängige Kopie der Optionen (spätere Änderungen in der Oberfläche wirken nicht auf den Auftrag)."""
    new = dataclasses.replace(o)
    for f in dataclasses.fields(new):
        v = getattr(new, f.name)
        if isinstance(v, (list, dict, set)):
            setattr(new, f.name, copy.deepcopy(v))
    return new


# =========================================================================== Auftrag

@dataclass
class Job:
    id: int
    options: object  # converter.Options (Momentaufnahme)
    answers: dict = field(default_factory=dict)  # gespeicherte Antworten auf Rückfragen beim Hinzufügen
    settings_text: str = ""
    est_bytes: Optional[int] = None  # geschätzte Zielgröße (None = unbekannt)
    status: str = ST_WAIT
    summary: Optional[object] = None  # converter.RunSummary
    error: str = ""  # Ausnahme-Text (Programmfehler im Auftrag)
    fingerprint: str = ""  # Fingerabdruck der Quelle beim Hinzufügen ('' = keiner, dann keine Prüfung beim Start)

    @property
    def source(self) -> str:
        return str(self.options.source)

    @property
    def target(self) -> str:
        return str(self.options.target)

    def result_line(self) -> str:
        """Ergebniszeile („Ergebnis: …“) des Auftrags, auch ohne Lauf."""
        if self.error:
            return "Ergebnis: Fehler – " + self.error
        if self.summary is not None:
            line = self.summary.result_line()
            reason = " ".join(str(getattr(self.summary, "abort_reason", "") or "").split())
            if reason and self.status == ST_ERROR:
                line += " – " + reason
            return line
        if self.status == ST_WAIT:
            return "Ergebnis: nicht gestartet"
        return "Ergebnis: " + self.status


def classify(summary, user_cancelled: bool = True) -> str:
    """Status eines beendeten Laufs.  Ein Hitze-Abbruch (``abort_reason``) oder jeder andere Abbruch, den der Lauf selbst
    auslöst (``user_cancelled`` False), ist ein Fehler dieses Auftrags (die Liste läuft weiter); nur ein Abbruch durch den
    Nutzer ergibt „abgebrochen“."""
    if getattr(summary, "fatal", ""):
        return ST_ERROR
    if getattr(summary, "cancelled", False):
        return ST_ERROR if (getattr(summary, "abort_reason", "") or not user_cancelled) else ST_CANCELLED
    if getattr(summary, "failed", 0):
        return ST_ERROR
    if getattr(summary, "verify_problem", False):
        return ST_DEVIATION
    return ST_DONE


def find_conflict(jobs: list, source: str, target: str, source_root: Optional[str] = None,
                  labels: Optional[list] = None) -> str:
    """Deutsche Meldung, wenn Quelle/Ziel eines neuen Auftrags mit der Liste kollidieren, sonst ''.

    Abgelehnt: Ziel steht schon in der Liste (auch ineinander verschachtelt), Quelle ist Ziel eines anderen Auftrags und
    umgekehrt.  Dieselbe Quelle mit verschiedenen Zielen ist erlaubt."""
    srcs = [s for s in (source, source_root) if s]
    for n, j in enumerate(jobs, 1):
        lab = labels[n - 1] if labels else f"Auftrag {n}"
        if paths_related(target, j.target):
            return (f"Das Ziel „{target}“ steht schon in der Liste ({lab}: Ziel „{j.target}“). "
                    "Zwei Aufträge dürfen nicht in denselben Ordner schreiben.")
        if any(paths_related(s, j.target) for s in srcs):
            return (f"Die Quelle „{source}“ ist Ziel eines anderen Auftrags ({lab}: Ziel „{j.target}“). "
                    "Quellen werden nur gelesen, nie beschrieben.")
        if any(paths_related(target, s) for s in (j.source,)):
            return (f"Das Ziel „{target}“ ist Quelle eines anderen Auftrags ({lab}: Quelle „{j.source}“). "
                    "Quellen werden nur gelesen, nie beschrieben.")
    return ""


def find_source_conflict(jobs: list, target: str, source_root: Optional[str] = None) -> str:
    """Für den direkten Start: Meldung, wenn das Ziel in oder auf einer Quelle eines Auftrags der Liste liegt, sonst ''."""
    for n, j in enumerate(jobs, 1):
        if paths_related(target, j.source):
            return (f"Das Ziel „{target}“ liegt in oder auf der Quelle eines Auftrags der Liste (Auftrag {n}: Quelle "
                    f"„{j.source}“). Quellen werden nur gelesen, nie beschrieben.")
    return ""


def same_source_jobs(jobs: list, source: str, source_root: Optional[str] = None) -> list:
    """Wartende (oder abgebrochene) Aufträge mit derselben Quelle."""
    return [j for j in jobs if j.status in (ST_WAIT, ST_CANCELLED)
            and (norm_path(j.source) == norm_path(source)
                 or (source_root and norm_path(j.source) == norm_path(source_root)))]


def source_fingerprint(source: str) -> str:
    """Fingerabdruck einer Quellkarte: Hash von ``MEDIAPRO.XML`` (falls vorhanden) sowie Namen und Größen der Dateien in
    ``Clip/``.  Liest nur Verzeichniseinträge und MEDIAPRO.XML, nie Clip-Inhalte.  '' bei Fehler."""
    try:
        from . import card
        root = card.find_xdroot(source)
        h = hashlib.sha256()
        mp = root / "MEDIAPRO.XML"
        if mp.is_file():
            h.update(b"MP:" + hashlib.sha256(mp.read_bytes()).digest())
        entries = []
        with os.scandir(root / "Clip") as it:
            for e in it:
                if e.is_file():
                    entries.append((e.name, e.stat().st_size))
        for name, size in sorted(entries):
            h.update(f"|{name}:{size}".encode("utf-8"))
        return h.hexdigest()
    except Exception:  # noqa: BLE001
        return ""


SOURCE_CHANGED_MSG = ("Die Quelle hat sich seit dem Hinzufügen geändert (andere Karte im Kartenleser oder Karte entfernt). "
                      "Der Auftrag wurde nicht gestartet.")


def source_changed(job: "Job", fp_fn: Optional[Callable[[str], str]] = None) -> str:
    """Meldung, wenn die Quelle nicht mehr dieselbe Karte ist (Fingerabdruck weicht ab), sonst ''."""
    if not job.fingerprint:
        return ""
    now = (fp_fn or source_fingerprint)(job.source)
    return "" if now == job.fingerprint else SOURCE_CHANGED_MSG


def plural_jobs(n: int) -> str:
    return "1 Auftrag" if n == 1 else f"{n} Aufträge"


def space_needed(jobs: list, new_target: str, new_est: Optional[int],
                 drive_of: Callable[[str], str] = drive_key) -> tuple:
    """(Laufwerk, benötigte Bytes) für ``new_target``: Schätzung des neuen Auftrags plus aller WARTENDEN Aufträge auf
    demselben Laufwerk (Aufträge ohne Schätzung zählen nicht).  Wartende, abgebrochene und der laufende Auftrag zählen mit."""
    key = drive_of(new_target)
    need = int(new_est or 0)
    for j in jobs:
        if j.status in (ST_WAIT, ST_CANCELLED, ST_RUN) and j.est_bytes and drive_of(j.target) == key:
            need += int(j.est_bytes)
    return key, need


def space_warning(jobs: list, new_target: str, new_est: Optional[int], fmt_size: Callable[[float], str],
                  free_fn: Optional[Callable[[str], Optional[int]]] = None,
                  drive_of: Callable[[str], str] = drive_key) -> str:
    """Warntext, wenn die geschätzten Zielgrößen aller wartenden Aufträge auf einem Laufwerk (samt dem neuen) den freien
    Platz übersteigen; sonst ''.  Kein hartes Verbot (die Schätzung ist grob)."""
    if not new_est:
        return ""
    key, need = space_needed(jobs, new_target, new_est, drive_of)
    free = (free_fn or free_bytes)(new_target)
    if free is None or need <= free:
        return ""
    where = f" auf {key}" if key and not key.startswith("dev") else ""
    return (f"Die geschätzte Zielgröße aller wartenden Aufträge{where} beträgt zusammen ca. {fmt_size(need)}, "
            f"frei sind nur {fmt_size(free)}. Die Schätzung ist grob. Den Auftrag trotzdem hinzufügen?")


# =========================================================================== Liste

@dataclass
class Claim:
    job: Job
    n: int  # laufende Nummer im Listenlauf („Auftrag n von total“)
    total: int


class JobQueue:
    """Die Auftragsliste.  Alle Methoden sind thread-sicher."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._jobs: list[Job] = []
        self._next_id = 1
        self._claimed: list[Job] = []  # Aufträge des aktuellen Listenlaufs (in Reihenfolge)

    # ---- Liste pflegen
    def add(self, options, answers: Optional[dict] = None, est_bytes: Optional[int] = None,
            text: Optional[str] = None, fingerprint: str = "") -> Job:
        """Neuen wartenden Auftrag aus einer Momentaufnahme der Optionen anlegen."""
        with self._lock:
            snap = snapshot_options(options)
            job = Job(id=self._next_id, options=snap, answers=dict(answers or {}),
                      settings_text=text if text is not None else settings_text(snap), est_bytes=est_bytes,
                      fingerprint=fingerprint)
            self._next_id += 1
            self._jobs.append(job)
            return job

    def jobs(self) -> list:
        with self._lock:
            return list(self._jobs)

    def get(self, job_id: int) -> Optional[Job]:
        with self._lock:
            return next((j for j in self._jobs if j.id == job_id), None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._jobs)

    def remove(self, job_id: int) -> bool:
        """Auftrag entfernen; der laufende Auftrag kann nicht entfernt werden (False)."""
        with self._lock:
            j = self.get(job_id)
            if j is None or j.status == ST_RUN:
                return False
            self._jobs.remove(j)
            return True

    def clear(self) -> int:
        """Alle Aufträge außer dem laufenden entfernen; Anzahl der entfernten."""
        with self._lock:
            keep = [j for j in self._jobs if j.status == ST_RUN]
            n = len(self._jobs) - len(keep)
            self._jobs[:] = keep
            return n

    def find_conflict(self, source: str, target: str, source_root: Optional[str] = None) -> str:
        with self._lock:
            return find_conflict(self._jobs, source, target, source_root)

    def find_source_conflict(self, target: str, source_root: Optional[str] = None) -> str:
        with self._lock:
            return find_source_conflict(self._jobs, target, source_root)

    def same_source_jobs(self, source: str, source_root: Optional[str] = None) -> list:
        with self._lock:
            return same_source_jobs(self._jobs, source, source_root)

    def set_estimate(self, job_id: int, est: Optional[int]) -> None:
        with self._lock:
            j = self.get(job_id)
            if j is not None and est:
                j.est_bytes = int(est)

    def space_warning(self, new_target: str, new_est: Optional[int], fmt_size: Callable[[float], str],
                      free_fn: Optional[Callable[[str], Optional[int]]] = None,
                      drive_of: Callable[[str], str] = drive_key) -> str:
        with self._lock:
            return space_warning(self._jobs, new_target, new_est, fmt_size, free_fn, drive_of)

    # ---- Abfragen
    def waiting(self) -> list:
        with self._lock:
            return [j for j in self._jobs if j.status == ST_WAIT]

    def running_job(self) -> Optional[Job]:
        with self._lock:
            return next((j for j in self._jobs if j.status == ST_RUN), None)

    def has_runnable(self) -> bool:
        """Gibt es etwas für „Alle konvertieren“ (wartende oder abgebrochene = wieder aufzunehmende Aufträge)?"""
        with self._lock:
            return any(j.status in (ST_WAIT, ST_CANCELLED) for j in self._jobs)

    # ---- Listenlauf
    def begin_run(self) -> int:
        """Listenlauf vorbereiten: abgebrochene Aufträge werden wieder aufgenommen (-> wartet).  Fertige und fehlerhafte
        Aufträge werden nicht erneut gestartet.  Gibt die Zahl der wartenden Aufträge zurück."""
        with self._lock:
            self._claimed = []
            for j in self._jobs:
                if j.status == ST_CANCELLED:
                    j.status = ST_WAIT
                    j.summary = None
                    j.error = ""
            return sum(1 for j in self._jobs if j.status == ST_WAIT)

    def claim_next(self) -> Optional[Claim]:
        """Nächsten wartenden Auftrag (Listenreihenfolge) auf „läuft“ setzen; None, wenn keiner mehr wartet.  Auch während
        des Laufs hinzugefügte Aufträge werden so noch erfasst."""
        with self._lock:
            job = next((j for j in self._jobs if j.status == ST_WAIT), None)
            if job is None:
                return None
            job.status = ST_RUN
            self._claimed.append(job)
            total = len(self._claimed) + sum(1 for j in self._jobs if j.status == ST_WAIT)
            return Claim(job, len(self._claimed), total)

    def finish(self, job: Job, summary, user_cancelled: bool = True) -> str:
        with self._lock:
            job.summary = summary
            job.status = classify(summary, user_cancelled)
            return job.status

    def fail(self, job: Job, exc) -> str:
        """Unerwartete Ausnahme (oder Meldungstext) in einem Auftrag: Fehler dieses Auftrags, die Liste läuft weiter."""
        with self._lock:
            msg = " ".join((exc if isinstance(exc, str) else f"{type(exc).__name__}: {exc}").split())
            job.error = msg if len(msg) <= MAX_ERROR_CHARS else msg[:MAX_ERROR_CHARS].rstrip() + " …"
            job.status = ST_ERROR
            return job.status

    def abandon_running(self, why: str = "Programmfehler: Auftrag wurde nicht beendet") -> None:
        """Sicherheitsnetz am Ende des Listenlaufs: kein Auftrag darf „läuft“ bleiben."""
        with self._lock:
            for j in self._jobs:
                if j.status == ST_RUN:
                    j.status = ST_ERROR
                    j.error = j.error or why

    def release(self, job: Job) -> None:
        """Einen gerade übernommenen Auftrag, der nicht gestartet wurde (Nutzer-Abbruch), wieder auf „wartet“ setzen und aus
        den Aufträgen des Listenlaufs nehmen."""
        with self._lock:
            if job.status == ST_RUN:
                job.status = ST_WAIT
            if job in self._claimed:
                self._claimed.remove(job)

    def run_jobs(self) -> list:
        """Aufträge des letzten Listenlaufs (in der Reihenfolge, in der sie gestartet wurden)."""
        with self._lock:
            return list(self._claimed)


# =========================================================================== Sammel-Zusammenfassung

def batch_totals(jobs: list) -> dict:
    """Summen über die Aufträge: Clips (umgewandelt + bereits fertig), Größe vorher/nachher, Dauer."""
    clips = src = dst = 0
    seconds = 0.0
    # Statusnamen des Converters; Import hier, damit das Modul ohne converter importierbar bleibt
    from . import converter as _c
    for j in jobs:
        s = j.summary
        if s is None:
            continue
        seconds += float(getattr(s, "seconds", 0.0) or 0.0)
        for r in getattr(s, "results", []):
            if r.status in (_c.ST_CONVERTED, _c.ST_SKIPPED_DONE):
                clips += 1
                src += int(r.src_size or 0)
                dst += int(r.dst_size or 0)
    return {"clips": clips, "src": src, "dst": dst, "seconds": seconds}


def batch_message(jobs: list, n_not_started: int = 0, fmt_size: Optional[Callable[[float], str]] = None,
                  fmt_duration: Optional[Callable[[float], str]] = None, user_cancelled: bool = False) -> tuple:
    """Das EINE Abschlussfenster des Listenlaufs: (art, titel, text); art = info | warning.

    Je Auftrag eine Zeile mit Ziel und Status plus die Ergebniszeile, darunter die Summen."""
    if fmt_size is None or fmt_duration is None:
        from . import converter as _c
        fmt_size = fmt_size or _c.fmt_size
        fmt_duration = fmt_duration or _c.fmt_duration
    lines = []
    for n, j in enumerate(jobs, 1):
        lines.append(f"{n}. {j.target} – {j.status}")
        lines.append("   " + j.result_line())
    t = batch_totals(jobs)
    n_ok = sum(1 for j in jobs if j.status == ST_DONE)
    parts = [f"{plural_jobs(len(jobs))} bearbeitet, davon {n_ok} ohne Beanstandung.", "\n".join(lines)]
    parts.append(f"Summen: {t['clips']} Clips umgewandelt bzw. bereits fertig, Größe {fmt_size(t['src'])} -> "
                 f"{fmt_size(t['dst'])}, Dauer {fmt_duration(t['seconds'])}")
    cancelled = user_cancelled or any(j.status == ST_CANCELLED for j in jobs)
    if n_not_started:
        verb = "wartet" if n_not_started == 1 else "warten"
        parts.append(f"Nicht gestartet: {plural_jobs(n_not_started)} {verb} noch. „Alle konvertieren“ setzt die "
                     "Liste fort (fertige Clips werden übersprungen).")
    if cancelled:
        kind, title = "warning", "Listenlauf abgebrochen"
    elif n_ok == len(jobs):
        kind, title = "info", "Auftragsliste fertig"
        parts.append("Empfehlung: Bitte die Ergebnisse prüfen (z. B. einige Clips abspielen), "
                     "bevor die Quellkarten gelöscht oder überschrieben werden.")
    else:
        kind, title = "warning", "Auftragsliste fertig – mit Problemen"
        parts.append("Einzelheiten stehen im Protokoll und in der Protokolldatei jedes Zielordners.")
    if any(j.status == ST_ERROR for j in jobs):
        parts.append("Aufträge mit „Fehler“ werden nicht automatisch wiederholt. Zum Fortsetzen den Auftrag entfernen "
                     "und erneut hinzufügen (fertige Clips werden übersprungen).")
    return kind, title, "\n\n".join(parts)


def batch_status(jobs: list, n_not_started: int = 0, user_cancelled: bool = False) -> str:
    """Kurztext für die Statuszeile nach dem Listenlauf."""
    if user_cancelled or any(j.status == ST_CANCELLED for j in jobs):
        wait = f", {plural_jobs(n_not_started)} nicht gestartet" if n_not_started else ""
        return f"Listenlauf abgebrochen: {plural_jobs(len(jobs))} bearbeitet{wait}."
    bad = sum(1 for j in jobs if j.status != ST_DONE)
    return f"Liste fertig: {plural_jobs(len(jobs))}" + (f", davon {bad} mit Fehlern oder Abweichungen." if bad else ".")
