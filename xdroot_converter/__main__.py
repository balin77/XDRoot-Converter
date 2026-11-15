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


def run() -> None:
    try:
        from .gui import main

        main()
    except Exception:  # noqa: BLE001
        _report_crash(traceback.format_exc())
        raise SystemExit(1)


if __name__ == "__main__":
    run()
