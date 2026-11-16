"""Fuehrt die Temperatur-Diagnose (--thermal-test) aus und wartet am Ende IMMER auf Enter, damit das (erhoehte)
Konsolenfenster offen bleibt - auch nach Strg+C oder einem Fehler.  Wird von
``python -m xdroot_converter.cli --thermal-test --als-administrator`` erhoeht gestartet."""

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
    except SystemExit:
        raise
    except KeyboardInterrupt:
        print("\nAbgebrochen.")
    except BaseException:  # noqa: BLE001
        traceback.print_exc()
    try:
        input("\nEnter druecken zum Schliessen ...")
    except (EOFError, KeyboardInterrupt):
        pass
    return rc


if __name__ == "__main__":
    sys.exit(main())
