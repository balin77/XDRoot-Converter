"""Kommandozeile.

  python -m xdroot_converter.cli <quelle> <ziel> [--bitrate 50] [--preset medium] [--max-frames N]
  python -m xdroot_converter.cli --verify <ziel-xdroot> [--source <quell-xdroot>] [--deep]
"""

from __future__ import annotations

import argparse
import signal
import sys
import time

from . import converter, tools, verify


class CliReporter(converter.Reporter):
    def __init__(self) -> None:
        self._last = 0.0

    def log(self, level: str, msg: str) -> None:
        prefix = {"warn": "WARNUNG: ", "error": "FEHLER: "}.get(level, "")
        print(prefix + msg, flush=True)

    def progress(self, clip_name, clip_frac, total_frac, eta_s, text="") -> None:
        now = time.time()
        if now - self._last < 10 and clip_frac < 1.0:
            return
        self._last = now
        eta = f", Rest ca. {converter.fmt_duration(eta_s)}" if eta_s else ""
        print(f"  [{clip_name}] {clip_frac * 100:5.1f} % - gesamt {total_frac * 100:5.1f} %{eta} {text}", flush=True)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="xdroot_converter.cli", description="Sony XDROOT: XAVC-Intra -> XAVC-L")
    ap.add_argument("source", nargs="?", help="Quellordner (XDROOT oder Elternordner)")
    ap.add_argument("target", nargs="?", help="Zielordner (darin entsteht XDROOT)")
    ap.add_argument("--bitrate", type=float, default=50.0, help="Video-Bitrate in Mbit/s (Standard 50)")
    ap.add_argument("--preset", default="medium", help="x264-Preset (Standard medium)")
    ap.add_argument("--max-frames", type=int, default=None, help="NUR TEST: nur die ersten N Frames je Clip")
    ap.add_argument("--unlisted", choices=["convert", "skip"], default="convert",
                    help="Clips, die nicht in MEDIAPRO.XML stehen: umwandeln (Standard) oder ueberspringen")
    ap.add_argument("--only", action="append", default=[], metavar="TEXT",
                    help="nur Clips, deren Name TEXT enthaelt (mehrfach moeglich; alle anderen werden nicht uebernommen)")
    ap.add_argument("--embed-xml", choices=["auto", "always", "never"], default="auto",
                    help="M01.XML in die MXF einbetten (auto: nur wenn die Quelle es auch hat)")
    ap.add_argument("--no-anc", action="store_true", help="ANC-Datenspur nicht uebernehmen")
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

    if args.verify:
        cancel = tools.CancelToken()
        rep = verify.verify_card(args.verify, args.verify_source, deep=args.deep, log=lambda s: print(s, flush=True), cancel=cancel)
        print(rep.text())
        return 0 if rep.ok else 1

    if not args.source or not args.target:
        build_parser().error("Quelle und Ziel angeben (oder --verify)")
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
    )
    cancel = tools.CancelToken()
    try:
        signal.signal(signal.SIGINT, lambda *_: cancel.cancel())
    except Exception:
        pass
    conv = converter.Converter(opts, CliReporter(), cancel)
    summary = conv.run()
    if summary.cancelled:
        return 130
    return 1 if (summary.failed or summary.fatal) else 0


if __name__ == "__main__":
    sys.exit(main())
