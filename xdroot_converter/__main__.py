"""Start der GUI: python -m xdroot_converter"""

import sys
import tempfile
import traceback
from pathlib import Path


def _report_crash(tb: str) -> None:
    """Unter pythonw gibt es keine Konsole: Fehler in eine Datei schreiben und anzeigen."""
    path = Path(tempfile.gettempdir()) / "XDROOT-Converter-Fehler.txt"
    try:
        path.write_text(tb, encoding="utf-8")
    except OSError:
        pass
    text = f"Der XDROOT-Converter konnte nicht gestartet werden bzw. ist abgestürzt.\n\n{tb[-1500:]}\n\nDetails: {path}"
    try:
        import tkinter
        from tkinter import messagebox

        r = tkinter.Tk()
        r.withdraw()
        messagebox.showerror("XDROOT-Converter", text)
        r.destroy()
    except Exception:  # noqa: BLE001
        if sys.stderr is not None:
            print(text, file=sys.stderr)


def run(argv=None) -> None:
    args = sys.argv[1:] if argv is None else list(argv)
    if "--als-administrator" in args:
        # Rechte-Erhoehung in Python (nicht in der .bat): startet start_gui.pyw erhoeht (UAC-Abfrage) und beendet sich
        from . import elevate, thermal

        try:  # Windows-Konsolen (cp850) vertragen nicht jedes Zeichen: nie an der Ausgabe scheitern
            sys.stderr.reconfigure(errors="replace")
        except Exception:  # noqa: BLE001
            pass
        say = lambda s="": print(s, file=sys.stderr, flush=True)  # noqa: E731
        if not thermal.is_admin():
            rc = elevate.relaunch_as_admin(console=False, script_name=elevate.GUI_SCRIPT, out=say)
            # 0 = Erhoehung angestossen; sonst eigener Exit-Code, damit die .bat "abgelehnt" von einem Absturz unterscheidet
            raise SystemExit(0 if rc == 0 else elevate.EXIT_ELEVATION_FAILED)
        # schon erhoeht: GUI ohne Konsolenfenster starten, das (Konsolen-)Fenster der .bat schliesst sich
        rc = elevate.start_detached(elevate.GUI_SCRIPT, out=say)
        if rc == 0:
            raise SystemExit(0)
    try:
        from .gui import main

        main()
    except Exception:  # noqa: BLE001
        _report_crash(traceback.format_exc())
        raise SystemExit(1)


if __name__ == "__main__":
    run()
