"""Externe Programme finden und Subprozesse sicher starten (ffmpeg, ffprobe, raw2bmx, mxf2raw; optional MXFDump)."""

from __future__ import annotations

import os
import shutil
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
        found = shutil.which(name)
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


class CancelToken:
    """Gemeinsames Abbruch-Objekt; beendet registrierte Prozesse."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._procs: list[subprocess.Popen] = []

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def check(self) -> None:
        if self._event.is_set():
            raise Cancelled()

    def register(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.append(proc)
        if self._event.is_set():
            kill_process(proc)

    def unregister(self, proc: subprocess.Popen) -> None:
        with self._lock:
            if proc in self._procs:
                self._procs.remove(proc)

    def cancel(self) -> None:
        self._event.set()
        self.kill_all()

    def kill_all(self) -> None:
        """Alle registrierten Kindprozesse sofort hart beenden (auch fuer ein zweites Strg+C)."""
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            kill_process(p)


def kill_process(proc: subprocess.Popen) -> None:
    """Prozess (unter Windows samt Prozessbaum) beenden.  Schlaegt taskkill fehl oder fehlt es,
    wird trotzdem ``proc.kill()`` versucht."""
    try:
        if proc.poll() is not None:
            return
    except Exception:
        return
    if sys.platform == "win32":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
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
    try:
        while proc.poll() is None:
            if cancel is not None and cancel.cancelled:
                kill_process(proc)
                break
            if timeout is not None and time.monotonic() - t_start > timeout:
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
