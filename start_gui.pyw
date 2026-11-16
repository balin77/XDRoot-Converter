"""Startet die XDROOT-Converter-Oberflaeche (auch erhoeht per ``python -m xdroot_converter --als-administrator``).

Setzt Suchpfad und Arbeitsverzeichnis auf den eigenen Ordner und zeigt JEDEN Fehler beim Start in einer Meldungsbox.
Die technischen Einzelheiten stehen ausserdem IMMER in start_fehler.txt neben diesem Skript."""

import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
ERROR_NAME = "start_fehler.txt"

SHORT_TEXT = ("XDROOT-Converter konnte nicht gestartet werden. Ist der Programmordner vollständig? "
              "Ist Python 3.11 oder neuer mit tkinter installiert?")


def _report(tb: str) -> None:
    error_file = os.path.join(HERE, ERROR_NAME)
    saved = False
    try:
        with open(error_file, "w", encoding="utf-8") as f:
            f.write(SHORT_TEXT + "\n\nEinzelheiten:\n" + tb)
        saved = True
    except Exception:  # noqa: BLE001
        pass
    where = f"\n\nDie Einzelheiten stehen auch in: {error_file}" if saved else "\n\n(Die Einzelheiten konnten nicht gespeichert werden.)"
    text = SHORT_TEXT + "\n\nEinzelheiten:\n" + tb[-1500:] + where
    try:
        import tkinter
        from tkinter import messagebox

        root = tkinter.Tk()
        root.withdraw()
        messagebox.showerror("XDROOT-Converter", text)
        root.destroy()
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    try:
        sys.path.insert(0, HERE)
        os.chdir(HERE)
        from xdroot_converter.gui import main as gui_main

        gui_main()
        return 0
    except Exception:  # noqa: BLE001
        _report(traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(main())
