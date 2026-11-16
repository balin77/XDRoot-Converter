"""Externe Programme finden und Subprozesse sicher starten (ffmpeg, ffprobe, raw2bmx, mxf2raw; optional MXFDump)."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0

_PROGRAMS = ("ffmpeg", "ffprobe", "raw2bmx", "mxf2raw")
# Optionale Programme (keine Pflicht fuer die Umwandlung; nur fuer scripts/mxf_vergleich.py)
OPTIONAL_PROGRAMS = ("MXFDump",)
_cache: dict[str, str] = {}


class ToolNotFound(RuntimeError):
    pass


class Cancelled(Exception):
    """Wird ausgeloest, wenn der Benutzer abbricht."""


def _search_path(name: str) -> Optional[str]:
    """Eigene PATH-Suche statt shutil.which (which sucht unter Windows zuerst im Arbeitsordner und nimmt PATHEXT wie
    .bat/.com): nur ABSOLUTE Ordner, unter Windows nur '<name>.exe', sonst der bloße Name mit Ausführrecht; nie der
    Arbeitsordner.  Ergebnis ist immer ein absoluter Pfad."""
    win = sys.platform == "win32"
    fname = name + ".exe" if win else name
    for d in os.environ.get("PATH", "").split(os.pathsep):
        d = d.strip().strip('"')
        if not d or not os.path.isabs(d):
            continue
        cand = os.path.join(d, fname)
        if os.path.isfile(cand) and (win or os.access(cand, os.X_OK)):
            return os.path.abspath(cand)
    return None


def find_tool(name: str) -> str:
    """Pfad zu einem Programm: erst tools/**/<name>.exe, dann PATH."""
    if name in _cache and os.path.exists(_cache[name]):
        return _cache[name]
    exe = name + (".exe" if sys.platform == "win32" else "")
    tools_dir = PROJECT_ROOT / "tools"
    found: Optional[str] = None
    if tools_dir.is_dir():
        hits = sorted(tools_dir.rglob(exe))
        if hits:
            found = str(hits[-1])
    if found is None:
        found = _search_path(name)
    if found is None:
        raise ToolNotFound(
            f"Programm '{exe}' nicht gefunden (weder in {tools_dir} noch im PATH). "
            "Unter Windows: setup_tools.bat doppelklicken (laedt FFmpeg und bmx nach tools/); "
            "sonst FFmpeg und bmx installieren und in den PATH aufnehmen (siehe README)."
        )
    _cache[name] = found
    return found


def find_optional_tool(name: str) -> Optional[str]:
    """Wie find_tool, liefert aber None statt eines Fehlers, wenn das Programm fehlt."""
    try:
        return find_tool(name)
    except ToolNotFound:
        return None


def check_tools() -> dict[str, str]:
    return {n: find_tool(n) for n in _PROGRAMS}


def suspend_process(proc: subprocess.Popen) -> bool:
    """Prozess anhalten (Windows: NtSuspendProcess, POSIX: SIGSTOP).  True bei Erfolg; jeder Fehler -> False."""
    try:
        if proc.poll() is not None:
            return False
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            nt = ctypes.WinDLL("ntdll")
            nt.NtSuspendProcess.restype = ctypes.c_long
            nt.NtSuspendProcess.argtypes = [wintypes.HANDLE]
            return nt.NtSuspendProcess(int(proc._handle)) >= 0  # type: ignore[attr-defined]
        import signal

        os.kill(proc.pid, signal.SIGSTOP)
        return True
    except Exception:  # noqa: BLE001
        return False


def resume_process(proc: subprocess.Popen) -> bool:
    """Angehaltenen Prozess fortsetzen (Windows: NtResumeProcess, POSIX: SIGCONT).  True bei Erfolg."""
    try:
        if proc.poll() is not None:
            return False
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            nt = ctypes.WinDLL("ntdll")
            nt.NtResumeProcess.restype = ctypes.c_long
            nt.NtResumeProcess.argtypes = [wintypes.HANDLE]
            return nt.NtResumeProcess(int(proc._handle)) >= 0  # type: ignore[attr-defined]
        import signal

        os.kill(proc.pid, signal.SIGCONT)
        return True
    except Exception:  # noqa: BLE001
        return False


class CancelToken:
    """Gemeinsames Abbruch-Objekt; beendet registrierte Prozesse.  Kann sie ausserdem anhalten (Temperaturpause)."""

    def __init__(self) -> None:
        self._event = threading.Event()
        # RLock: der Signal-Handler (Hauptthread, ruft cancel()) kann einen Aufruf des Hauptthreads unterbrechen, der die Sperre haelt
        self._lock = threading.RLock()
        self._procs: list[subprocess.Popen] = []
        self.pause_error = ""  # Grund, wenn pause() Prozesse nicht anhalten konnte ('' = nichts zu melden)
        self._paused = False
        self._paused_since = 0.0
        self._paused_total = 0.0
        self._suspended: list[subprocess.Popen] = []

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    @property
    def paused(self) -> bool:
        return self._paused

    def paused_time(self) -> float:
        """Summe aller Pausenzeiten in Sekunden (eine laufende Pause zaehlt mit).  Zeitueberwachungen ziehen
        diese Zeit ab, damit ein angehaltener Prozess nie als haengend gilt."""
        with self._lock:
            extra = time.monotonic() - self._paused_since if self._paused else 0.0
            return self._paused_total + extra

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled()

    def register(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.append(proc)
            if self._paused and not self._event.is_set() and suspend_process(proc):
                self._suspended.append(proc)
        if self._event.is_set():
            kill_process(proc)

    def unregister(self, proc: subprocess.Popen) -> None:
        with self._lock:
            if proc in self._procs:
                self._procs.remove(proc)
            if proc in self._suspended:
                self._suspended.remove(proc)

    def pause(self) -> bool:
        """Alle registrierten Prozesse anhalten (und spaeter registrierte, bis ``resume``).  True nur, wenn mindestens
        ein Prozess wirklich angehalten wurde.  False, wenn schon pausiert, abgebrochen, kein Prozess laeuft oder das
        Anhalten fehlschlug (dann steht der Grund in ``pause_error``; es wird keine Pausenzeit gezaehlt)."""
        with self._lock:
            self.pause_error = ""
            if self._paused or self._event.is_set():
                return False
            done: list[subprocess.Popen] = []
            failed = 0
            for p in list(self._procs):
                if suspend_process(p):
                    done.append(p)
                elif p.poll() is None:
                    failed += 1
            if not done:
                if failed:
                    self.pause_error = f"{failed} Prozess(e) konnten nicht angehalten werden (NtSuspendProcess/SIGSTOP fehlgeschlagen)"
                return False
            self._paused = True
            self._paused_since = time.monotonic()
            self._suspended.extend(done)
        return True

    def resume(self) -> bool:
        """Pause beenden: angehaltene Prozesse fortsetzen.  False, wenn nicht pausiert."""
        with self._lock:
            if not self._paused:
                return False
            self._paused = False
            self._paused_total += time.monotonic() - self._paused_since
            procs, self._suspended = self._suspended, []
            for p in procs:
                resume_process(p)
        return True

    def cancel(self) -> None:
        """Abbrechen: erst fortsetzen (ein angehaltener Prozess soll sauber enden koennen), dann beenden."""
        self._event.set()
        self.resume()
        self.kill_all()

    def kill_all(self) -> None:
        """Alle registrierten Kindprozesse sofort hart beenden (auch fuer ein zweites Strg+C)."""
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            kill_process(p)


def system_directory() -> Optional[str]:
    """Windows-Systemverzeichnis ueber GetSystemDirectoryW (nicht ueber PATH/Arbeitsordner); None = unbekannt."""
    try:
        import ctypes

        buf = ctypes.create_unicode_buffer(520)
        n = ctypes.windll.kernel32.GetSystemDirectoryW(buf, 520)  # type: ignore[attr-defined]
        return buf.value if 0 < n < 520 else None
    except Exception:  # noqa: BLE001
        return None


def kill_process(proc: subprocess.Popen) -> None:
    """Prozess (unter Windows samt Prozessbaum) beenden.  Schlaegt taskkill fehl oder fehlt es,
    wird trotzdem ``proc.kill()`` versucht."""
    try:
        if proc.poll() is not None:
            return
    except Exception:
        return
    sysdir = system_directory() if sys.platform == "win32" else None
    if sysdir:  # absoluter Pfad: unter Administratorrechten darf kein taskkill.exe aus dem Arbeitsordner laufen
        try:
            subprocess.run(
                [os.path.join(sysdir, "taskkill.exe"), "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=CREATE_NO_WINDOW,
                timeout=15,
            )
        except Exception:
            pass  # taskkill fehlt/haengt: unten trotzdem proc.kill()
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass


class ProcResult:
    def __init__(self, returncode: int, stdout: str, stderr: str, timed_out: bool = False) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.timed_out = timed_out


_job_handle = None  # Windows: Job-Object (KILL_ON_JOB_CLOSE); None = nicht verfuegbar/noch nicht angelegt
_job_failed = False


def _get_kill_on_close_job():
    """Windows: Job-Object anlegen, das alle zugewiesenen Prozesse beendet, wenn dieser Prozess endet (auch bei
    hartem Kill).  Sehr defensiv: bei JEDEM Fehler None (dann bleibt es beim bisherigen Verhalten).  Der Handle wird
    absichtlich nie geschlossen; Windows schliesst ihn beim Prozessende und beendet damit die Kindprozesse."""
    global _job_handle, _job_failed
    if _job_handle is not None or _job_failed or sys.platform != "win32":
        return _job_handle
    try:
        import ctypes
        from ctypes import wintypes

        class _BASIC(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD),
            ]

        class _IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class _EXT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BASIC), ("IoInfo", _IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.restype = wintypes.BOOL
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        job = k32.CreateJobObjectW(None, None)
        if not job:
            raise OSError("CreateJobObject")
        ext = _EXT()
        ext.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(ext), ctypes.sizeof(ext)):  # ...ExtendedLimitInformation
            k32.CloseHandle(wintypes.HANDLE(job))
            raise OSError("SetInformationJobObject")
        _job_handle = job
    except Exception:  # noqa: BLE001
        _job_failed = True
        _job_handle = None
    return _job_handle


def _assign_to_job(proc: subprocess.Popen) -> bool:
    """Kindprozess dem Kill-on-close-Job zuordnen (nur Windows, best effort; jeder Fehler wird verschluckt)."""
    if sys.platform != "win32":
        return False
    try:
        job = _get_kill_on_close_job()
        h = getattr(proc, "_handle", None)
        if not job or h is None:
            return False
        import ctypes
        from ctypes import wintypes

        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        return bool(k32.AssignProcessToJobObject(job, int(h)))
    except Exception:  # noqa: BLE001
        return False


def _popen(args: list[str], **kw) -> subprocess.Popen:
    proc = subprocess.Popen(
        [str(a) for a in args],
        stdin=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
        **kw,
    )
    # Windows: bei hartem Kill dieses Programms sollen ffmpeg/raw2bmx nicht als Waisen weiterlaufen.
    # (Linux: bewusst kein prctl(PR_SET_PDEATHSIG) per preexec_fn - preexec_fn ist mit Threads (tkinter, Lese-Threads)
    # nicht sicher, und PDEATHSIG haengt am erzeugenden Thread, nicht am Prozess.)
    _assign_to_job(proc)
    return proc


def _drain(stream, sink: list[str], on_line: Optional[Callable[[str], None]] = None, keep: int = 400) -> None:
    try:
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
            if on_line is not None:
                try:
                    on_line(line)
                except Exception:
                    pass
            sink.append(line)
            if len(sink) > keep:
                del sink[: len(sink) - keep]
    except Exception:
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


def run_capture(
    args: Iterable,
    cancel: Optional[CancelToken] = None,
    on_stdout_line: Optional[Callable[[str], None]] = None,
    on_stderr_line: Optional[Callable[[str], None]] = None,
    keep_lines: Optional[int] = None,
    timeout: Optional[float] = None,
) -> ProcResult:
    """Prozess starten, stdout und stderr parallel lesen (kein Pipe-Deadlock), UTF-8/replace.

    ``keep_lines`` begrenzt die gespeicherten Zeilen (None = alles bei Ausgabe-Parsing).
    ``timeout`` (Sekunden): danach wird der Prozess beendet, ``ProcResult.timed_out`` ist dann True.

    Gewartet wird in kurzen Schritten (nie unbegrenzt blockierend), damit unter Windows der
    SIGINT-Handler (Strg+C) laufen kann und ein Abbruch sofort greift.
    """
    args = [str(a) for a in args]
    keep = keep_lines if keep_lines is not None else 10**9
    if cancel is not None:
        cancel.check()
    proc = _popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if cancel is not None:
        cancel.register(proc)
    out: list[str] = []
    err: list[str] = []
    t1 = threading.Thread(target=_drain, args=(proc.stdout, out, on_stdout_line, keep), daemon=True)
    t2 = threading.Thread(target=_drain, args=(proc.stderr, err, on_stderr_line, keep), daemon=True)
    t1.start()
    t2.start()
    timed_out = False
    t_start = time.monotonic()
    pause0 = cancel.paused_time() if cancel is not None else 0.0
    try:
        while proc.poll() is None:
            if cancel is not None and cancel.cancelled:
                kill_process(proc)
                break
            # Pausenzeit (Temperaturpause, Prozess angehalten) zaehlt nicht zur Zeitueberwachung
            paused = (cancel.paused_time() - pause0) if cancel is not None else 0.0
            if timeout is not None and time.monotonic() - t_start - paused > timeout:
                timed_out = True
                kill_process(proc)
                break
            try:
                proc.wait(timeout=0.2)
            except subprocess.TimeoutExpired:
                pass
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass
        # Lesethreads: nach Prozessende kommt die Restausgabe sofort; haelt ein Enkelprozess die Pipe offen,
        # nicht ewig warten
        end = time.monotonic() + 3
        t1.join(timeout=max(0.0, end - time.monotonic()))
        t2.join(timeout=max(0.0, end - time.monotonic()))
    finally:
        if cancel is not None:
            cancel.unregister(proc)
        if proc.poll() is None:
            kill_process(proc)
    if cancel is not None:
        cancel.check()
    rc = proc.returncode if proc.returncode is not None else -1
    return ProcResult(rc, "\n".join(out), "\n".join(err), timed_out=timed_out)
