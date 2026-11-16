"""Start mit Administratorrechten (Windows, UAC) - in Python statt in der .bat (Umlaute, Arbeitsverzeichnis, Fehler).

``python -m xdroot_converter --als-administrator`` und ``python -m xdroot_converter.cli --thermal-test
--als-administrator`` rufen ``relaunch_as_admin`` auf: Es wird ``ShellExecuteW(None, "runas", ...)`` mit einem
ABSOLUTEN Interpreterpfad und einem ABSOLUTEN Skriptpfad im Projektordner aufgerufen (Arbeitsverzeichnis = Projektordner,
aber nichts haengt von ihm ab)."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable, Optional

PROJECT_DIR = Path(__file__).resolve().parent.parent
GUI_SCRIPT = "start_gui.pyw"
THERMAL_SCRIPT = "start_thermal_test.py"

_SE_ERR_ACCESSDENIED = 5
# Exit-Code der Programme, wenn die ERHOEHUNG abgelehnt wurde oder scheiterte (die .bat zeigt nur dann den Hinweis;
# 0/1 sind Ergebnisse des gestarteten Programms selbst)
EXIT_ELEVATION_FAILED = 3


class InterpreterNotFound(Exception):
    """sys.executable ist leer oder keine Datei (eingebettetes Python o. ae.)."""


def _shell_execute(verb, exe, params, cwd, show) -> int:
    """ShellExecuteW; Rueckgabewert > 32 = Erfolg.  (In Tests ersetzt - die Erhoehung loest eine UAC-Abfrage aus.)"""
    import ctypes

    fn = ctypes.windll.shell32.ShellExecuteW  # type: ignore[attr-defined]
    fn.restype = ctypes.c_void_p
    r = fn(None, verb, exe, params, cwd, show)
    return int(r or 0)


def interpreter_for(console: bool, executable: Optional[str] = None) -> str:
    """Absoluter Interpreter: ohne Konsole pythonw.exe neben dem laufenden Python (sonst dieses), mit Konsole python.exe."""
    exe = executable if executable is not None else sys.executable
    if not exe or not os.path.isfile(exe):  # os.path.abspath("") waere der Arbeitsordner - nie benutzen
        raise InterpreterNotFound("Der Python-Interpreter dieses Programms wurde nicht gefunden.")
    exe = os.path.abspath(exe)
    d, name = os.path.split(exe)
    want = "python.exe" if console else "pythonw.exe"
    cand = os.path.join(d, want)
    if os.path.isfile(cand):
        return cand
    return exe


def describe_failure(code: int) -> str:
    if code == _SE_ERR_ACCESSDENIED:
        return ("Der Start mit Administratorrechten wurde abgelehnt (die Windows-Abfrage wurde mit Nein beantwortet "
                "oder ist nicht erlaubt).")
    return f"Der Start mit Administratorrechten ist fehlgeschlagen (Windows-Fehlercode {code})."


def relaunch_as_admin(console: bool, script_name: str = GUI_SCRIPT, project_dir: Optional[Path] = None,
                      shell_execute: Optional[Callable] = None, out: Callable = print,
                      executable: Optional[str] = None) -> int:
    """Startet ``<Projektordner>/<script_name>`` erhoeht.  0 = Erhoehung angestossen, 1 = abgelehnt/fehlgeschlagen."""
    if sys.platform != "win32" and shell_execute is None:
        out("Der Start mit Administratorrechten ist nur unter Windows moeglich.")
        return 1
    pd = Path(project_dir) if project_dir is not None else PROJECT_DIR
    script = (pd / script_name).resolve()
    if not script.is_file():
        out(f"Startskript nicht gefunden: {script}")
        return 1
    try:
        exe = interpreter_for(console, executable)
    except InterpreterNotFound as e:
        out(f"{e} Der Start mit Administratorrechten ist nicht moeglich.")
        return 1
    fn = shell_execute or _shell_execute
    try:
        # -E (Umgebungsvariablen wie PYTHONPATH ignorieren) und -s (kein Benutzer-site-packages): der erhoehte
        # Interpreter laedt nichts aus der Umgebung des Benutzers
        code = int(fn("runas", exe, f'-E -s "{script}"', str(pd), 1))
    except Exception as e:  # noqa: BLE001
        out(f"Der Start mit Administratorrechten ist fehlgeschlagen ({type(e).__name__}: {e}).")
        return 1
    if code <= 32:
        out(describe_failure(code))
        return 1
    return 0


def start_detached(script_name: str = GUI_SCRIPT, project_dir: Optional[Path] = None, popen: Optional[Callable] = None,
                   out: Callable = print, executable: Optional[str] = None) -> int:
    """Startet ``<Projektordner>/<script_name>`` OHNE Konsolenfenster als eigenen Prozess (fuer den Fall, dass die
    Administratorrechte schon vorhanden sind: Das Konsolenfenster der .bat schliesst sich danach).  0 = gestartet."""
    import subprocess

    pd = Path(project_dir) if project_dir is not None else PROJECT_DIR
    script = (pd / script_name).resolve()
    if not script.is_file():
        out(f"Startskript nicht gefunden: {script}")
        return 1
    try:
        exe = interpreter_for(False, executable)
    except InterpreterNotFound as e:
        out(str(e))
        return 1
    flags = 0
    if sys.platform == "win32":
        flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    try:
        (popen or subprocess.Popen)([exe, "-E", "-s", str(script)], cwd=str(pd), creationflags=flags, close_fds=True,
                                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError as e:
        out(f"Der Start ist fehlgeschlagen ({e}).")
        return 1
    return 0


def run_with_enter_prompt(run: Callable, prompt: str = "\nEnter druecken zum Schliessen ...",
                          out: Callable = print) -> int:
    """Fuehrt ``run()`` (liefert einen Exit-Code) aus und wartet am Ende IMMER auf Enter, damit ein Konsolenfenster offen
    bleibt - auch nach Strg+C oder einer Ausnahme.  SystemExit wird ohne Abfrage durchgereicht."""
    import traceback

    rc = 1
    try:
        rc = int(run())
    except SystemExit:
        raise
    except KeyboardInterrupt:
        out("\nAbgebrochen.")
    except BaseException:  # noqa: BLE001
        traceback.print_exc()
    try:
        input(prompt)
    except (EOFError, KeyboardInterrupt):
        pass
    return rc
