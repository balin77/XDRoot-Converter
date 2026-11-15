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


def _popen(args: list[str], **kw) -> subprocess.Popen:
    return subprocess.Popen(
        [str(a) for a in args],
        stdin=subprocess.DEVNULL,
        creationflags=CREATE_NO_WINDOW,
        **kw,
    )


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
        t1.join(timeout=5)
        t2.join(timeout=5)
    finally:
        if cancel is not None:
            cancel.unregister(proc)
        if proc.poll() is None:
            kill_process(proc)
    if cancel is not None:
        cancel.check()
    rc = proc.returncode if proc.returncode is not None else -1
    return ProcResult(rc, "\n".join(out), "\n".join(err), timed_out=timed_out)
