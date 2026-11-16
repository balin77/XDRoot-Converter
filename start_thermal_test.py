"""Führt die Temperatur-Diagnose (--thermal-test) aus und wartet am Ende IMMER auf Enter, damit das (erhöhte)
Konsolenfenster offen bleibt - auch nach Strg+C, einem Fehler oder einem SystemExit (dann wird dessen Code nach der
Enter-Abfrage weitergegeben).  Wird von ``python -m xdroot_converter.cli --thermal-test --als-administrator``
erhöht gestartet."""

import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))


def main() -> int:
    rc = 1
    try:
        sys.path.insert(0, HERE)
        os.chdir(HERE)
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001
            pass
        from xdroot_converter import cli

        rc = cli.main(["--thermal-test"])
    except SystemExit as e:
        code = e.code
        rc = code if isinstance(code, int) else (0 if code is None else 1)
        if code is not None and not isinstance(code, int):
            print(code)
    except KeyboardInterrupt:
        print("\nAbgebrochen.")
    except BaseException:  # noqa: BLE001
        traceback.print_exc()
    try:
        input("\nEnter drücken zum Schließen ...")
    except (EOFError, KeyboardInterrupt):
        pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
