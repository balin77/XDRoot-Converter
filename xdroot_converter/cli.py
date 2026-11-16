"""Kommandozeile.

  python -m xdroot_converter.cli <quelle> <ziel> [--bitrate 50] [--preset medium] [--max-frames N]
  python -m xdroot_converter.cli --verify <ziel-xdroot> [--source <quell-xdroot>] [--deep]
  python -m xdroot_converter.cli --mxf-info <datei.MXF>
  python -m xdroot_converter.cli --thermal-test        (Diagnose Temperatur/Lüfter; Administrator-Konsole)

Temperaturüberwachung: --thermal auto|off (Standard auto), --temp-fan GRAD, --temp-pause GRAD;
Schonmodus: --threads N (ffmpeg-Threads begrenzen).
Abschlussprüfung nach der Umwandlung (Standard an, gründlich): --no-auto-verify, --auto-verify-quick, --retries N.
"""

from __future__ import annotations

import argparse
import os
import signal
import sys
import time

from . import converter, keepawake, thermal, tools, verify


class CliReporter(converter.Reporter):
    def __init__(self, verbose: bool = False) -> None:
        self._last = 0.0
        self.verbose = verbose
        self._temp_text = ""  # letzte Temperatur-Statuszeile (wird an die Fortschrittszeile angehaengt)

    def thermal_status(self, text: str, state: str) -> None:
        self._temp_text = text if state in ("ok", "fan", "pause") else ""

    def log(self, level: str, msg: str) -> None:
        if level == "detail" and not self.verbose:
            return  # Einzelheiten stehen nur in konvertierung.log (oder mit --verbose hier)
        prefix = {"warn": "WARNUNG: ", "error": "FEHLER: "}.get(level, "")
        print(prefix + msg, flush=True)

    def progress(self, clip_name, clip_frac, total_frac, eta_s, text="") -> None:
        now = time.time()
        if now - self._last < 10 and clip_frac < 1.0:
            return
        self._last = now
        eta = f", Rest ca. {converter.fmt_duration(eta_s)}" if eta_s else ""
        temp = f" | {self._temp_text}" if self._temp_text else ""
        print(f"  [{clip_name}] {clip_frac * 100:5.1f} % - gesamt {total_frac * 100:5.1f} %{eta} {text}{temp}", flush=True)


THREADS_MAX = 256  # wie die Oberflaeche
NB = " "  # geschütztes Leerzeichen: "97 °C" bricht in --help nicht zwischen Zahl und Einheit um


def _positive_int(text: str) -> int:
    try:
        v = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{text}' ist keine ganze Zahl") from None
    if v < 1:
        raise argparse.ArgumentTypeError("muss mindestens 1 sein")
    if v > THREADS_MAX:
        raise argparse.ArgumentTypeError(f"darf hoechstens {THREADS_MAX} sein")
    return v


def _retries_value(text: str) -> int:
    try:
        v = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{text}' ist keine ganze Zahl") from None
    if not (0 <= v <= converter.AUTO_RETRIES_MAX):
        raise argparse.ArgumentTypeError(f"muss zwischen 0 und {converter.AUTO_RETRIES_MAX} liegen")
    return v


def _temp_value(text: str) -> float:
    """Argumenttyp fuer eine Temperaturschwelle: nur die Zahl.  Die Grenzen (wie in der Oberflaeche) werden erst nach dem
    Parsen mit ``thermal.check_limits`` geprueft, und nur bei eingeschalteter Ueberwachung (--thermal off prueft nichts).
    (argparse setzt den Optionsnamen selbst vor die Meldung.)"""
    try:
        v = float(text.replace(",", "."))
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{text}' ist keine Zahl") from None
    if v != v or v in (float("inf"), float("-inf")):
        raise argparse.ArgumentTypeError(f"'{text}' ist keine gueltige Temperatur")
    return v


def _os_message(e: OSError, path) -> str:
    """Deutsche Meldung statt des (englischen) strerror-Textes."""
    if isinstance(e, FileNotFoundError):
        return f"Datei nicht gefunden: {path}"
    if isinstance(e, (PermissionError, IsADirectoryError)):
        return f"Kein Zugriff auf: {path}"
    return f"Datei nicht lesbar: {path}"


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        prog="xdroot_converter.cli", description="Sony XDROOT: XAVC-Intra -> XAVC-L",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Exit-Codes: 0 fertig (mit Abschlussprüfung: geprüft ohne Abweichung), 1 Fehler oder Abweichung, "
               "2 falscher Aufruf, 3 Rechte-Erhöhung abgelehnt, 130 abgebrochen (auch Temperatur-Abbruch)")
    ap.add_argument("source", nargs="?", help="Quellordner (XDROOT oder Elternordner)")
    ap.add_argument("target", nargs="?", help="Zielordner (darin entsteht XDROOT)")
    ap.add_argument("--bitrate", type=float, default=50.0, help="Video-Bitrate in Mbit/s (Standard 50)")
    ap.add_argument("--preset", default="medium", help="x264-Preset (Standard medium)")
    ap.add_argument("--max-frames", type=int, default=None, help="NUR TEST: nur die ersten N Frames je Clip (ohne Abschlussprüfung)")
    ap.add_argument("--unlisted", choices=["convert", "skip"], default="convert",
                    help="Clips, die nicht in MEDIAPRO.XML stehen: umwandeln (Standard) oder ueberspringen")
    ap.add_argument("--only", action="append", default=[], metavar="TEXT",
                    help="nur Clips, deren Name TEXT enthaelt (mehrfach moeglich; alle anderen werden nicht uebernommen)")
    ap.add_argument("--embed-xml", choices=["auto", "always", "never"], default="auto",
                    help="M01.XML in die MXF einbetten (auto: nur wenn die Quelle es auch hat)")
    ap.add_argument("--no-anc", action="store_true", help="ANC-Datenspur nicht uebernehmen")
    ap.add_argument("--no-sony-header", action="store_true",
                    help="MXF-Kopfdaten NICHT an die Quelle angleichen (Ausgabe traegt dann bmx-Kennungen und bmx-Track-IDs)")
    ap.add_argument("--verbose", action="store_true",
                    help="auch Einzelheiten ausgeben (z. B. jede Aenderung der MXF-Kopfdaten); sie stehen immer in konvertierung.log")
    ap.add_argument("--thermal", choices=["auto", "off"], default="auto",
                    help="Temperaturüberwachung (Messung, Lüfter voll, Last drosseln, Pause); auto = nur wenn ein Anbieter nutzbar ist "
                         "(Standard), sonst läuft alles unverändert")
    ap.add_argument("--temp-fan", type=_temp_value, default=thermal.DEFAULT_FAN_ON_C, metavar="GRAD",
                    help=f"Lüfter voll ab dieser CPU-Temperatur (Standard {thermal.DEFAULT_FAN_ON_C:g}{NB}°C; bleibt bis zum Laufende voll; erlaubt {thermal.TEMP_FAN_MIN:.0f} bis {thermal.TEMP_FAN_MAX:.0f}{NB}°C)")
    ap.add_argument("--temp-pause", type=_temp_value, default=thermal.DEFAULT_PAUSE_C, metavar="GRAD",
                    help=f"ab dieser CPU-Temperatur zuerst Last drosseln (weniger Kerne), ffmpeg anhalten nur als letzte Stufe bzw. 5{NB}°C darüber (Standard {thermal.DEFAULT_PAUSE_C:g}{NB}°C; weiter 12{NB}°C darunter; erlaubt {thermal.TEMP_PAUSE_MIN:.0f} bis {thermal.TEMP_PAUSE_MAX:.0f}{NB}°C)")
    ap.add_argument("--threads", type=_positive_int, default=None, metavar="N",
                    help="Schonmodus: ffmpeg höchstens N Threads (1 bis 256; Decoder und Encoder; auch bei --verify --deep) - weniger "
                         "Wärme, dauert länger (Standard: unbegrenzt). Achtung: x264 mit anderer Kernzahl erzeugt einen "
                         "gleichwertigen, aber NICHT bitgleichen Datenstrom.")
    ap.add_argument("--no-auto-verify", action="store_true",
                    help="keine Abschlussprüfung nach der Umwandlung (Standard: die Zielkarte wird automatisch wie mit "
                         "--verify --source --deep geprüft, fehlerhafte Clips werden neu umgewandelt)")
    ap.add_argument("--auto-verify-quick", action="store_true",
                    help="Abschlussprüfung ohne vollständiges Dekodieren jedes Clips (nur das Clip-Ende; schneller, weniger gründlich)")
    ap.add_argument("--retries", type=_retries_value, default=converter.Options.retries, metavar="N",
                    help=f"Wiederholungsrunden der Abschlussprüfung für fehlerhafte Clips (0 bis {converter.AUTO_RETRIES_MAX}, "
                         f"Standard {converter.Options.retries})")
    ap.add_argument("--thermal-test", action="store_true",
                    help="Diagnose: Anbieter, Rechte, 5 Messungen, nach Enter Lüfter 10 s voll und zurück, Urteil in der "
                         "letzten Zeile; die Ausgabe steht auch in thermal_test.txt (braucht Administratorrechte; "
                         "es läuft nur der Test)")
    ap.add_argument("--als-administrator", action="store_true",
                    help="nur mit --thermal-test: den Test in einer Administrator-Konsole starten (Windows fragt nach der Bestätigung)")
    ap.add_argument("--mxf-info", metavar="DATEI", help="Header-Metadaten einer MXF-Datei ausgeben (Diagnose)")
    ap.add_argument("--verify", metavar="ZIEL", help="Ziel-XDROOT (oder Elternordner) pruefen")
    ap.add_argument("--source", dest="verify_source", metavar="QUELLE", help="mit --verify: zusaetzlich gegen Quelle vergleichen")
    ap.add_argument("--deep", action="store_true", help="mit --verify: jeden Clip komplett dekodieren")
    return ap


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args(argv)

    if args.als_administrator and not args.thermal_test:
        build_parser().error("--als-administrator gibt es hier nur zusammen mit --thermal-test")
    if args.thermal_test:
        if args.source or args.target:
            print("Hinweis: --thermal-test ist eine reine Diagnose - es laeuft NUR der Test, keine Umwandlung "
                  "(Quelle/Ziel werden ignoriert).", flush=True)
        say = lambda s="": print(s, flush=True)  # noqa: E731
        if args.als_administrator:
            from . import elevate
            if not thermal.is_admin():
                # Erhoehung in einem neuen Konsolenfenster (start_thermal_test.py); dieses Fenster endet sofort.  Exit-Code 3 =
                # Erhoehung abgelehnt/fehlgeschlagen (die .bat zeigt nur dann den Hinweis)
                rc = elevate.relaunch_as_admin(True, elevate.THERMAL_SCRIPT, out=say)
                return 0 if rc == 0 else elevate.EXIT_ELEVATION_FAILED
            # schon erhoeht: Test direkt in diesem Fenster, am Ende bleibt es bis Enter offen (Ergebnis lesbar)
            return elevate.run_with_enter_prompt(lambda: thermal.run_thermal_test_logged(say))
        return thermal.run_thermal_test_logged(say)

    if args.mxf_info:
        from . import mxfheader
        if os.path.isdir(args.mxf_info):
            print(f"FEHLER: '{args.mxf_info}' ist ein Ordner, keine MXF-Datei.", file=sys.stderr)
            return 2
        try:
            print(mxfheader.describe(args.mxf_info))
        except mxfheader.MxfParseError as e:
            print(f"FEHLER: {e}", file=sys.stderr)
            return 1
        except OSError as e:
            print(f"FEHLER: {_os_message(e, args.mxf_info)}", file=sys.stderr)
            return 2
        return 0

    # Hinweis bei wirkungslosen Kombinationen (kein Fehler)
    ap_defaults = build_parser().parse_args([])
    if args.verify:
        for flag, name in ((args.no_auto_verify, "--no-auto-verify"), (args.auto_verify_quick, "--auto-verify-quick"),
                           (args.retries != ap_defaults.retries, "--retries")):
            if flag:
                print(f"Hinweis: {name} hat zusammen mit --verify keine Wirkung (die Abschlussprüfung gehört zur "
                      "Umwandlung).", flush=True)
    else:
        if args.no_auto_verify:
            for flag, name in ((args.retries != ap_defaults.retries, "--retries"),
                               (args.auto_verify_quick, "--auto-verify-quick")):
                if flag:
                    print(f"Hinweis: {name} hat zusammen mit --no-auto-verify keine Wirkung (es gibt keine "
                          "Abschlussprüfung).", flush=True)
        if args.max_frames and args.retries != ap_defaults.retries:
            print("Hinweis: --retries hat zusammen mit --max-frames keine Wirkung (Testausgaben haben keine "
                  "Abschlussprüfung).", flush=True)

    if args.verify:
        cancel = tools.CancelToken()
        try:
            with keepawake.keep_awake(lambda s: print(s, flush=True)):  # Standby verhindern (gilt je Thread: derselbe)
                rep = verify.verify_card(args.verify, args.verify_source, deep=args.deep,
                                         log=lambda s: print(s, flush=True), cancel=cancel, threads=args.threads)
        except FileNotFoundError as e:  # Ordner fehlt / kein XDROOT darin (die Meldung nennt den Pfad)
            print(f"FEHLER: {e if e.errno is None else _os_message(e, e.filename or args.verify)}", file=sys.stderr)
            return 2
        except OSError as e:
            print(f"FEHLER: {_os_message(e, e.filename or args.verify)}", file=sys.stderr)
            return 2
        print(rep.text())
        return 0 if rep.ok else 1

    if not args.source or not args.target:
        build_parser().error("Quelle und Ziel angeben (oder --verify)")
    if args.thermal != "off":  # wie die Oberflaeche: bei ausgeschalteter Ueberwachung werden die Grenzen nicht geprueft
        try:
            thermal.check_limits(args.temp_fan, args.temp_pause, "--temp-fan", "--temp-pause")
        except ValueError as e:
            build_parser().error(str(e))
    opts = converter.Options(
        source=args.source,
        target=args.target,
        bitrate_mbps=args.bitrate,
        preset=args.preset,
        max_frames=args.max_frames,
        unlisted=args.unlisted,
        only=args.only,
        embed_xml=args.embed_xml,
        keep_anc=not args.no_anc,
        sony_header=not args.no_sony_header,
        thermal=args.thermal,
        temp_fan=args.temp_fan,
        temp_pause=args.temp_pause,
        threads=args.threads,
        auto_verify=not args.no_auto_verify,
        auto_verify_deep=not args.auto_verify_quick,
        retries=args.retries,
    )
    cancel = tools.CancelToken()
    hits = [0]

    def on_signal(*_):
        hits[0] += 1
        if hits[0] == 1:
            print("Abbruch angefordert - laufende Programme werden beendet (nochmal Strg+C: sofort hart beenden) ...",
                  file=sys.stderr, flush=True)
            cancel.cancel()
        else:
            # zweites Strg+C: Kindprozesse hart beenden und sofort aussteigen (Sperre/Temp-Dateien raeumt der naechste Lauf auf)
            cancel.kill_all()
            thermal.shutdown_all(timeout=4.0)  # Luefterzustand wiederherstellen, Hilfsprozess beenden (os._exit ueberspringt atexit)
            os._exit(130)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, on_signal)
        except (OSError, ValueError):
            pass
    conv = converter.Converter(opts, CliReporter(verbose=args.verbose), cancel)
    summary = conv.run()
    if summary.cancelled:
        return 130
    # Exit-Code 0 nur ohne Fehler und (bei eingeschalteter Abschlussprüfung) bei geprüfter Karte ohne Abweichung
    return 1 if (summary.failed or summary.fatal or summary.verify_problem) else 0


if __name__ == "__main__":
    sys.exit(main())
