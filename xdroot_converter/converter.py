"""Ablaufsteuerung: Karte pruefen, Struktur kopieren, Clips umwandeln (ffmpeg + raw2bmx), XML anpassen."""

from __future__ import annotations

import contextlib
import dataclasses
import datetime as _dt
import json
import os
import re
import shutil
import socket
import sys
import threading
import time
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Optional

from . import card, keepawake, mxfheader, mxfinfo, tools, verify
from . import thermal as _thermal_mod  # (Options.thermal ist ein Feld; Modulname nicht verdecken)

TMP_NAME = "_tmp_xdroot_converter"
LOG_NAME = "konvertierung.log"
LOCK_NAME = "_xdroot_converter.lock"
SETTINGS_NAME = "konvertierung_einstellungen.json"

# Status je Clip
ST_CONVERTED = "umgewandelt"
ST_COPIED = "unveraendert kopiert"
ST_SKIPPED_DONE = "uebersprungen (bereits fertig)"
ST_NOT_SELECTED = "nicht ausgewaehlt"
ST_FAILED = "FEHLER"
ST_CANCELLED = "abgebrochen"

# Rueckfallwerte der Sony-Identification (siehe docs/analysis/fx6_mxf2raw.txt), falls die Quelle sie nicht liefert
SONY_COMPANY = "Sony"
SONY_PRODUCT = "Mem "  # mit Leerzeichen am Ende
SONY_VERSION_STRING = "2.00"
SONY_PRODUCT_UID = "urn:uuid:cede1104-8280-11de-8a39-08004678031c"
_HEADER_ERRORS = (mxfheader.HeaderPatchError, mxfheader.MxfParseError, OSError)

_SIGNAL_STD = {
    "SMPTE_274": "st274",
    "SMPTE_296": "st296",
    "SMPTE_347": "st347",
    "SMPTE_349": "st349",
    "SMPTE_428": "st428",
}
_COLOR_SITING = {
    "Cositing": "cositing",
    "Horizontal_Mp": "horizmp",
    "Three_Tap": "3tap",
    "Quincunx": "quincunx",
    "Rec601": "bt601",
    "Line_Alternating": "linealt",
    "Vertical_Mp": "vertmp",
}


@dataclass
class Options:
    source: str
    target: str
    bitrate_mbps: float = 50.0
    preset: str = "medium"
    max_frames: Optional[int] = None  # nur Test
    unlisted: str = "convert"  # 'convert' | 'skip': Clips, die nicht in MEDIAPRO.XML stehen
    only: list = field(default_factory=list)  # nur Clips, deren Name eines dieser Teilstuecke enthaelt (Test)
    embed_xml: str = "auto"  # 'auto' (wenn Quelle eins hat) | 'always' | 'never'
    keep_anc: bool = True
    set_create_date: bool = True
    sony_header: bool = True  # MXF-Kopfdaten (Identification, Track-IDs ...) aus der Quell-MXF uebernehmen
    # Schonmodus / Temperaturueberwachung (Runde 3).  Der Standard hier ist "off" (Ablauf exakt wie bisher); CLI und GUI
    # schalten auf "auto" (Messung, wenn ein Anbieter nutzbar ist, sonst unveraendert).
    thermal: str = "off"  # 'auto' | 'off'
    temp_fan: float = _thermal_mod.DEFAULT_FAN_ON_C  # Luefter voll ab (Grad C); bleibt bis zum Laufende
    temp_pause: float = _thermal_mod.DEFAULT_PAUSE_C  # ab hier erst drosseln, Pause als letzte Stufe; weiter unter temp_pause - 12
    # Runde 6: Drosseln, Pause, Notfall und Hitze-Abbruch NUR auf Wunsch (Standard aus); temp_pause wirkt nur damit
    thermal_throttle: bool = False
    threads: Optional[int] = None  # ffmpeg -threads fuer Decoder und Encoder (None = unbegrenzt)
    thermal_interval_s: float = _thermal_mod.DEFAULT_INTERVAL_S  # Messabstand
    thermal_provider: Optional[object] = None  # nur Tests: Fabrik fuer einen Attrappen-Anbieter
    thermal_max_pause_s: float = _thermal_mod.DEFAULT_MAX_PAUSE_S  # laengste Temperaturpause, danach Abbruch (fortsetzbar)
    # Abschlusspruefung mit automatischer Wiederholung (Runde 4).  Intern ist sie AUS (der Ablauf bleibt fuer die Bibliothek
    # und die vielen direkten Aufrufe in den Tests exakt wie bisher, analog zu ``thermal``); CLI und GUI schalten sie ein.
    auto_verify: bool = False
    auto_verify_deep: bool = True  # jeden Clip komplett dekodieren (False = nur das Clip-Ende, wie --verify ohne --deep)
    retries: int = 2  # Wiederholungsrunden (0 bis 5)

AUTO_RETRIES_MAX = 5


class Reporter:
    """Standard-Berichterstatter (tut nichts); GUI/CLI leiten ab."""

    def log(self, level: str, msg: str) -> None:
        """level: ``info`` / ``warn`` / ``error`` oder ``detail``.  ``detail`` sind Einzelheiten (z. B. die
        Zeilen der Kopfdaten-Angleichung), die nur in die Log-Datei gehoeren; Oberflaechen blenden sie
        aus bzw. zeigen sie nur auf Wunsch (CLI: ``--verbose``)."""
        pass

    def progress(self, clip_name: str, clip_frac: float, total_frac: float, eta_s: Optional[float], text: str = "") -> None:
        pass

    def clip_done(self, result: "ClipResult") -> None:
        pass

    def thermal_status(self, text: str, state: str) -> None:
        """Statuszeile der Temperaturueberwachung; ``state``: ok | fan | pause | unavailable | admin (= Adminrechte
        fehlen) | '' (Ueberwachung beendet)."""
        pass


@dataclass
class ClipResult:
    name: str
    status: str = ""
    src_size: int = 0
    dst_size: int = 0
    frames: int = 0
    seconds: float = 0.0
    pause_seconds: float = 0.0  # davon Temperaturpause (ffmpeg angehalten)
    notes: list = field(default_factory=list)
    target_ok: bool = False  # FEHLER, aber ein vorhandener umgewandelter Ziel-Clip blieb unangetastet erhalten
    foreign: bool = False  # FEHLER, weil im Ziel eine fremde Datei (andere UMID) liegt - wurde nicht ueberschrieben
    listed: bool = True  # Clip steht in MEDIAPRO.XML der Quelle
    retry_reasons: list = field(default_factory=list)  # Gruende der Wiederholungen durch die Abschlusspruefung (je Runde)
    retry_failed: bool = False  # die Wiederholung scheiterte; ein vorhandener Ziel-Clip blieb mit seiner Abweichung liegen


@dataclass
class VerifyOutcome:
    """Ergebnis der Abschlusspruefung (samt Wiederholungsrunden)."""
    deep: bool = True
    retries: int = 0  # erlaubte Wiederholungsrunden
    started: bool = False  # die Pruefung hat begonnen
    complete: bool = False  # sie lief vollstaendig zu Ende (nicht abgebrochen, kein Fehler)
    cancelled: bool = False
    error: str = ""  # Pruefung selbst nicht moeglich (Ausnahme)
    skipped: str = ""  # Grund, warum keine Pruefung lief (z. B. Testlauf)
    checked: int = 0  # in der ersten Runde geprueft
    ok: int = 0  # davon am Ende ohne Abweichung
    rounds: int = 0  # durchgefuehrte Wiederholungsrunden
    retried: dict = field(default_factory=dict)  # Clipname -> [Grund je Runde]
    deviations: list = field(default_factory=list)  # endgueltige Abweichungen (Texte)
    bad_clips: list = field(default_factory=list)  # Clips mit endgueltiger Abweichung/Fehler
    notes: list = field(default_factory=list)  # Hinweise (keine Abweichung)
    seconds: float = 0.0  # Dauer der Abschlusspruefung samt Wiederholungen
    retry_seconds: float = 0.0  # davon Neu-Umwandlung

    @property
    def verified_ok(self) -> bool:
        """Geprueft und ohne Abweichung (nur dann 'Fertig - geprueft')."""
        return self.started and self.complete and not self.deviations and not self.error

    @property
    def problem(self) -> bool:
        return self.started and not self.cancelled and bool(self.deviations or self.error)


@dataclass
class RunSummary:
    results: list = field(default_factory=list)
    cancelled: bool = False
    seconds: float = 0.0
    log_path: Optional[Path] = None
    out_root: Optional[Path] = None
    fatal: str = ""
    copy_errors: list = field(default_factory=list)  # [(relativer Pfad, Meldung)] beim Kopieren der Struktur
    pauses: int = 0  # Temperaturpausen (ffmpeg angehalten)
    pause_seconds: float = 0.0
    throttle_steps: int = 0  # Stufenwechsel der Lastdrosselung nach unten (Prozess-Affinitaet)
    throttle_seconds: float = 0.0  # Zeit gedrosselt (ohne die Zeit in der Pause)
    max_cpu_c: Optional[float] = None
    thermal_throttle: bool = True  # Schalter "Bei Hitze drosseln und pausieren" war an (steuert nur den Text der Hoechsttemperatur)
    thermal_note: str = ""  # z. B. "Temperatur nicht verfuegbar: ..." (leer = keine Besonderheit)
    abort_reason: str = ""  # Abbruch durch die Temperaturueberwachung (Pause zu lang); gesetzt zusammen mit cancelled
    verify: Optional[VerifyOutcome] = None  # Abschlusspruefung (None = nicht eingeschaltet)
    convert_seconds: float = 0.0  # Dauer der Umwandlung (ohne Abschlusspruefung); 0 = nicht getrennt gemessen
    test_run: bool = False  # Testlauf (--max-frames): keine Abschlusspruefung

    @property
    def verify_problem(self) -> bool:
        """Die Abschlusspruefung hat Abweichungen gefunden (oder war nicht moeglich)."""
        return self.verify is not None and self.verify.problem

    @property
    def verified_ok(self) -> bool:
        """Lauf ohne Fehler und Abschlusspruefung ohne Abweichung."""
        return (self.verify is not None and self.verify.verified_ok and not self.cancelled and not self.fatal
                and not self.failed)

    def count(self, status: str) -> int:
        return sum(1 for r in self.results if r.status == status)

    @property
    def failed(self) -> int:
        """Fehlgeschlagene Clips plus nicht kopierbare Strukturdateien."""
        return self.count(ST_FAILED) + len(self.copy_errors)

    def incomplete_text(self) -> str:
        """Deutliche Warnung, wenn fehlgeschlagene Clips im Ziel fehlen bzw. unbrauchbar sind ('' wenn alles da)."""
        bad = [r for r in self.results if r.status == ST_FAILED]
        kept = [r.name for r in bad if r.target_ok and not r.retry_failed]
        deviating = [r.name for r in bad if r.target_ok and r.retry_failed and r.dst_size]
        bad = [r for r in bad if not r.target_ok]
        missing = [r.name for r in bad if not r.dst_size]
        foreign = [r.name for r in bad if r.dst_size and r.foreign]
        broken = [r.name for r in bad if r.dst_size and not r.foreign]
        parts = []
        if deviating:
            parts.append(f"{len(deviating)} Clip(s) liegen im Ziel, haben aber eine Abweichung (Neu-Umwandlung "
                         f"gescheitert, der alte Clip blieb unveraendert): {', '.join(deviating)}")
        if kept:
            parts.append(f"{len(kept)} Clip(s) wurden diesmal NICHT verarbeitet, der vorhandene umgewandelte "
                         f"Ziel-Clip blieb unveraendert erhalten: {', '.join(kept)}")
        if missing:
            parts.append(f"{len(missing)} Clip(s) fehlen: {', '.join(missing)}")
        if broken:
            parts.append(f"{len(broken)} Clip(s) im Ziel sind unbrauchbar/veraltet: {', '.join(broken)}")
        if foreign:
            parts.append(f"{len(foreign)} fremde Datei(en) im Ziel (nicht ueberschrieben): {', '.join(foreign)}")
        if not parts:
            return ""
        if not missing and not broken and not foreign:
            return "ACHTUNG: " + "; ".join(parts)
        in_mp = [r.name for r in bad if r.listed]
        if not in_mp:
            tail = ""
        elif len(in_mp) == len(bad):
            tail = " (die Clips stehen aber in MEDIAPRO.XML)"
        else:
            tail = f" (in MEDIAPRO.XML stehen aber: {', '.join(in_mp)})"
        return "ACHTUNG: Zielkarte unvollstaendig - " + "; ".join(parts) + tail

    def _verify_lines(self) -> list:
        v = self.verify
        if v is None:
            return []
        if v.skipped:
            return [f"Abschlussprüfung: {v.skipped}"]
        if not v.started:
            return []
        mode = "gründlich, jeder Clip komplett dekodiert" if v.deep else "schnell, ohne vollständiges Dekodieren"
        out = []
        if v.cancelled:
            out.append(f"Abschlussprüfung ({mode}): abgebrochen, Prüfung unvollständig")
        elif v.error:
            out.append(f"Die Prüfung konnte nicht durchgeführt werden: {v.error} – die Karte ist NICHT geprüft.")
        elif v.checked == 0 and v.deviations and not v.rounds:
            out.append("Abschlussprüfung: keine Clips geprüft")
        else:
            out.append(f"Abschlussprüfung ({mode}): {v.checked} Clips geprüft, {v.ok} ohne Abweichung"
                       + (f", {len(v.retried)} Clip(s) wiederholt in {v.rounds} Runde(n)" if v.rounds else "")
                       + (f", endgültig fehlerhaft: {len(v.bad_clips)}" if v.bad_clips else ""))
        for name, reasons in v.retried.items():
            out.append(f"    - wiederholt: {name} ({'; '.join(reasons)})")
        for d in v.deviations:
            out.append(f"    ABWEICHUNG: {d}")
        for n in v.notes:
            out.append(f"    Hinweis: {n}")
        if v.started:
            conv = f"Umwandlung {fmt_duration(self.convert_seconds)}, " if self.convert_seconds else ""
            out.append(f"Dauer: {conv}Abschlussprüfung {fmt_duration(v.seconds)}"
                       + (f" (davon Wiederholung der Umwandlung {fmt_duration(v.retry_seconds)})" if v.retry_seconds else ""))
        return out

    def result_line(self) -> str:
        """Die EINE eindeutige Ergebniszeile (letzte Zeile von ``text()``)."""
        v = self.verify
        if self.fatal:
            return "Ergebnis: Fehler – " + _first_line(self.fatal, 200)
        if self.cancelled:
            if v is not None and v.started:
                return "Ergebnis: Abgebrochen, Prüfung unvollständig"
            return "Ergebnis: Abgebrochen – Zielkarte unvollständig"
        names: list = []
        for r in self.results:
            if r.status == ST_FAILED and r.name not in names:
                names.append(r.name)
        names += [rel for rel, _m in self.copy_errors if rel not in names]
        if not names and v is not None and v.started:
            names = list(v.bad_clips)

        def broken() -> str:
            shown = ", ".join(names[:10]) + (f" und {len(names) - 10} weitere" if len(names) > 10 else "")
            return "Ergebnis: Fertig mit Fehlern" + (f" (betroffen: {shown})" if names else "")

        if self.failed:
            return broken()
        if v is None or not v.started:
            if self.test_run or (v is not None and v.skipped):
                return "Ergebnis: Fertig (nicht geprüft: Testausgabe)"
            return "Ergebnis: Fertig (nicht geprüft: Abschlussprüfung ausgeschaltet)"
        if v.error:
            return "Ergebnis: Umwandlung fertig, Prüfung nicht möglich – Karte nicht geprüft"
        if v.deviations:
            return broken()
        if v.verified_ok:
            return ("Ergebnis: Fertig – geprüft, ohne Abweichung" if v.deep
                    else "Ergebnis: Fertig – geprüft (schnelle Prüfung), ohne Abweichung")
        return broken()

    def text(self) -> str:
        lines = ["", "=== Zusammenfassung ==="]
        tot_src = tot_dst = 0
        for r in self.results:
            if r.status in (ST_NOT_SELECTED,):
                continue
            lines.append(
                f"{r.name}: {r.status}  {fmt_size(r.src_size)} -> {fmt_size(r.dst_size)}"
                + ((f"  ({r.frames} Frames, {r.seconds:.0f} s"
                    + (f", davon Pause {r.pause_seconds:.0f} s" if r.pause_seconds >= 1 else "") + ")") if r.frames else "")
            )
            for n in r.notes:
                lines.append(f"    - {n}")
            if r.status in (ST_CONVERTED, ST_SKIPPED_DONE):
                tot_src += r.src_size
                tot_dst += r.dst_size
        if tot_src:
            saved = tot_src - tot_dst
            lines.append(
                f"Umgewandelte Clips gesamt: {fmt_size(tot_src)} -> {fmt_size(tot_dst)} "
                f"(Ersparnis {fmt_size(saved)}, {100.0 * saved / tot_src:.0f} %)"
            )
        if self.results or not self.fatal:  # bei fatalem Fehler ohne Clips keine irrefuehrende "Fehler: 0"-Zeile
            lines.append(
                f"umgewandelt: {self.count(ST_CONVERTED)}, bereits fertig: {self.count(ST_SKIPPED_DONE)}, "
                f"kopiert: {self.count(ST_COPIED)}, Fehler: {self.failed}"
                + (", ABGEBROCHEN" if self.cancelled else "")
                + f" - Dauer {fmt_duration(self.seconds)}"
                + (f" (davon Pause {fmt_duration(self.pause_seconds)})" if self.pauses else "")
            )
        if self.abort_reason:
            lines.append(self.abort_reason)
        lines.extend(self._verify_lines())
        for rel, msg in self.copy_errors:
            lines.append(f"FEHLER beim Kopieren von {rel}: {msg}")
        if self.throttle_steps or self.throttle_seconds >= 1:
            lines.append(f"Last gedrosselt: gesamt {fmt_duration(self.throttle_seconds)}"
                         + (f" ({self.throttle_steps} Stufenwechsel)" if self.throttle_steps else ""))
        if self.pauses:
            lines.append(f"Temperaturpausen: {self.pauses}, gesamt {fmt_duration(self.pause_seconds)}"
                         + (f" (höchste gemessene CPU-Temperatur {self.max_cpu_c:.0f} °C)" if self.max_cpu_c else ""))
        elif self.max_cpu_c and not self.thermal_throttle:
            lines.append(f"Höchste CPU-Temperatur {self.max_cpu_c:.0f} °C (Messwert des Geräts, kann über der Anzeige "
                         "des Herstellers liegen)")
        elif self.max_cpu_c:
            lines.append(f"Höchste gemessene CPU-Temperatur: {self.max_cpu_c:.0f} °C, keine Pause nötig")
        if self.thermal_note:
            lines.append(self.thermal_note)
        inc = self.incomplete_text()
        if inc:
            lines.append(inc)
        if self.fatal:
            lines.append("FEHLER: " + self.fatal)
        lines.append(self.result_line())
        return "\n".join(lines)


def _first_line(text: str, limit: int = 160) -> str:
    """Erste nichtleere Zeile eines (evtl. mehrzeiligen) Textes, gekuerzt."""
    lines = [l.strip() for l in str(text).splitlines() if l.strip()]
    t = lines[0] if lines else ""
    return t if len(t) <= limit else t[:limit].rstrip() + " …"


def fmt_size(n: float) -> str:
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024.0
    return f"{n:.2f} TB"


def fmt_duration(s: float) -> str:
    s = int(round(s))
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h:d}:{m:02d}:{sec:02d}" if h else f"{m:d}:{sec:02d}"


def estimate_clip_bytes(frames: int, fps: Fraction, n_audio: int, bitrate_mbps: float) -> int:
    """Geschaetzte Groesse eines Ziel-Clips (Video + 24-Bit-Audio + Muxing)."""
    secs = float(frames) / float(fps)
    video = secs * bitrate_mbps * 1_000_000 / 8.0 * 1.03
    audio = secs * 48000 * 3 * n_audio
    return int(video + audio + frames * 4000)  # ~4 KB/Frame ANC/Index/Overhead


def mxf_date_to_arg(d: str) -> Optional[str]:
    """'2023-05-29 16:46:52.000' -> '2023-05-29T16:46:52:00' (raw2bmx: qm = 1/250 s)."""
    m = re.match(r"^(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})(?:\.(\d+))?", d.strip())
    if not m:
        return None
    ms = int((m.group(3) or "0")[:3].ljust(3, "0"))
    return f"{m.group(1)}T{m.group(2)}:{min(249, ms // 4):02d}"


def prod_info_args(info: "mxfinfo.MxfInfo") -> list[str]:
    """Argumente fuer ``raw2bmx --prod-info`` (Company, Product, Version, Version-String, UID) aus der
    Quell-MXF; Rueckfall auf die Sony-Werte der FX6-Referenz.  ProductVersion '0.0.0.0.0' = weglassen."""
    return [
        "--prod-info",
        info.ident_company or SONY_COMPANY,
        info.ident_product or SONY_PRODUCT,
        "0.0.0.0.0",
        info.ident_version_string or SONY_VERSION_STRING,
        info.ident_product_uid or SONY_PRODUCT_UID,
    ]


# --------------------------------------------------------------------------- Sperre gegen parallele Laeufe
class TargetBusy(RuntimeError):
    """Der Zielordner wird bereits von einem anderen Lauf benutzt."""


class ForeignTarget(RuntimeError):
    """Im Ziel liegt unter dem Namen des Clips eine fremde Datei (andere UMID o. ae.); sie wird nie ueberschrieben."""


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            import ctypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = ctypes.c_void_p
            h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return ctypes.get_last_error() == 5  # ACCESS_DENIED: Prozess existiert
            try:
                code = ctypes.c_ulong(0)
                if k32.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code)):
                    return code.value == 259  # STILL_ACTIVE
                return True
            finally:
                k32.CloseHandle(ctypes.c_void_p(h))
        except Exception:  # noqa: BLE001
            return True  # im Zweifel: lebt
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _boot_time() -> Optional[float]:
    """Unix-Zeit des Systemstarts (best effort, None = unbekannt)."""
    try:
        if sys.platform == "win32":
            import ctypes

            k32 = ctypes.WinDLL("kernel32")
            k32.GetTickCount64.restype = ctypes.c_ulonglong
            return time.time() - k32.GetTickCount64() / 1000.0
        with open("/proc/stat", encoding="ascii", errors="replace") as f:
            for line in f:
                if line.startswith("btime"):
                    return float(line.split()[1])
    except Exception:  # noqa: BLE001
        pass
    return None


def _proc_start_time(pid: int) -> Optional[int]:
    """Startzeit eines Prozesses als opake Zahl (Linux: /proc/<pid>/stat Feld 22, Windows: GetProcessTimes);
    None = nicht ermittelbar.  Dient dazu, Wiederverwendung einer PID zu erkennen."""
    try:
        if sys.platform == "win32":
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.OpenProcess.restype = wintypes.HANDLE
            k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            k32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
            k32.CloseHandle.argtypes = [wintypes.HANDLE]
            h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return None
            try:
                c, e, k, u = (wintypes.FILETIME() for _ in range(4))
                if not k32.GetProcessTimes(h, ctypes.byref(c), ctypes.byref(e), ctypes.byref(k), ctypes.byref(u)):
                    return None
                return (c.dwHighDateTime << 32) | c.dwLowDateTime
            finally:
                k32.CloseHandle(h)
        with open(f"/proc/{int(pid)}/stat", "rb") as f:
            data = f.read().decode("ascii", errors="replace")
        rest = data[data.rindex(")") + 2:].split()  # ab Feld 3
        return int(rest[19])  # Feld 22 = starttime
    except Exception:  # noqa: BLE001
        return None


def _read_lock(path: Path) -> Optional[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _lock_is_stale(path: Path, info: Optional[dict]) -> bool:
    """Gehoert die Sperre keinem laufenden Lauf mehr?  Im Zweifel (fremder Rechner, unklar) -> nicht verwaist."""
    if info is None:
        try:  # leer/unlesbar: ein gerade erst angelegter Lauf schreibt gleich hinein
            return time.time() - path.stat().st_mtime > 10
        except OSError:
            return True
    try:
        pid = int(info.get("pid", 0))
        started = float(info.get("start", 0))
    except (TypeError, ValueError):
        return True
    host = str(info.get("host", ""))
    try:
        here = socket.gethostname()
    except OSError:
        here = ""
    if host and here and host != here:
        return False  # anderer Rechner (Netzlaufwerk): PID nicht pruefbar
    if not _pid_alive(pid):
        return True
    saved = info.get("pstart")
    if saved is not None:  # PID gibt es noch - ist es derselbe Prozess wie damals (oder eine Wiederverwendung)?
        cur = _proc_start_time(pid)
        if cur is not None:
            return cur != saved  # gleiche Startzeit = aktiv (auch bei Uhrsprung), andere = PID wiederverwendet
    # Ohne (ermittelbare) Prozess-Startzeit: Sperre aelter als der letzte Systemstart -> verwaist
    boot = _boot_time()
    if boot is not None and started and started < boot - 5:
        return True
    return False


def acquire_lock(target_dir: Path) -> Path:
    """Sperrdatei im Zielordner anlegen (O_EXCL, PID + Startzeit).  Wirft TargetBusy, wenn ein anderer
    Lauf aktiv ist; eine verwaiste Sperre (Prozess existiert nicht mehr) wird uebernommen."""
    path = Path(target_dir) / LOCK_NAME
    payload = json.dumps({"pid": os.getpid(), "start": time.time(), "host": socket.gethostname(),
                          "pstart": _proc_start_time(os.getpid())})
    if path.is_dir():
        raise TargetBusy(f"'{path}' ist ein Ordner und keine Sperrdatei - bitte umbenennen oder loeschen.")
    for _ in range(4):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            info = _read_lock(path)
            if not _lock_is_stale(path, info):
                who = f" (PID {info.get('pid')})" if info and info.get("pid") else ""
                raise TargetBusy(
                    f"Der Zielordner wird bereits von einem anderen Lauf benutzt{who}. "
                    f"Falls sicher kein anderer Lauf aktiv ist, die Datei '{path}' loeschen."
                ) from None
            try:
                if _read_lock(path) == info:  # nur unveraendert entfernen
                    path.unlink()
            except FileNotFoundError:
                pass
            except OSError as e:
                raise TargetBusy(f"Verwaiste Sperre '{path}' konnte nicht entfernt werden: {e}") from e
            continue
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(payload)
        except BaseException:  # z. B. Platte voll: keine leere Sperrdatei zuruecklassen
            try:
                path.unlink()
            except OSError:
                pass
            raise
        return path
    raise TargetBusy("Der Zielordner wird bereits von einem anderen Lauf benutzt (Sperre konnte nicht uebernommen werden).")


def release_lock(path: Optional[Path]) -> None:
    if path is None:
        return
    info = _read_lock(path)
    if info is not None and info.get("pid") not in (None, os.getpid()):
        return  # gehoert nicht uns
    try:
        path.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- Einstellungen (Fortsetzen)
_SETTING_LABELS = {
    "bitrate_mbps": "Bitrate (Mbit/s)", "preset": "Preset", "sony_header": "Kopfdaten-Angleichung",
    "embed_xml": "XML einbetten", "keep_anc": "ANC uebernehmen", "ffmpeg": "ffmpeg", "raw2bmx": "raw2bmx",
}
_VERSIONS: Optional[dict] = None


def tool_versions() -> dict:
    """Versionszeilen von ffmpeg und raw2bmx (best effort, einmal je Prozess; '?' = unbekannt)."""
    global _VERSIONS
    if _VERSIONS is None:
        v = {}
        for name, flag in (("ffmpeg", "-version"), ("raw2bmx", "--version")):
            try:
                r = tools.run_capture([tools.find_tool(name), flag], timeout=15)
                lines = [l.strip() for l in (r.stdout or r.stderr).splitlines() if l.strip()]
                v[name] = lines[0][:100] if lines else "?"
            except Exception:  # noqa: BLE001
                v[name] = "?"
        _VERSIONS = v
    return dict(_VERSIONS)


def diff_settings(old: dict, new: dict) -> list[str]:
    """Unterschiede zweier Einstellungs-Dicts (alt = beim Erzeugen, neu = aktuell) als lesbare Teile
    ('Bitrate (Mbit/s): damals 40, jetzt 50')."""
    out = []
    for k, label in _SETTING_LABELS.items():
        a, b = old.get(k), new.get(k)
        if a is None or b is None or a == "?" or b == "?":
            continue
        if a != b:
            out.append(f"{label}: damals {a}, jetzt {b}")
    return out


# --------------------------------------------------------------------------- Quell-Deskriptor
def read_seq_offsets(path: Path) -> list[int]:
    """SequenceOffset der Sound-Deskriptoren der Quell-MXF (je Tonspur; fehlendes Item = 0).
    Nur lesend.  Wirft MxfParseError/HeaderPatchError/OSError, wenn nicht ermittelbar."""
    hm = mxfheader.read_header(path)
    st = mxfheader.resolve(hm)
    if st.file is None:
        raise mxfheader.HeaderPatchError("kein File Package")
    dmap = st.descriptor_by_track()
    out: list[int] = []
    for t in st.file.kind_tracks("sound"):
        d = dmap.get(id(t.set))
        if d is None:
            raise mxfheader.HeaderPatchError("Sound-Deskriptor nicht zuordenbar")
        v = hm.get(d, "WaveAudioDescriptor.SequenceOffset")
        out.append(int.from_bytes(v, "big") if v else 0)
    return out


def read_kag_size(path: Path) -> int:
    """KAG-Size der Quell-MXF (aus dem ersten Partition Pack; nur lesend).  Wirft bei Fehlern."""
    parts = mxfheader.find_partitions(path)
    if not parts:
        raise mxfheader.MxfParseError("keine Partition gefunden")
    return int(parts[0].kag_size)


def read_audio_order(path: Path) -> list[int]:
    """Track-IDs der Sound-Tracks des Material Packages der Quell-MXF in der Reihenfolge des Tracks-Arrays
    (so nummeriert ffmpeg die Audiostreams ``0:a:i``).  Nur lesend.  Wirft, wenn nicht ermittelbar."""
    hm = mxfheader.read_header(path)
    st = mxfheader.resolve(hm)
    if st.material is None:
        raise mxfheader.HeaderPatchError("kein Material Package")
    return [t.track_id for t in st.material.kind_tracks("sound")]


def audio_stream_map(info: "mxfinfo.MxfInfo", order: list, n_probe_audio: Optional[int]) -> list[int]:
    """Zuordnung Tonspur j (Reihenfolge von mxf2raw/bmx = ``info.audio``) -> ffmpeg-Audioindex ``0:a:i``.

    ffmpeg nummeriert die Audiostreams in der Reihenfolge des Tracks-Arrays des Material Packages (``order``),
    mxf2raw/bmx sortiert nach Track-Nummer/-ID.  Die Zuordnung laeuft ueber die Material-Package-Track-ID.
    Wirft ValueError, wenn sie nicht eindeutig bestimmbar ist (nie raten)."""
    audio = info.audio
    n = len(audio)
    if n_probe_audio is not None and n_probe_audio != n:
        raise ValueError(f"ffprobe findet {n_probe_audio} Audiostreams, mxf2raw {n} Tonspuren")
    if len(order) != n:
        raise ValueError(f"{len(order)} Sound-Tracks im Header, mxf2raw meldet {n} Tonspuren")
    if len(set(order)) != len(order) or any(t is None for t in order):
        raise ValueError("Track-IDs der Tonspuren im Header sind nicht eindeutig")
    out: list[int] = []
    for j, a in enumerate(audio):
        tid = a.mp_track_id
        if tid is None or tid not in order:
            raise ValueError(f"Track-ID von Tonspur {j + 1} ({tid}) steht nicht im Material Package")
        out.append(order.index(tid))
    if len(set(out)) != len(out):
        raise ValueError("Tonspuren sind nicht eindeutig zuordenbar (doppelte Track-IDs)")
    return out


def level_from_probe(level) -> Optional[str]:
    """ffprobe-Level (``42``, ``'41'``) -> '4.2' / '4.1'."""
    try:
        n = int(str(level).strip())
    except (TypeError, ValueError):
        return None
    if n <= 0:
        return None
    return f"{n // 10}.{n % 10}"


def level_from_fps(fps) -> str:
    """Ziel-Level ohne Codec-String: bis einschliesslich 30 fps 4.1, darueber 4.2."""
    return "4.1" if fps is not None and fps <= 30 else "4.2"


def make_expect(level: Optional[str] = None, **kw) -> "verify.ClipExpect":
    """ClipExpect bauen; ``level`` nur setzen, wenn verify.ClipExpect das Feld kennt."""
    if level and "level" in {f.name for f in dataclasses.fields(verify.ClipExpect)}:
        kw["level"] = level
    return verify.ClipExpect(**kw)


@dataclass
class ClipPlan:
    mxf: Path
    stem: str
    material: Optional[card.Material]
    selected: bool = True
    skip_reason: str = ""
    info: Optional[mxfinfo.MxfInfo] = None
    decision: Optional[mxfinfo.SourceDecision] = None
    src_codec: str = ""
    new_codec: str = ""
    xml_path: Optional[Path] = None
    frames: int = 0  # zu erzeugende Frames
    notes: list = field(default_factory=list)
    codec_free: bool = False  # kein Codec-String (kein XML, kein MEDIAPRO-Eintrag); MXF ist eindeutig XAVC-I
    fallback_level: str = ""  # nur ohne Codec-String: Level aus der Bildrate ('4.1' bis 30 fps, sonst '4.2')
    seq_off: Optional[int] = None  # SequenceOffset der Quell-Tonspuren (None = unbekannt/0)
    kag: int = 0  # KAG-Size der Quell-MXF (0 = unbekannt)
    audio_map: list = field(default_factory=list)  # Tonspur j (mxf2raw-Reihenfolge) -> ffmpeg-Index 0:a:i
    error: str = ""  # Clip kann nicht umgewandelt werden (Quelle nicht lesbar, Zuordnung unklar): unveraendert kopieren, bei vorhandenem L-Ziel FEHLER

    @property
    def convert(self) -> bool:
        return bool(self.selected and not self.error and self.decision and self.decision.convert
                    and (self.new_codec or self.codec_free))

    @property
    def level(self) -> str:
        return card.level_from_codec(self.new_codec) or self.fallback_level or "4.2"

    @property
    def name(self) -> str:
        return self.mxf.name

    @property
    def rel(self) -> str:
        return f"Clip/{self.mxf.name}"


def _is_xavcl_info(info: Optional[mxfinfo.MxfInfo]) -> bool:
    return bool(info is not None and info.video is not None and info.video.essence_type == verify.XAVCL_ESSENCE_TYPE)


class Converter:
    def __init__(self, opts: Options, reporter: Optional[Reporter] = None, cancel: Optional[tools.CancelToken] = None):
        self.o = opts
        self.rep = reporter or Reporter()
        self.cancel = cancel or tools.CancelToken()
        self._logf = None
        self._t0 = time.time()
        self._frames_total = 0
        self._frames_done = 0
        self._frames_pending = 0  # Frames der Clips, die voraussichtlich noch transkodiert werden muessen
        self._work_frames_done = 0  # in DIESEM Lauf tatsaechlich transkodierte Frames (fertige Clips)
        self._work_secs_done = 0.0
        self._clip_t0 = 0.0
        self._src_root: Optional[Path] = None
        self._out_root: Optional[Path] = None
        self._mp_bytes: Optional[bytes] = None
        self._tgt_info: dict = {}  # name.casefold() -> MxfInfo | None (vorhandene Ziel-MXF; None = nicht lesbar)
        self.l_codecs: dict[str, str] = {}  # 'Clip/NAME.MXF' -> XAVC-L-Codec (Ist-Zustand des Ziels)
        self._settings_store: dict = {"version": 1, "clips": {}}
        self._settings_path: Optional[Path] = None
        self._resume_diffs: dict = {}
        self._log_lock = threading.Lock()  # der Temperatur-Thread schreibt ebenfalls ins Protokoll
        self._thermal: Optional[_thermal_mod.ThermalSession] = None
        self._thermal_closed = False
        self._thermal_summary: Optional[RunSummary] = None
        self._pause0_clip = 0.0
        self._abort_reason = ""  # gesetzt, wenn die Temperaturueberwachung den Lauf abbricht
        self._force: set = set()  # Clipnamen (casefold), die erzwungen neu umgewandelt werden (Wiederholung)
        self._progress_prefix = ""  # Vorsatz der Fortschrittstexte in der Wiederholungsrunde ("Wiederholung Runde 1/2: Clip 1/2 – ")

    # ------------------------------------------------------------------ Logging
    def _log(self, level: str, msg: str) -> None:
        self.rep.log(level, msg)
        with self._log_lock:
            if self._logf is not None:
                ts = _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                try:
                    self._logf.write(f"{ts} [{level}] {msg}\n")
                    self._logf.flush()
                except Exception:
                    pass

    def info(self, m: str) -> None:
        self._log("info", m)

    def detail(self, m: str) -> None:
        self._log("detail", m)

    def warn(self, m: str) -> None:
        self._log("warn", m)

    def error(self, m: str) -> None:
        self._log("error", m)

    # ------------------------------------------------------------------ Analyse
    def scan(self, src_root: Path, mp: card.MediaPro, mxfs: list[Path]) -> list[ClipPlan]:
        plans: list[ClipPlan] = []
        clip_files = sorted((src_root / "Clip").iterdir())
        total = len(mxfs)
        for i, mxf in enumerate(mxfs):
            self.cancel.check()
            plan = ClipPlan(mxf=mxf, stem=mxf.stem, material=mp.find(f"Clip/{mxf.name}"))
            pat = re.compile(re.escape(mxf.stem) + r"M\d\d\.XML$", re.I)
            for p in clip_files:
                if pat.match(p.name):
                    plan.xml_path = p
                    break
            xml_codec = ""
            if plan.xml_path is not None:
                try:
                    xml_codec = card.parse_clip_xml(plan.xml_path.read_bytes()).video_codec
                except Exception as e:  # noqa: BLE001
                    plan.notes.append(f"M01.XML nicht lesbar: {e}")
            mp_codec = plan.material.video_type if plan.material else ""
            plan.src_codec = xml_codec or mp_codec
            plan.new_codec = card.derive_xavcl_codec(plan.src_codec) or ""
            if xml_codec and mp_codec and xml_codec != mp_codec:
                plan.notes.append(f"videoCodec in M01.XML ({xml_codec}) und videoType in MEDIAPRO.XML ({mp_codec}) unterscheiden sich")
            if self.o.only and not any(t.casefold() in mxf.stem.casefold() for t in self.o.only):
                plan.selected = False
                plan.skip_reason = "Filter --only"
            elif plan.material is None and self.o.unlisted == "skip":
                plan.selected = False
                plan.skip_reason = "nicht in MEDIAPRO.XML gelistet (uebersprungen)"
            plans.append(plan)
            if not plan.selected:
                plan.notes = []
                continue
            self.rep.progress(mxf.name, i / max(1, total), 0.0, None, f"Analysiere Clip {i + 1}/{total}")
            try:
                plan.info = mxfinfo.read_mxf_info(str(mxf), cancel=self.cancel)
                probe = mxfinfo.run_ffprobe(str(mxf), cancel=self.cancel)
                plan.decision = mxfinfo.decide_source(plan.info, probe)
                n_probe_audio: Optional[int] = len(probe.audio)
            except tools.Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                # Quelle nicht lesbar: nie kopieren (ein defektes Original darf kein Ziel ersetzen), Clip = FEHLER
                plan.decision = mxfinfo.SourceDecision(False, f"Analyse fehlgeschlagen: {e}")
                first = (str(e).strip().splitlines() or ["?"])[0][:220]
                plan.error = f"Quelle nicht lesbar/analysierbar: {first}"
                continue
            if plan.decision.convert:
                if not plan.src_codec:
                    # K9: weder XML noch MEDIAPRO nennen einen Codec, die MXF ist aber eindeutig XAVC-I
                    plan.codec_free = True
                    plan.new_codec = ""
                    # Level aus der Bildrate (nicht aus ffprobe der Quelle: dort kann ein ungueltiges Level stehen)
                    plan.fallback_level = level_from_fps(plan.decision.fps)
                    plan.notes.append(
                        "kein Codec-String in M01.XML/MEDIAPRO.XML - Clip wird anhand der MXF als XAVC-I erkannt und umgewandelt "
                        f"(H.264-Level {plan.level} aus der Bildrate, kein XML anzupassen)"
                    )
                elif not plan.new_codec:
                    plan.decision = mxfinfo.SourceDecision(
                        False, f"unbekanntes Codec-Muster {plan.src_codec!r} - wird nicht geraten, Clip bleibt unveraendert"
                    )
                if plan.convert and (plan.info.duration is None or plan.info.duration <= 0):
                    # K8: ohne bekannte Dauer laesst sich weder --dur setzen noch das Ergebnis pruefen
                    plan.decision = mxfinfo.SourceDecision(
                        False, f"Dauer der Quelle unbekannt ({plan.info.duration}) - kann nicht umgewandelt werden")
                    plan.notes.append("ACHTUNG: " + plan.decision.reason + "; der Clip bleibt XAVC-I (wird nur kopiert)")
                if plan.convert:
                    if plan.material and plan.material.dur is not None and plan.material.dur != plan.info.duration:
                        plan.notes.append(f"MEDIAPRO dur {plan.material.dur} != MXF-Dauer {plan.info.duration}")
                    frames = plan.info.duration
                    plan.frames = min(frames, self.o.max_frames) if self.o.max_frames else frames
                    # K10: KAG-Size wie die Quelle (512 -> --kag-size-512)
                    try:
                        plan.kag = read_kag_size(mxf)
                    except Exception as e:  # noqa: BLE001
                        plan.notes.append(f"KAG-Size der Quelle nicht lesbar ({e}) - Standard (KAG 1)")
                    else:
                        if plan.kag not in (0, 1, 512):
                            plan.notes.append(f"KAG-Size {plan.kag} der Quelle ist nicht nachbildbar - Ziel erhaelt KAG 1")
                    # G1: Zuordnung Tonspur (mxf2raw-Reihenfolge) -> ffmpeg-Audiostream explizit herstellen
                    try:
                        plan.audio_map = audio_stream_map(plan.info, read_audio_order(mxf), n_probe_audio)
                    except Exception as e:  # noqa: BLE001
                        plan.error = (f"Ton-Zuordnung nicht bestimmbar (Tonspuren <-> ffmpeg-Audiostreams: {e}; "
                                      "Clip wird nicht umgewandelt, Gefahr vertauschter Tonspuren)")
                        plan.frames = 0
                        continue
                    # A2: SequenceOffset der Tonspuren (5-Bild-Folge bei 29.97/59.94) wie in der Quelle
                    try:
                        offs = read_seq_offsets(mxf)
                    except Exception as e:  # noqa: BLE001
                        plan.notes.append(f"SequenceOffset der Quelle nicht lesbar ({e}) - es wird kein --seq-off gesetzt")
                    else:
                        if len(set(offs)) > 1:
                            plan.decision = mxfinfo.SourceDecision(
                                False, f"Tonspuren mit unterschiedlichem SequenceOffset {offs} - wird nicht umgewandelt"
                            )
                            plan.frames = 0
                        elif offs and offs[0]:
                            plan.seq_off = offs[0]
        return plans

    # ------------------------------------------------------------------ Zielzustand
    def _scan_target(self, plans: list[ClipPlan], out_root: Path) -> None:
        """Vorhandene Ziel-MXF mit mxf2raw -i einlesen (Ist-Zustand fuer Heilung, Speicherschaetzung, Fortsetzen)."""
        self._tgt_info = {}
        todo = [p for p in plans if (out_root / "Clip" / p.mxf.name).is_file()]
        for i, p in enumerate(todo):
            self.cancel.check()
            self.rep.progress(p.name, i / max(1, len(todo)), 0.0, None, f"Pruefe vorhandenes Ziel {i + 1}/{len(todo)}")
            try:
                self._tgt_info[p.name.casefold()] = mxfinfo.read_mxf_info(str(out_root / "Clip" / p.mxf.name), cancel=self.cancel)
            except tools.Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                self._tgt_info[p.name.casefold()] = None
                self.detail(f"{p.name}: vorhandene Zieldatei nicht lesbar ({e})")

    def _tgt_is_l(self, plan: ClipPlan) -> bool:
        return _is_xavcl_info(self._tgt_info.get(plan.name.casefold()))

    def _tgt_done(self, plan: ClipPlan) -> bool:
        """Vorhandenes Ziel ist XAVC-L und passt in Dauer/Bildrate (wird beim Fortsetzen voraussichtlich nur geprueft,
        nicht neu erzeugt).  Sonst wird der Clip neu erzeugt (Speicher: neu + alte Datei)."""
        ti = self._tgt_info.get(plan.name.casefold())
        if not _is_xavcl_info(ti):
            return False
        if plan.info is not None and ti.duration != plan.frames:
            return False
        if plan.info is not None and plan.info.edit_rate and ti.edit_rate != plan.info.edit_rate:
            return False
        return True

    def _heal_target(self, plans: list[ClipPlan], out_root: Path) -> None:
        """Ist-Zustand herstellen: fuer jeden Clip, dessen Ziel-MXF existiert und XAVC-L ist, den neuen Codec in
        M01.XML und (spaeter) MEDIAPRO.XML eintragen - unabhaengig davon, in welchem Lauf er entstand."""
        n = 0
        for plan in plans:
            ti = self._tgt_info.get(plan.name.casefold())
            if not _is_xavcl_info(ti) or not plan.new_codec:
                continue
            want = self._expected_umid(plan)
            if want and ti.mp_uid and ti.mp_uid.upper() != want:
                continue  # gehoert nicht zu dieser Karte - nicht anfassen
            self.l_codecs[plan.rel] = plan.new_codec
            try:
                if self._write_clip_xml(plan, out_root / "Clip", plan.new_codec):
                    n += 1
                    self.info(f"{plan.name}: M01.XML an vorhandenen XAVC-L-Clip angeglichen")
            except OSError as e:
                self.warn(f"{plan.name}: M01.XML konnte nicht angeglichen werden: {e}")
        if self.l_codecs:
            self.info(f"{len(self.l_codecs)} bereits umgewandelte Clip(s) im Ziel gefunden")

    def _prewrite_clip_xml(self, plans: list[ClipPlan], out_root: Path) -> None:
        """Fehlt im Ziel die M01.XML eines ausgewaehlten Clips, die unveraenderte Quell-XML schreiben, damit auch
        bei Abbruch/Fehler/hartem Kill jeder Clip eine (zum Zustand 'noch XAVC-I' passende) XML hat.  Eine
        bereits vorhandene Ziel-XML (z. B. angepasst) wird nicht angeruehrt."""
        for plan in plans:
            if not plan.selected or plan.xml_path is None:
                continue
            d = out_root / "Clip" / plan.xml_path.name
            if d.exists():
                continue
            try:
                card.copy_file_atomic(plan.xml_path, d)
            except OSError as e:
                self.warn(f"{plan.xml_path.name}: konnte nicht kopiert werden: {e}")

    # ------------------------------------------------------------------ Einstellungen
    def _current_settings(self) -> dict:
        v = tool_versions()
        return {
            "bitrate_mbps": self.o.bitrate_mbps, "preset": self.o.preset, "sony_header": self.o.sony_header,
            "embed_xml": self.o.embed_xml, "keep_anc": self.o.keep_anc,
            "ffmpeg": v.get("ffmpeg", "?"), "raw2bmx": v.get("raw2bmx", "?"),
            # nur zur Information: steht NICHT in _SETTING_LABELS, loest also beim Fortsetzen keine Warnung aus
            "threads": self.o.threads,
        }

    def _load_settings(self, target_dir: Path) -> None:
        self._settings_path = target_dir / SETTINGS_NAME
        try:
            data = json.loads(self._settings_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except (OSError, ValueError) as e:
            self._quarantine_settings(f"nicht lesbar: {e}")
            return
        if isinstance(data, dict) and isinstance(data.get("clips"), dict):
            self._settings_store = data
        else:
            self._quarantine_settings("unerwarteter Aufbau")

    def _quarantine_settings(self, why: str) -> None:
        """Kaputte Einstellungsdatei nicht stillschweigend ueberschreiben: als ``.defekt`` sichern + Warnung."""
        path = self._settings_path
        if path is None:
            return
        bad = path.with_name(path.name + ".defekt")
        try:
            card.replace_retry(path, bad)
            self.warn(f"{SETTINGS_NAME} ist defekt ({why}); alte Datei als '{bad.name}' gesichert, es wird neu begonnen "
                      "(Angaben zu frueheren Einstellungen sowie zu weggelassener ANC/XML gehen verloren).")
        except OSError as e:
            self.warn(f"{SETTINGS_NAME} ist defekt ({why}) und konnte nicht gesichert werden: {e}")

    def _record_selection(self) -> None:
        """Auswahl dieses (letzten) Laufs festhalten, damit --verify bewusst nicht umgewandelte Clips erkennt."""
        self._settings_store["auswahl"] = {"unlisted": self.o.unlisted, "only": list(self.o.only or [])}
        self._save_settings()

    def _save_settings(self) -> None:
        if self._settings_path is None:
            return
        try:
            card.write_bytes_atomic(
                self._settings_path, json.dumps(self._settings_store, indent=1, ensure_ascii=False).encode("utf-8"))
        except OSError as e:
            self.warn(f"Einstellungsdatei konnte nicht geschrieben werden: {e}")

    def _record_clip_settings(self, plan: ClipPlan, **extra) -> None:
        """Einstellungen dieses Clips merken; ``extra`` (z. B. ``anc_dropped=True``) = bewusst Weggelassenes."""
        entry = self._current_settings()
        entry.update(extra)
        self._settings_store["clips"][plan.rel] = entry
        self._save_settings()

    def _update_clip_setting(self, plan: ClipPlan, key: str, value) -> None:
        """Einen einzelnen Wert im Einstellungseintrag eines schon fertigen Clips nachtragen (z. B. nach dem
        Nachziehen der Kopfdaten); legt keinen neuen Eintrag an."""
        entry = self._settings_store["clips"].get(plan.rel)
        if isinstance(entry, dict) and entry.get(key) != value:
            entry[key] = value
            self._save_settings()

    def _check_resume_settings(self, plan: ClipPlan) -> None:
        old = self._settings_store["clips"].get(plan.rel)
        if not isinstance(old, dict):
            return
        d = diff_settings(old, self._current_settings())
        if d:
            self._resume_diffs.setdefault(", ".join(d), []).append(plan.name)

    def _report_resume_diffs(self) -> None:
        for d, names in self._resume_diffs.items():
            shown = ", ".join(names[:5]) + (" ..." if len(names) > 5 else "")
            self.warn(
                f"Bereits fertige Clips ({len(names)}) wurden mit anderen Einstellungen erzeugt ({d}) "
                f"und werden nicht neu erzeugt: {shown}"
            )

    # ------------------------------------------------------------------ Hauptablauf
    def run(self) -> RunSummary:
        """Lauf durchfuehren.  Die Temperaturueberwachung (falls aktiv) wird in JEDEM Fall beendet und der
        Luefterzustand wiederhergestellt - auch bei Abbruch, Strg+C oder einer unerwarteten Ausnahme."""
        self._thermal_closed = False
        self._thermal = None
        self._abort_reason = ""
        self._keepawake_msgs = []
        # Energiesparmodus verhindern: Eintritt und Verlassen hier, im Thread, der den Lauf ausfuehrt (der Zustand gilt
        # je Thread); Testlaeufe (--max-frames) brauchen es nicht.
        guard = contextlib.nullcontext() if self.o.max_frames else keepawake.keep_awake(self._keepawake_msgs.append)
        try:
            with guard:
                return self._run()
        finally:
            self._thermal_stop()
            self._flush_keepawake()

    def _flush_keepawake(self) -> None:
        """Gepufferte Hinweise von keepawake ins Protokoll (die Protokolldatei ist beim Eintritt noch nicht offen)."""
        msgs, self._keepawake_msgs = self._keepawake_msgs, []
        for m in msgs:
            (self.warn if m.startswith("Hinweis") else self.info)(m)

    def _run(self) -> RunSummary:
        summary = RunSummary(test_run=bool(self.o.max_frames))
        self._thermal_summary = summary
        t0 = time.time()
        out_root: Optional[Path] = None
        tmp_root: Optional[Path] = None
        src_root: Optional[Path] = None
        target_dir: Optional[Path] = None
        lock_path: Optional[Path] = None
        touched = False  # erst wenn True, wird im Ziel etwas geschrieben (MEDIAPRO.XML im finally)
        clean_run = False
        self._resume_diffs = {}
        try:
            tools.check_tools()
            src_root = card.find_xdroot(self.o.source)
            target_dir = Path(self.o.target)
            out_root = card.check_target(src_root, target_dir, chosen_source=Path(self.o.source))
            target_dir.mkdir(parents=True, exist_ok=True)
            summary.out_root = out_root
            lock_path = acquire_lock(target_dir)
            mp_bytes = (src_root / "MEDIAPRO.XML").read_bytes()
            mp = card.parse_mediapro(mp_bytes)
            # G2: gehoert ein vorhandenes Ziel zu derselben Karte?  Sonst Abbruch vor jeder Aenderung.
            card.check_existing_target(src_root, out_root, mp)
            self._src_root, self._out_root, self._mp_bytes = src_root, out_root, mp_bytes
            self.l_codecs = {}
            self._tgt_info = {}

            summary.log_path = target_dir / LOG_NAME
            self._logf = open(summary.log_path, "a", encoding="utf-8")
            self.info(f"Start: Quelle {src_root} -> Ziel {out_root}")
            self._flush_keepawake()
            if self.o.max_frames:
                self.warn(f"TESTLAUF: nur die ersten {self.o.max_frames} Frames je Clip; dur/Duration werden NICHT angepasst")
            self.info(f"Einstellungen: Bitrate {self.o.bitrate_mbps} Mbit/s, Preset {self.o.preset}"
                      + (f", ffmpeg-Threads begrenzt auf {self.o.threads}" if self.o.threads else ""))
            ncpu = os.cpu_count()
            if self.o.threads and ncpu and self.o.threads >= ncpu:
                self.info(f"Hinweis: {self.o.threads} Threads sind nicht weniger als die {ncpu} CPU-Kerne dieses Rechners - "
                          "die Begrenzung begrenzt nichts (kein Fehler).")
            self._thermal_start()
            if not self.o.sony_header:
                self.warn("Kopfdaten-Angleichung ABGESCHALTET (--no-sony-header): Die Dateien tragen bmx-Kennungen "
                          "(BBC/bmx, Toolkit/Platform) und bmx-Track-IDs (1001/2001/...) statt der Sony-Kennungen der Quelle.")
            if self.o.bitrate_mbps != 50:
                self.warn("Bitrate ungleich 50 Mbit/s: Die Kennung 'AVC50' in den XML-Dateien entspricht dann nicht der Bitrate.")

            mxfs = card.list_clip_mxf(src_root)
            self.info(f"{len(mxfs)} Clips gefunden ({mp.system_kind or 'Kamera unbekannt'}), {len(mp.materials)} davon in MEDIAPRO.XML")

            self.info("Analysiere Clips ...")
            plans = self.scan(src_root, mp, mxfs)
            todo = [p for p in plans if p.convert]
            self._frames_total = sum(p.frames for p in todo) or 1
            for p in plans:
                # p.error (nicht analysierbar / Ton-Zuordnung unklar) wird erst bei der Verarbeitung gemeldet:
                # unveraendert kopiert (WARNUNG) bzw. FEHLER, falls ein umgewandelter Ziel-Clip existiert
                if p.selected and not p.error and not p.convert and p.decision is not None:
                    self.warn(f"{p.name}: wird nicht umgewandelt ({p.decision.reason}) - Datei wird unveraendert kopiert "
                              "(sofern kein umgewandelter Ziel-Clip existiert)")
                for n in p.notes:
                    self.warn(f"{p.name}: {n}")
            self.info(f"Umzuwandeln: {len(todo)} Clips, {self._frames_total} Frames")

            # Ist-Zustand des Ziels
            card.remove_stray_temp_files(out_root)
            self._scan_target(plans, out_root)
            marker = target_dir / verify.TEST_MARKER
            if self.o.max_frames and not marker.exists():
                done_l = [p.name for p in todo if self._tgt_is_l(p)]
                if done_l:
                    raise RuntimeError(
                        f"Der Zielordner enthaelt bereits vollstaendig umgewandelte Clips (z. B. {done_l[0]}), und es ist kein "
                        f"Testlauf. Ein --max-frames-Lauf wuerde sie mit gekuerzten Clips ueberschreiben. "
                        f"Bitte einen anderen Zielordner waehlen."
                    )
            self._frames_pending = sum(p.frames for p in todo if not self._tgt_done(p)) or 0

            self._check_disk_space(target_dir, plans)

            # --- ab hier wird im Ziel geschrieben
            touched = True
            tmp_root = target_dir / TMP_NAME
            self._shutil_clean(tmp_root)
            out_root.mkdir(parents=True, exist_ok=True)
            tmp_root.mkdir(parents=True, exist_ok=True)
            self._load_settings(target_dir)
            self._record_selection()

            skip_stems = {p.stem.casefold() for p in plans if not p.selected}
            self.info("Kopiere Nicht-MXF-Dateien ...")
            copy_errors: list = []
            n = card.copy_structure(src_root, out_root, skip_stems=skip_stems, cancel_check=self.cancel.check,
                                    errors=copy_errors)
            n += self._copy_orphan_xml(src_root, out_root, mxfs, copy_errors)
            self.info(f"{n} Dateien kopiert")
            summary.copy_errors = copy_errors
            for rel, msg in copy_errors:
                self.error(f"{rel}: konnte nicht kopiert werden: {msg}")
            if self.o.max_frames:
                card.write_bytes_atomic(
                    marker,
                    f"TESTAUSGABE - nur die ersten Frames je Clip, dur/Duration nicht angepasst\nmax_frames={self.o.max_frames}\n".encode("utf-8"),
                )
            # Ist-Zustand in die XML-Dateien uebertragen (M01.XML bereits umgewandelter Clips, MEDIAPRO.XML)
            self._heal_target(plans, out_root)
            self._prewrite_clip_xml(plans, out_root)
            self._write_mediapro()

            self._t0 = time.time()
            for plan in plans:
                self.cancel.check()
                res = ClipResult(name=plan.name, src_size=plan.mxf.stat().st_size, listed=plan.material is not None)
                if not plan.selected:
                    res.status = ST_NOT_SELECTED
                    res.notes.append(plan.skip_reason)
                    summary.results.append(res)
                    continue
                self._run_clip(plan, res, summary.results, src_root, out_root, tmp_root)
            summary.convert_seconds = time.time() - t0
            self._report_resume_diffs()
            clean_run = not any(r.status == ST_FAILED for r in summary.results) and not summary.copy_errors
            # K2: ein voller Lauf macht aus einer Testausgabe wieder eine normale Ausgabe (vor der Abschlusspruefung,
            # damit sie die Dauern nicht an der Testmarke misst)
            if clean_run and not self.o.max_frames and marker.exists():
                if any(not p.selected and (out_root / "Clip" / p.mxf.name).exists() for p in plans):
                    self.warn("Testmarker TESTAUSGABE.txt bleibt bestehen: nicht ausgewaehlte Clips im Ziel stammen evtl. aus einem Testlauf")
                else:
                    try:
                        marker.unlink()
                        self.info("Testmarker TESTAUSGABE.txt entfernt (voller Lauf, alle Clips neu geprueft)")
                    except OSError as e:
                        self.warn(f"Testmarker konnte nicht entfernt werden: {e}")
            if self.o.auto_verify:
                self._auto_verify(summary, plans, src_root, out_root, tmp_root)
        except tools.Cancelled:
            summary.cancelled = True
            summary.abort_reason = self._abort_reason
            self.warn("Abgebrochen. Bereits fertige Clips bleiben erhalten; ein erneuter Lauf setzt fort.")
        except Exception as e:  # noqa: BLE001
            summary.fatal = str(e)
            self.error(f"Abbruch wegen Fehler: {e}")
        finally:
            try:
                self._thermal_stop()  # Pause beenden, Luefter wiederherstellen; Zahlen fuer die Zusammenfassung
                # MEDIAPRO.XML immer passend zum tatsaechlichen Zustand (Ist-Zustand des Ziels) schreiben
                if touched:
                    try:
                        self._write_mediapro(final=True)
                    except Exception as e:  # noqa: BLE001
                        self.error(f"MEDIAPRO.XML konnte nicht geschrieben werden: {e}")
                if tmp_root is not None:
                    self._shutil_clean(tmp_root)
                    try:
                        tmp_root.rmdir()
                    except OSError:
                        pass
                if touched and out_root is not None:
                    card.remove_stray_temp_files(out_root)
                summary.seconds = time.time() - t0
                self.info(summary.text())
                if self._logf is not None:
                    try:
                        self._logf.close()
                    except Exception:
                        pass
                    self._logf = None
            finally:
                release_lock(lock_path)  # auch bei KeyboardInterrupt/anderen BaseExceptions freigeben
        return summary

    def _run_clip(self, plan: ClipPlan, res: ClipResult, results: list, src_root: Path, out_root: Path,
                  tmp_root: Path, replace: Optional[ClipResult] = None) -> ClipResult:
        """Einen Clip verarbeiten und das Ergebnis in ``results`` eintragen (``replace``: das alte Ergebnis dieses Clips
        wird ersetzt, z. B. bei der Wiederholung).  Bei Abbruch wird ``tools.Cancelled`` weitergereicht; ein neues Ergebnis
        steht dann nur ohne ``replace`` in ``results``."""
        t_clip = time.time()
        pause_clip0 = self.cancel.paused_time()
        dst = out_root / "Clip" / plan.mxf.name
        try:
            self._process_clip(plan, src_root, out_root, tmp_root, res)
        except tools.Cancelled:
            res.status = ST_CANCELLED
            res.dst_size = dst.stat().st_size if dst.exists() else 0
            if replace is None:
                results.append(res)
            raise
        except Exception as e:  # noqa: BLE001
            res.status = ST_FAILED
            res.foreign = isinstance(e, ForeignTarget)
            res.notes.append(str(e))
            self.error(f"{plan.name}: {e}")
            if replace is not None and not res.foreign:
                # gescheiterte Wiederholung: ersetzt wird nur nach erfolgreicher Neuerzeugung, ein vorhandener Ziel-Clip
                # blieb also unveraendert - target_ok aus dem tatsaechlichen Zustand
                res.retry_failed = True
                res.target_ok = dst.exists()
        res.seconds = time.time() - t_clip
        res.pause_seconds = max(0.0, self.cancel.paused_time() - pause_clip0)
        try:
            res.dst_size = dst.stat().st_size if dst.exists() else 0
        except OSError:
            res.dst_size = 0
        if replace is not None and replace in results:
            results[results.index(replace)] = res
        else:
            results.append(res)
        self.rep.clip_done(res)
        self._shutil_clean(tmp_root / plan.stem)
        return res

    # ------------------------------------------------------------------ Abschlusspruefung und Wiederholung
    def _auto_verify(self, summary: RunSummary, plans: list, src_root: Path, out_root: Path, tmp_root: Path) -> None:
        """Abschlusspruefung der Zielkarte gegen die Quelle (derselbe Code wie ``--verify --source [--deep]``), danach
        Wiederholung der fehlerhaften Clips (hoechstens ``retries`` Runden).  Die Sperre des Zielordners bleibt gehalten
        (laeuft innerhalb von ``_run``); Abbruch wirkt als ``tools.Cancelled``."""
        retries = max(0, min(AUTO_RETRIES_MAX, int(self.o.retries or 0)))
        out = VerifyOutcome(deep=bool(self.o.auto_verify_deep), retries=retries)
        summary.verify = out
        if self.o.max_frames:
            out.skipped = "entfällt bei einer Testausgabe (--max-frames)"
            self.info("Abschlussprüfung " + out.skipped)
            return
        out.started = True
        t_v0 = time.time()
        if (Path(self.o.target) / verify.TEST_MARKER).exists():
            # M1: eine stehengebliebene Testmarke (frueherer --max-frames-Lauf) liesse die Pruefung gekuerzte Dauern
            # erwarten und alle Clips sinnlos neu umwandeln: keine Pruefung, keine Wiederholung
            msg = (f"Im Ziel liegt eine Testausgabe ({verify.TEST_MARKER}) – Karte nicht geprüft; "
                   "Zielordner leeren oder neuen Zielordner wählen")
            out.complete = True
            out.deviations = [msg]
            out.seconds = time.time() - t_v0
            self.warn("Abschlussprüfung: " + msg)
            return
        try:
            self._verify_rounds(summary, out, plans, src_root, out_root, tmp_root, retries)
            out.complete = True
        except tools.Cancelled:
            out.cancelled = True
            self.warn("Abschlussprüfung abgebrochen, Prüfung unvollständig.")
            raise
        except Exception as e:  # noqa: BLE001
            out.error = f"{type(e).__name__}: {e}"
            self.error(f"Abschlussprüfung nicht möglich: {out.error}")
        finally:
            out.seconds = time.time() - t_v0
            self._force = set()
            self._progress_prefix = ""

    def _verify_call(self, src_root: Path, out_root: Path, clips, card_wide: bool, label: str,
                     checked_names: Optional[set] = None):
        t_start = time.time()
        cur: list = [0, 0, ""]  # aktueller Clip: (i, n, Name)

        def progress(i: int, n: int, rel: str) -> None:
            cur[:] = [i, n, Path(rel).name]
            if checked_names is not None:
                checked_names.add(Path(rel).name.casefold())
            frac = (i - 1) / max(1, n)
            eta = (time.time() - t_start) / (i - 1) * (n - i + 1) if i > 1 else None
            self.rep.progress(Path(rel).name, frac, frac, eta, f"Abschlussprüfung{label}: Clip {i}/{n}")

        def stage(text: str) -> None:
            # Teilschritt (Dekodieren, vollstaendiger Tonvergleich, Begleitdateien): die Anzeige friert nicht ein
            i, n, name = cur
            if text.startswith("Begleitdateien") or not i:
                self.rep.progress("", 1.0 if not i else (i - 1) / max(1, n), 1.0 if not i else (i - 1) / max(1, n), None,
                                  f"Abschlussprüfung{label}: Begleitdateien ...")
            else:
                frac = (i - 1) / max(1, n)
                self.rep.progress(name, frac, frac, None, f"Abschlussprüfung{label}: Clip {i}/{n} – {text}")

        return verify.verify_card(
            str(out_root), str(src_root), deep=bool(self.o.auto_verify_deep),
            log=lambda s: self.detail(str(s).strip()), cancel=self.cancel, threads=self.o.threads,
            clips=clips, card_wide=card_wide, progress=progress, stage=stage)

    def _verify_rounds(self, summary: RunSummary, out: VerifyOutcome, plans: list, src_root: Path, out_root: Path,
                       tmp_root: Path, retries: int) -> None:
        def norm(s: str) -> str:
            return str(s).replace("\\", "/").casefold()

        by_rel = {norm(p.rel): p for p in plans}
        by_name = {p.name.casefold(): p for p in plans}
        checked_names: set = set()  # Clips, die in irgendeiner Runde geprueft wurden (Endstand je Clip, K1)
        unlisted_deliberate: list = []  # nicht gelistete, bewusst nicht ausgewaehlte Quell-Clips (strukturiert aus verify)
        verify_notes: list = []  # Hinweise von verify_card: nur Detail im Protokoll
        foreign_names = {r.name.casefold() for r in summary.results if r.foreign}
        mode = "gründlich, jeder Clip komplett dekodiert" if out.deep else "schnell, ohne vollständiges Dekodieren"
        self.info(f"Abschlussprüfung der Zielkarte gegen die Quelle ({mode}; bis zu {retries} Wiederholungsrunde(n)) ...")
        clip_f: dict = {}  # Clipname (casefold) -> [Finding]  (Einzelpruefung des Clips)
        card_f: list = []  # kartenweite Befunde (inkl. fehlende Clips, Begleitdateien, XML)
        notes: list = []

        def plan_of(f) -> Optional[ClipPlan]:
            return by_rel.get(norm(f.clip or ""))

        def take_notes(rep) -> None:
            for n in rep.notes:
                if n != getattr(rep, "deliberate_note", "") and n not in verify_notes:
                    verify_notes.append(n)

        def absorb(rep, restricted) -> None:
            """Befunde eines verify_card-Aufrufs uebernehmen.  ``restricted``: Namen (casefold) der in diesem Aufruf
            einzeln geprueften Clips (None = erste, volle Runde)."""
            if restricted is None:
                clip_f.clear()
                card_f.clear()
            else:
                for nm in restricted:
                    clip_f.pop(nm, None)
                card_f[:] = [f for f in card_f if not (f.kind == "missing_listed" and f.clip
                                                       and Path(f.clip).name.casefold() in restricted)]
            for f in rep.findings:
                if f.kind in ("clip", "source"):
                    clip_f.setdefault(Path(f.clip or "").name.casefold(), []).append(f)
                else:
                    card_f.append(f)
            take_notes(rep)
            if restricted is None:
                unlisted_deliberate[:] = list(getattr(rep, "deliberate", []) or [])

        def failed_now() -> dict:
            """Clips, die in der Umwandlung als Fehler endeten und neu versucht werden duerfen: {Name casefold: Grund}."""
            d = {}
            for r in summary.results:
                p = by_name.get(r.name.casefold())
                if r.status == ST_FAILED and not r.foreign and p is not None and p.convert:
                    d[r.name.casefold()] = "Fehler in der Umwandlung: " + _first_line(r.notes[-1] if r.notes else "unbekannt")
            return d

        def retry_candidates() -> dict:
            """{Name casefold: Grund} aller Clips, die eine Neu-Umwandlung beheben kann."""
            cand = dict(failed_now())
            for nm, fl in clip_f.items():
                p = by_name.get(nm)
                good = [f for f in fl if f.kind == "clip"]
                if (p is not None and p.selected and p.convert and good and nm not in cand
                        and nm not in foreign_names):
                    cand[nm] = "Abweichung in der Prüfung: " + _first_line(good[0].message)
            for f in card_f:
                if f.kind in ("missing_listed", "missing_unlisted"):
                    p = plan_of(f)
                    if (p is not None and p.selected and p.convert and p.name.casefold() not in cand
                            and p.name.casefold() not in foreign_names):
                        cand[p.name.casefold()] = "Clip fehlt im Ziel"
            return cand

        # --- Runde 0: alles pruefen
        rep = self._verify_call(src_root, out_root, None, True, "", checked_names)
        absorb(rep, None)
        self.cancel.check()

        recopied: set = set()
        for rnd in range(1, retries + 1):
            cand = retry_candidates()
            comp = [f for f in card_f if f.kind == "companion" and f.path and f.path not in recopied]
            if not cand and not comp:
                break
            out.rounds = rnd
            self.info(f"Wiederholungsrunde {rnd}/{retries}: {len(cand)} Clip(s) neu umwandeln"
                      + (f", {len(comp)} Begleitdatei(en) neu kopieren" if comp else ""))
            t_r = time.time()
            for f in comp:
                recopied.add(f.path)
                if self._recopy_companion(src_root, out_root, f.path):
                    # K3: erfolgreich neu kopierte Datei ist kein Kopierfehler des Laufs mehr
                    summary.copy_errors[:] = [(r, m) for r, m in summary.copy_errors if norm(r) != norm(f.path)]
            redone: list = []
            todo = [p for p in plans if p.name.casefold() in cand]
            if todo:
                self._frames_total = sum(p.frames for p in todo) or 1
                self._frames_pending = self._frames_total
                self._frames_done = 0
                self._work_frames_done = 0
                self._work_secs_done = 0.0
                for k, p in enumerate(todo, 1):
                    self.cancel.check()
                    self._progress_prefix = f"Wiederholung Runde {rnd}/{retries}: Clip {k}/{len(todo)} – "
                    reason = cand[p.name.casefold()]
                    self.info(f"{p.name}: Wiederholung in Runde {rnd} – {reason}")
                    prev = next((r for r in summary.results if r.name.casefold() == p.name.casefold()), None)
                    out.retried.setdefault(p.name, []).append(f"Runde {rnd}: {reason}")
                    self._force = {p.name.casefold()}
                    res = ClipResult(name=p.name, src_size=p.mxf.stat().st_size, listed=p.material is not None)
                    res.retry_reasons = list(prev.retry_reasons if prev else []) + [f"Runde {rnd}: {reason}"]
                    res = self._run_clip(p, res, summary.results, src_root, out_root, tmp_root, replace=prev)
                    self._force = set()
                    self._progress_prefix = ""
                    if res.status != ST_FAILED:
                        redone.append(p.name)
            out.retry_seconds += time.time() - t_r
            # erneut pruefen: nur die neu erzeugten Clips (die Befunde fehlgeschlagener Versuche bleiben stehen)
            if redone:
                names = {n.casefold() for n in redone}
                rep = self._verify_call(src_root, out_root, set(redone), False, " (Wiederholung)", checked_names)
                absorb(rep, names)
        if out.rounds:
            # am Ende einmal die kartenweiten Punkte (MEDIAPRO.XML, Begleitdateien) neu pruefen
            self.rep.progress("", 1.0, 1.0, None, "Abschlussprüfung (kartenweit): Begleitdateien ...")
            rep = self._verify_call(src_root, out_root, set(), True, " (kartenweit)")
            card_f[:] = [f for f in rep.findings if f.kind not in ("clip", "source")]
            take_notes(rep)
            unlisted_deliberate[:] = list(getattr(rep, "deliberate", []) or [])

        # --- Ergebnis einordnen.  Nur FEHLENDE, bewusst nicht ausgewaehlte Clips sind kein Fehler; ein vorhandener, nicht
        # ausgewaehlter Clip mit Abweichung bleibt eine Abweichung (wird aber nicht neu umgewandelt).
        deviations: list = []
        bad: list = []
        deliberate_listed: list = []
        deliberate_unlisted: list = []
        for nm, fl in clip_f.items():
            p = by_name.get(nm)
            for f in fl:
                msg = f.message
                if p is not None and not p.selected:
                    msg += (f" – nicht ausgewählt – nicht neu umgewandelt; ohne --only bzw. mit --only {p.stem} "
                            "erneut starten")
                elif f.kind == "source":
                    msg += " – Quelle nicht lesbar – Kartenleser/Karte prüfen und erneut starten"
                deviations.append(msg)
                if (p.name if p else nm) not in bad:
                    bad.append(p.name if p else nm)
        for f in card_f:
            p = plan_of(f)
            if f.kind in ("missing_listed", "missing_unlisted") and p is not None and not p.selected:
                (deliberate_listed if f.kind == "missing_listed" else deliberate_unlisted).append(p.name)
                continue
            deviations.append(f.message)
            if p is not None and p.name not in bad:
                bad.append(p.name)
        for r in summary.results:
            if r.status == ST_FAILED and r.name not in bad:
                bad.append(r.name)
        # B6: EINE Zeile zu den bewusst nicht ausgewaehlten Clips (nicht gelistete aus verify strukturiert uebernommen)
        unl = {Path(x).name.casefold(): Path(x).name for x in unlisted_deliberate}
        unl.update({n.casefold(): n for n in deliberate_unlisted})
        parts = []
        if deliberate_listed:
            parts.append(f"{len(set(deliberate_listed))} Clips aus MEDIAPRO.XML")
        if unl:
            parts.append(f"{len(unl)} nicht gelistete Clips")
        if parts:
            notes.append("Nicht ausgewählt (kein Fehler, nicht wiederholt): " + ", ".join(parts))
        # K1: Zaehlung aus dem Endstand je Clip ueber alle Runden
        bad_cf = {b.casefold() for b in bad}
        out.checked = len(checked_names)
        out.ok = len([n for n in checked_names if n not in bad_cf])
        out.deviations = deviations
        out.bad_clips = bad
        out.notes = notes
        for n in verify_notes:
            self.detail("Abschlussprüfung, Hinweis: " + n)
        for d in deviations:
            self.warn("Abschlussprüfung, Abweichung: " + d)
        for n in notes:
            self.info("Abschlussprüfung, Hinweis: " + n)
        if not deviations and not bad:
            self.info(f"Abschlussprüfung: {out.checked} Clips geprüft, alle ohne Abweichung"
                      + (f" ({len(out.retried)} Clip(s) nach Wiederholung)" if out.retried else ""))
        else:
            self.warn(f"Abschlussprüfung: {len(deviations)} Abweichung(en), endgültig fehlerhaft: {', '.join(bad) or '-'}")

    def _recopy_companion(self, src_root: Path, out_root: Path, rel: str) -> bool:
        """Eine Begleitdatei, die byte-gleich mit der Quelle sein muesste, einmal neu kopieren (nur innerhalb von
        Quelle/Ziel; MEDIAPRO.XML und Clip-XML schreibt der Ablauf selbst).  True = kopiert."""
        parts = Path(rel).parts
        if not parts or ".." in parts or Path(rel).is_absolute():
            return False
        src, dst = src_root / rel, out_root / rel
        try:
            if not src.is_file():
                return False
            dst.parent.mkdir(parents=True, exist_ok=True)
            card.copy_file_atomic(src, dst)
            self.info(f"{rel}: Begleitdatei neu kopiert")
            return True
        except OSError as e:
            self.warn(f"{rel}: Begleitdatei konnte nicht neu kopiert werden: {e}")
            return False

    # ------------------------------------------------------------------ Temperaturueberwachung
    def _thermal_start(self) -> None:
        """Messthread starten (best effort: ein Fehler hier ist nie ein Fehler der Umwandlung)."""
        if self.o.thermal != "auto":
            return
        try:
            cfg = _thermal_mod.ThermalConfig.from_limits(self.o.temp_fan, self.o.temp_pause,
                                                         max_pause_s=self.o.thermal_max_pause_s,
                                                         throttle=bool(self.o.thermal_throttle))
            self._thermal = _thermal_mod.ThermalSession(
                cfg, self.cancel, notify=self._thermal_notify, status=self._thermal_status,
                provider_factory=self.o.thermal_provider, interval_s=self.o.thermal_interval_s,
                on_abort=self._thermal_abort)
            self._thermal.start()
        except Exception as e:  # noqa: BLE001
            self._thermal = None
            self.warn("Die Temperaturüberwachung konnte nicht gestartet werden; die Umwandlung läuft unverändert.")
            self._log("detail", f"Temperaturüberwachung nicht gestartet: {type(e).__name__}: {e}")

    def _thermal_abort(self, reason: str) -> None:
        """Pause dauerte zu lange: Lauf sauber abbrechen wie bei einem Benutzer-Abbruch (fortsetzbar), mit eigenem Grund."""
        self._abort_reason = reason
        self._log("warn", reason)  # ein Abbruch durch die Überwachung ist kein Fehler (die Zusammenfassung zeigt "Fehler: 0")
        self.cancel.cancel()

    def _thermal_notify(self, level: str, msg: str) -> None:
        try:
            self._log(level, msg)
        except Exception:  # noqa: BLE001
            pass

    def _thermal_status(self, text: str, state: str) -> None:
        try:
            self.rep.thermal_status(text, state)
        except Exception:  # noqa: BLE001
            pass

    def _thermal_stop(self) -> None:
        """Idempotent, wirft nie.  Beendet den Messthread (setzt eine Pause fort, stellt die Luefter wieder her,
        beendet den Hilfsprozess) und uebertraegt die Zahlen in die Zusammenfassung."""
        sess = self._thermal
        if sess is None or self._thermal_closed:
            return
        self._thermal_closed = True
        try:
            sess.stop()
        except Exception as e:  # noqa: BLE001
            try:
                self.warn(f"Temperaturüberwachung konnte nicht sauber beendet werden: {e}")
            except Exception:  # noqa: BLE001
                pass
        try:
            st = sess.stats
            sm = self._thermal_summary
            if sm is not None:
                sm.pauses, sm.pause_seconds, sm.max_cpu_c = st.pauses, st.pause_seconds, st.max_cpu_c
                sm.throttle_steps, sm.throttle_seconds = st.throttle_steps, st.throttle_seconds
                sm.thermal_throttle = bool(self.o.thermal_throttle)
                if st.unavailable_reason:
                    sm.thermal_note = f"Temperatur nicht verfügbar/ausgefallen: {st.unavailable_reason}"
        except Exception:  # noqa: BLE001
            pass
        self._thermal_status("", "")

    def _clip_active_secs(self) -> float:
        """Sekunden seit Clip-Beginn OHNE Temperaturpausen (fuer die Restzeit-Schaetzung)."""
        return max(0.0, time.time() - self._clip_t0 - (self.cancel.paused_time() - self._pause0_clip))

    # ------------------------------------------------------------------ Hilfen
    @staticmethod
    def _shutil_clean(path: Path) -> None:
        for _ in range(5):
            try:
                if path.exists():
                    shutil.rmtree(path)
                return
            except OSError:
                time.sleep(0.5)

    def _copy_orphan_xml(self, src_root: Path, out_root: Path, mxfs: list[Path], errors: list) -> int:
        """Clip-XML ohne zugehoerige MXF (die copy_structure nicht kopiert) unveraendert uebernehmen."""
        stems = {m.stem.casefold() for m in mxfs}
        n = 0
        for p in sorted((src_root / "Clip").iterdir()):
            stem = card.clip_xml_stem(p.name)
            if stem is None or not p.is_file() or stem.casefold() in stems:
                continue
            d = out_root / "Clip" / p.name
            try:
                if not (d.exists() and card.same_file_stat(p, d)):
                    card.copy_file_atomic(p, d)
                n += 1
            except OSError as e:
                errors.append((f"Clip/{p.name}", str(e)))
        return n

    def _write_mediapro(self, final: bool = False) -> None:
        """MEDIAPRO.XML aus der Quelle + Ist-Zustand (``l_codecs``) atomar schreiben."""
        if self._mp_bytes is None or self._out_root is None or self._src_root is None:
            return
        new_mp, changed = card.patch_mediapro(self._mp_bytes, self.l_codecs)
        dst = self._out_root / "MEDIAPRO.XML"
        try:
            if dst.is_file() and dst.read_bytes() == new_mp:
                if final:
                    self.info(f"MEDIAPRO.XML angepasst ({len(changed)} videoType-Eintraege)")
                return
        except OSError:
            pass
        card.write_bytes_atomic(dst, new_mp, stat_from=self._src_root / "MEDIAPRO.XML")
        if final:
            self.info(f"MEDIAPRO.XML angepasst ({len(changed)} videoType-Eintraege)")

    def _write_clip_xml(self, plan: ClipPlan, dst_dir: Path, new_codec: Optional[str]) -> bool:
        """Clip-XML ins Ziel schreiben (atomar): mit ``new_codec`` gepatcht, sonst unveraendert aus der Quelle.
        True, wenn die Datei geschrieben wurde (nicht, wenn sie schon identisch war)."""
        if plan.xml_path is None:
            return False
        data = plan.xml_path.read_bytes()
        if new_codec:
            data, changed = card.patch_clip_xml(data, new_codec)
            if not changed and card.parse_clip_xml(data).video_codec != new_codec:
                self.warn(f"{plan.xml_path.name}: videoCodec war nicht zu aendern")
        d = dst_dir / plan.xml_path.name
        try:
            if d.is_file() and d.read_bytes() == data:
                return False
        except OSError:
            pass
        card.write_bytes_atomic(d, data, stat_from=plan.xml_path)
        return True

    def _check_disk_space(self, target_dir: Path, plans: list[ClipPlan]) -> None:
        need = 0
        biggest = 0
        for p in plans:
            if not p.selected:
                continue
            if p.convert:
                if self._tgt_done(p):
                    continue  # schon im Ziel (wird nur geprueft)
                est = estimate_clip_bytes(p.frames, p.info.edit_rate, len(p.info.audio), self.o.bitrate_mbps)
                old = (self._out_root or target_dir) / "Clip" / p.mxf.name
                try:
                    if old.is_file():
                        est += old.stat().st_size  # die alte Datei bleibt bis zum Ersetzen liegen
                except OSError:
                    pass
            else:
                dst = (self._out_root or target_dir) / "Clip" / p.mxf.name
                if dst.exists() and card.same_file_stat(p.mxf, dst):
                    continue
                est = p.mxf.stat().st_size
                try:
                    if dst.is_file():
                        est += dst.stat().st_size  # die alte Datei bleibt bis zum Ersetzen liegen
                except OSError:
                    pass
            need += est
            biggest = max(biggest, est)  # temporaerer Platz fuer den groessten noch offenen Clip
        need = int((need + biggest) * 1.05) + 200 * 1024 * 1024
        free = shutil.disk_usage(target_dir).free
        self.info(f"Speicherbedarf geschaetzt {fmt_size(need)}, frei {fmt_size(free)}")
        if free < need:
            raise RuntimeError(
                f"Zu wenig freier Speicher im Zielordner: benoetigt ca. {fmt_size(need)}, frei {fmt_size(free)}."
            )

    def _emit_progress(self, plan: ClipPlan, clip_frac: float, text: str = "", transcoded: Optional[int] = None) -> None:
        """``transcoded``: bisher in diesem Clip transkodierte Frames (nur dann gibt es eine Restzeit; sie
        beruht allein auf der Geschwindigkeit der in DIESEM Lauf tatsaechlich transkodierten Frames)."""
        clip_frac = max(0.0, min(1.0, clip_frac))
        done = self._frames_done + (clip_frac * plan.frames if plan.convert else 0)
        total = done / self._frames_total if self._frames_total else 0.0
        eta = None
        if transcoded is not None:
            work_frames = self._work_frames_done + transcoded
            work_secs = self._work_secs_done + self._clip_active_secs()
            if work_secs > 5 and work_frames > 0:
                remaining = max(0, self._frames_pending - work_frames)
                eta = remaining / (work_frames / work_secs)
        self.rep.progress(plan.name, clip_frac, min(1.0, total), eta,
                          (self._progress_prefix + text) if self._progress_prefix else text)

    # ------------------------------------------------------------------ Ein Clip
    def _expected_umid(self, plan: ClipPlan) -> str:
        """Erwartete Material-Package-UMID des Clips (Quell-MXF, sonst MEDIAPRO.XML, sonst M01.XML); '' = unbekannt."""
        if plan.info is not None and plan.info.mp_uid:
            return plan.info.mp_uid.upper()
        if plan.material is not None and plan.material.umid:
            return plan.material.umid.upper()
        if plan.xml_path is not None:
            try:
                return card.parse_clip_xml(plan.xml_path.read_bytes()).umid_ref.upper()
            except Exception:  # noqa: BLE001
                pass
        return ""

    def _target_belongs(self, plan: ClipPlan) -> bool:
        """Gehoert die vorhandene (lesbare) Ziel-MXF zu diesem Clip?  Unbekannte UMID zaehlt als 'gehoert dazu'
        (im Zweifel nichts ueberschreiben)."""
        ti = self._tgt_info.get(plan.name.casefold())
        if ti is None:
            return False
        want = self._expected_umid(plan)
        return not (want and ti.mp_uid and ti.mp_uid.upper() != want)

    def _check_existing_umid(self, plan: ClipPlan, dst: Path) -> None:
        """G2b: Eine vorhandene Ziel-MXF, die nicht zu diesem Clip gehoert (UMID), wird nie ueberschrieben."""
        ti = self._tgt_info.get(plan.name.casefold())
        if ti is None:
            return  # nicht lesbar: gilt als beschaedigte eigene Ausgabe, wird erst nach erfolgreicher Neuerzeugung ersetzt
        want = self._expected_umid(plan)
        if want and ti.mp_uid and ti.mp_uid.upper() != want:
            raise ForeignTarget(
                f"Im Ziel liegt bereits eine andere (fremde) Datei mit diesem Namen (Material-Package-UMID {ti.mp_uid} statt {want}) - "
                "wird nicht ueberschrieben. Bitte einen anderen Zielordner waehlen."
            )

    @staticmethod
    def _fsync_file(path: Path) -> None:
        """Datei auf den Datentraeger zwingen (best effort), bevor sie die fertige Zieldatei ersetzt."""
        try:
            with open(path, "r+b") as f:
                os.fsync(f.fileno())
        except OSError:
            pass

    @staticmethod
    def _anc_xml_related(probs: list) -> bool:
        """Stammt mindestens ein Pruefbefund von ANC bzw. eingebettetem XML (nur dann hilft der Rueckfall ohne sie)?"""
        return any(re.search(r"\bANC\b|\bXML\b", str(p), re.I) for p in probs)

    def _process_clip(self, plan: ClipPlan, src_root: Path, out_root: Path, tmp_root: Path, res: ClipResult) -> None:
        dst_dir = out_root / "Clip"
        dst = dst_dir / plan.mxf.name
        res.notes.extend(plan.notes)
        tgt_l = self._tgt_is_l(plan)

        warn_copy = ""
        if plan.error:
            # Quelle nicht analysierbar / Tonzuordnung unklar: nie umwandeln.  Ohne umgewandelten Ziel-Clip wird die Datei
            # unveraendert kopiert (WARNUNG, kein Fehler); ein vorhandenes Ziel bleibt in jedem Fall unberuehrt (FEHLER).
            belongs = bool(self._target_belongs(plan))
            if tgt_l:
                res.target_ok = belongs
                msg = f"{plan.error} - Clip wird nicht umgewandelt und nicht kopiert."
                if belongs:
                    raise RuntimeError(msg + " Der vorhandene umgewandelte Ziel-Clip bleibt unveraendert erhalten.")
                raise ForeignTarget(msg + " Im Ziel liegt eine fremde XAVC-L-Datei - nichts ueberschrieben.")
            if plan.info is None and dst.exists() and not card.same_file_stat(plan.mxf, dst):
                # unlesbare Quelle darf nie eine andere vorhandene Zieldatei ersetzen
                res.target_ok = belongs
                msg = f"{plan.error} - im Ziel liegt bereits eine andere Datei dieses Namens, sie bleibt unveraendert."
                if belongs:
                    raise RuntimeError(msg)
                raise ForeignTarget(msg)
            warn_copy = f"nicht analysierbar, unveraendert kopiert: {plan.error}"
            self.warn(f"{plan.name}: {warn_copy}")
            res.notes.append("WARNUNG: " + warn_copy)

        if not plan.convert:
            if tgt_l and not _is_xavcl_info(plan.info):
                # G1: ein fertiger XAVC-L-Ziel-Clip wird nie durch die (XAVC-I-)Quelle ueberschrieben
                self._check_existing_umid(plan, dst)
                res.target_ok = True
                raise RuntimeError(
                    "Quelle wird jetzt nicht (mehr) umgewandelt "
                    f"({plan.decision.reason if plan.decision else 'nicht analysierbar'}); der vorhandene umgewandelte "
                    "XAVC-L-Ziel-Clip bleibt erhalten und wird NICHT durch die Quelle ueberschrieben.")
            if dst.exists() and card.same_file_stat(plan.mxf, dst):
                self.info(f"{plan.name}: unveraendert kopierter Clip ist schon im Ziel - uebersprungen")
                res.status = ST_COPIED
                res.notes.append("bereits im Ziel vorhanden (Groesse und Zeitstempel gleich)")
                self._write_clip_xml(plan, dst_dir, None)
                return
            if dst.exists():
                self._check_existing_umid(plan, dst)
            self.info(f"{plan.name}: kopiere unveraendert ({plan.error or (plan.decision.reason if plan.decision else 'nicht analysierbar')})")
            tmpd = tmp_root / plan.stem
            tmpd.mkdir(parents=True, exist_ok=True)
            part = tmpd / "copy.mxf"
            self._copy_cancelable(plan.mxf, part, plan)
            card.copystat_writable(plan.mxf, part)
            card.replace_retry(part, dst)
            self.l_codecs.pop(plan.rel, None)  # das Ziel ist jetzt die unveraenderte Quelle, kein XAVC-L
            self._write_clip_xml(plan, dst_dir, None)
            res.status = ST_COPIED
            res.frames = 0
            if plan.decision and not plan.error:
                res.notes.append(plan.decision.reason)
            return

        info = plan.info
        assert info is not None and plan.decision is not None and plan.decision.fps is not None
        fps = plan.decision.fps
        frames = plan.frames
        res.frames = frames

        # Einstellungen/Befunde des fruehren Laufs fuer diesen Clip (Best-Effort-Verluste von ANC/XML)
        stored = self._settings_store["clips"].get(plan.rel)
        stored = stored if isinstance(stored, dict) else {}
        want_anc = bool(self.o.keep_anc and info.anc_tracks)
        want_xml = self._want_embed(info, plan)
        anc_dropped = bool(stored.get("anc_dropped")) and want_anc
        xml_dropped = bool(stored.get("xml_dropped")) and want_xml

        def mk_exp(anc: bool, xml: bool) -> "verify.ClipExpect":
            return make_expect(
                level=plan.level,
                frames=frames, mp_uid=info.mp_uid, fp_uid=info.fp_uid, start_tc=info.start_tc,
                n_audio=len(info.audio), fps=fps, expect_anc=anc, expect_xml=xml,
            )

        # Fortsetzen: was beim fruehen Lauf bewusst weggelassen wurde (ANC/XML), gilt nicht als Abweichung
        exp = mk_exp(want_anc and not anc_dropped, want_xml and not xml_dropped)
        src_arg = plan.mxf if self.o.sony_header else None

        # K1: --max-frames ersetzt nie einen vorhandenen Clip mit laengerer (voller) Dauer - unabhaengig vom Marker
        if self.o.max_frames and tgt_l and self._target_belongs(plan):
            tdur = self._tgt_info[plan.name.casefold()].duration
            if tdur is not None and tdur > frames:
                self.warn(f"{plan.name}: --max-frames-Lauf: vorhandener Ziel-Clip ({tdur} Frames) ist laenger als der "
                          f"Testlauf ({frames} Frames) und wird NICHT ersetzt")
                res.status = ST_SKIPPED_DONE
                res.notes.append(f"--max-frames: vorhandener Ziel-Clip mit {tdur} Frames nicht durch gekuerzten Clip ersetzt")
                self._frames_done += frames
                return

        # Wiederholung durch die Abschlusspruefung: ein vorhandener Ziel-Clip wird NICHT als fertig uebersprungen, sondern
        # neu erzeugt; ersetzt wird er (wie immer) erst nach erfolgreicher Neuerzeugung, die Fremd-Pruefung gilt weiter.
        forced = plan.name.casefold() in self._force
        if forced and dst.exists():
            self._check_existing_umid(plan, dst)

        # Fortsetzen: fertiger, verifizierter Ziel-Clip vorhanden?
        if dst.exists() and not forced:
            self._check_existing_umid(plan, dst)
            self._emit_progress(plan, 0.0, "Pruefe vorhandene Zieldatei ...")
            probs = verify.check_clip(dst, exp, cancel=self.cancel, source=src_arg, source_info=info)
            if probs and self.o.sony_header:
                # Weichen nur die Kopfdaten ab (z. B. Ausgabe einer aelteren Version), wird nachgezogen statt neu transkodiert
                if not verify.check_clip(dst, exp, cancel=self.cancel):
                    try:
                        self._adopt_header(plan, dst)
                        # danach die VOLLE Pruefung (nicht nur die Kopfdaten)
                        after = verify.check_clip(dst, exp, cancel=self.cancel, source=plan.mxf, source_info=info)
                    except _HEADER_ERRORS as e:
                        after = [f"Angleichen nicht moeglich: {e}"]
                    if after:
                        probs = ["Kopfdaten: " + after[0]]
                    else:
                        probs = []
                        self.info(f"{plan.name}: Kopfdaten nachgezogen (Datei nicht neu transkodiert)")
                        res.notes.append("Kopfdaten nachgezogen")
                        self._update_clip_setting(plan, "sony_header", True)
            if not probs:
                # K6: Ende der Datei dekodieren - ein Clip mit defektem Ende gilt nicht als fertig
                tprobs = verify.tail_decode_check(str(dst), frames, fps, cancel=self.cancel)
                if tprobs:
                    probs = ["Ende: " + tprobs[0]]
            if not probs:
                self.info(f"{plan.name}: bereits fertig und verifiziert - uebersprungen")
                if anc_dropped:
                    res.notes.append("ANC-Datenspur fehlt im Ziel (beim ersten Lauf nicht uebernehmbar, wird nicht erneut versucht)")
                    self.warn(f"{plan.name}: ANC-Spur fehlt im Ziel (Uebernahme war beim ersten Lauf nicht moeglich)")
                if xml_dropped:
                    res.notes.append("eingebettetes XML fehlt im Ziel (beim ersten Lauf nicht uebernehmbar, wird nicht erneut versucht)")
                    self.warn(f"{plan.name}: eingebettetes XML fehlt im Ziel (Uebernahme war beim ersten Lauf nicht moeglich)")
                if src_arg is not None:
                    # die Pruefung mit Quelle (inkl. Kopfdaten) bestanden: Eintrag nachtragen, falls ein Abbruch nach dem
                    # Nachziehen der Kopfdaten ihn nicht mehr schreiben konnte (sonst falsche Einstellungs-Warnung)
                    self._update_clip_setting(plan, "sony_header", True)
                self._finish_clip_files(plan, dst_dir)
                self._check_resume_settings(plan)
                res.status = ST_SKIPPED_DONE
                self._emit_progress(plan, 1.0, "bereits fertig")
                self._frames_done += frames
                return
            self.warn(f"{plan.name}: vorhandene Zieldatei unvollstaendig/abweichend ({probs[0]}) - wird neu erzeugt "
                      "(die alte Datei bleibt bis zum erfolgreichen Ersetzen bestehen)")

        # Neuerzeugung: ANC und XML werden (erneut) versucht
        expect_anc, embed = want_anc, want_xml
        exp = mk_exp(expect_anc, embed)
        self._clip_t0 = time.time()
        self._pause0_clip = self.cancel.paused_time()
        tmpd = tmp_root / plan.stem
        self._shutil_clean(tmpd)
        tmpd.mkdir(parents=True, exist_ok=True)
        self.info(f"{plan.name}: {frames} Frames, {float(fps):.3f} fps, {len(info.audio)} Audiospuren, GOP N={mxfinfo.gop_length(fps)}")
        self._emit_progress(plan, 0.0, "Starte Transkodierung ...", transcoded=0)

        # 1. Video + Audio transkodieren
        v_path = tmpd / "v.h264"
        wavs = [tmpd / f"a{i}.wav" for i in range(len(info.audio))]
        self._transcode(plan, fps, frames, v_path, wavs)
        self.cancel.check()

        # 2. ANC extrahieren (Best Effort)
        anc_file: Optional[Path] = None
        if expect_anc:
            anc_file = self._extract_anc(plan, tmpd)
            if anc_file is None:
                exp.expect_anc = False
        # 3. XML fuer Einbettung vorbereiten
        xml_file: Optional[Path] = None
        if embed:
            xml_file = self._prepare_embed_xml(plan, tmpd)
            if xml_file is None:
                exp.expect_xml = False

        # 4. Wrappen (Rueckfall bei Wrap-/ANC-/XML-Fehlern: erst ohne XML, dann ohne ANC)
        out_mxf = tmpd / "out.mxf"
        attempts = [(anc_file, xml_file)]
        if xml_file is not None:
            attempts.append((anc_file, None))
        if anc_file is not None:
            attempts.append((None, None))
        last_err = ""
        wrapped = False
        dropped: dict = {}
        for idx, (anc_f, xml_f) in enumerate(attempts):
            self.cancel.check()
            self._emit_progress(plan, 0.92, "Verpacke MXF (raw2bmx) ...", transcoded=frames)
            if out_mxf.exists():
                out_mxf.unlink()
            err = self._wrap(plan, fps, frames, v_path, wavs, out_mxf, anc_f, xml_f)
            if err:
                last_err = err
                self.warn(f"{plan.name}: raw2bmx fehlgeschlagen ({err.splitlines()[-1] if err else '?'}) - Versuch {idx + 1}/{len(attempts)}")
                continue
            exp.expect_anc = anc_f is not None
            exp.expect_xml = xml_f is not None
            if self.o.sony_header:
                self._emit_progress(plan, 0.95, "Gleiche MXF-Kopfdaten an die Quelle an ...", transcoded=frames)
                try:
                    self._adopt_header(plan, out_mxf)
                except _HEADER_ERRORS as e:
                    raise RuntimeError(f"MXF-Kopfdaten konnten nicht an die Quelle angeglichen werden: {e}") from e
            self._emit_progress(plan, 0.97, "Pruefe Ergebnis ...", transcoded=frames)
            probs = verify.check_clip(out_mxf, exp, cancel=self.cancel, source=src_arg, source_info=info)
            if probs:
                last_err = "; ".join(probs)
                if not self._anc_xml_related(probs):
                    # Befund hat mit ANC/XML nichts zu tun: ein Rueckfall ohne sie wuerde nur dasselbe Ergebnis liefern
                    self.warn(f"{plan.name}: Pruefung des Ergebnisses fehlgeschlagen: {last_err}")
                    break
                self.warn(f"{plan.name}: Pruefung des Ergebnisses fehlgeschlagen: {last_err} - Versuch {idx + 1}/{len(attempts)}")
                continue
            if forced and self.o.auto_verify_deep:
                # Wiederholung (erzwungene Neuerzeugung): gruendliche Pruefung der NEUEN Datei VOR dem Ersetzen
                # (komplett dekodieren, Ton jeder Spur vollstaendig gegen die Quelle); sonst bleibt der alte Clip
                self._emit_progress(plan, 0.98, "Pruefe Ergebnis gruendlich (Dekodieren, Ton) ...", transcoded=frames)
                dprobs = self._deep_check_new(plan, out_mxf, frames)
                if dprobs:
                    last_err = "; ".join(dprobs)
                    self.warn(f"{plan.name}: gruendliche Pruefung der neu erzeugten Datei fehlgeschlagen: {last_err} "
                              "- der vorhandene Clip wird nicht ersetzt")
                    break
            if forced and dst.exists() and ((expect_anc and anc_f is None) or (embed and xml_f is None)):
                # kein Rueckfall ohne ANC/eingebettetes XML, wenn der vorhandene Ziel-Clip diese Spur hat
                old_anc, old_xml = self._old_target_has(dst)
                lost = [n for n, want, got, had in (("ANC-Spur", expect_anc, anc_f, old_anc),
                                                     ("eingebettetes XML", embed, xml_f, old_xml))
                        if want and got is None and had]
                if lost:
                    last_err = (f"Neuerzeugung ohne {' und ohne '.join(lost)} - der vorhandene Clip hat sie und "
                                "bleibt unveraendert")
                    self.warn(f"{plan.name}: {last_err} - Versuch {idx + 1}/{len(attempts)}")
                    continue
            if expect_anc and anc_f is None:
                res.notes.append("ANC-Datenspur (Kamera-Metadaten) konnte nicht uebernommen werden und fehlt im Ziel")
                self.warn(f"{plan.name}: ANC-Spur fehlt im Ziel (Uebernahme nicht moeglich)")
                dropped["anc_dropped"] = True
            if embed and xml_f is None:
                res.notes.append("eingebettetes XML konnte nicht uebernommen werden")
                self.warn(f"{plan.name}: eingebettetes XML fehlt im Ziel")
                dropped["xml_dropped"] = True
            wrapped = True
            break
        if not wrapped:
            raise RuntimeError(f"MXF-Erzeugung/Verifikation fehlgeschlagen: {last_err}")

        # 5. Atomar ins Ziel (ersetzt eine abweichende alte Datei erst jetzt), danach XML und MEDIAPRO.XML
        for p in [v_path, *wavs]:
            try:
                p.unlink()
            except OSError:
                pass
        self._fsync_file(out_mxf)
        card.replace_retry(out_mxf, dst)
        self._finish_clip_files(plan, dst_dir)
        self._record_clip_settings(plan, **dropped)
        res.status = ST_CONVERTED
        self._work_frames_done += frames
        self._work_secs_done += self._clip_active_secs()
        self._emit_progress(plan, 1.0, "fertig")
        self._frames_done += frames
        self.info(f"{plan.name}: fertig ({fmt_size(plan.mxf.stat().st_size)} -> {fmt_size(dst.stat().st_size)})")

    def _deep_check_new(self, plan: ClipPlan, mxf: Path, frames: int) -> list:
        """Gruendliche Pruefung einer neu erzeugten Datei (vor dem Ersetzen): komplett dekodieren (jede Ausgabe von
        ffmpeg, Rueckgabewert != 0 oder falsche Bildzahl ist ein Befund) und Ton jeder Spur vollstaendig gegen die Quelle."""
        out: list = []
        n, errs = verify.deep_decode_check(str(mxf), cancel=self.cancel, threads=self.o.threads)
        if errs:
            out.append(f"Decodierfehler: {errs[:3]}")
        if n != frames:
            out.append(f"Decode ergab {n} Frames statt {frames}")
        out += verify.check_audio_full(mxf, plan.mxf, cancel=self.cancel, threads=self.o.threads)
        return out

    def _old_target_has(self, dst: Path) -> tuple:
        """(hat ANC-Spur, hat eingebettetes XML) der vorhandenen Ziel-MXF; unlesbar = (False, False)."""
        try:
            ti = mxfinfo.read_mxf_info(str(dst), cancel=self.cancel)
        except tools.Cancelled:
            raise
        except Exception:  # noqa: BLE001
            return False, False
        return bool(ti.anc_tracks), any(t.get("scheme_id") == verify.NRT_SCHEME_ID for t in ti.text_objects)

    def _finish_clip_files(self, plan: ClipPlan, dst_dir: Path) -> None:
        """Nach erfolgreichem Ersetzen/Pruefen der MXF: Clip-XML und MEDIAPRO.XML (in dieser Reihenfolge)."""
        if plan.new_codec:
            self._write_clip_xml(plan, dst_dir, plan.new_codec)
            self.l_codecs[plan.rel] = plan.new_codec
            self._write_mediapro()
        else:
            self._write_clip_xml(plan, dst_dir, None)

    def _adopt_header(self, plan: ClipPlan, mxf: Path) -> None:
        """Kopfdaten der (Ziel-)MXF an die Quell-MXF angleichen (Python-Patcher, Quelle nur lesend).
        Wirft HeaderPatchError / MxfParseError / OSError."""
        msgs = mxfheader.adopt_from_source(mxf, plan.mxf)
        changes = [m for m in msgs if not m.startswith("WARNUNG:")]
        self.info(f"{plan.name}: MXF-Kopfdaten an Quelle angeglichen ({len(changes)} Aenderungen)")
        for m in changes:
            self.detail(f"{plan.name}:   {m}")
        for m in msgs:
            if m.startswith("WARNUNG:"):
                self.warn(f"{plan.name}: {m[len('WARNUNG:'):].strip()}")

    def _want_embed(self, info: mxfinfo.MxfInfo, plan: ClipPlan) -> bool:
        if plan.xml_path is None or self.o.embed_xml == "never":
            return False
        if self.o.embed_xml == "always":
            return True
        return any(t.get("scheme_id") == verify.NRT_SCHEME_ID for t in info.text_objects)

    def _copy_cancelable(self, src: Path, dst: Path, plan: Optional[ClipPlan] = None) -> None:
        total = max(1, src.stat().st_size)
        done = 0
        last_emit = 0.0
        with open(src, "rb") as fi, open(dst, "wb") as fo:
            while True:
                self.cancel.check()
                buf = fi.read(16 * 1024 * 1024)
                if not buf:
                    break
                fo.write(buf)
                done += len(buf)
                now = time.time()
                if plan is not None and (now - last_emit > 0.5 or done >= total):
                    last_emit = now
                    self._emit_progress(plan, done / total, f"Kopiere {fmt_size(done)} / {fmt_size(total)}")
            fo.flush()
            try:
                os.fsync(fo.fileno())
            except OSError:
                pass

    def _prepare_embed_xml(self, plan: ClipPlan, tmpd: Path) -> Optional[Path]:
        try:
            data = plan.xml_path.read_bytes()  # type: ignore[union-attr]
            new = card.patch_clip_xml(data, plan.new_codec)[0] if plan.new_codec else data
            f = tmpd / "embed.xml"
            f.write_bytes(new)
            return f
        except Exception as e:  # noqa: BLE001
            self.warn(f"{plan.name}: XML zum Einbetten nicht vorbereitbar: {e}")
            return None

    # ------------------------------------------------------------------ ffmpeg
    def _transcode(self, plan: ClipPlan, fps: Fraction, frames: int, v_path: Path, wavs: list[Path]) -> None:
        n = mxfinfo.gop_length(fps)
        bitrate = f"{self.o.bitrate_mbps:g}M"
        level = plan.level
        x264 = (
            f"aud=1:nal-hrd=vbr:keyint={n}:min-keyint={n}:scenecut=0:b-pyramid=none:"
            "colorprim=bt709:transfer=bt709:colormatrix=bt709:chromaloc=0:force-cfr=1"
        )
        thr = ["-threads", str(int(self.o.threads))] if self.o.threads else []  # Schonmodus: Decoder + Encoder
        args = [
            tools.find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-nostats", "-v", "error",
            "-progress", "pipe:1",
            *thr, "-i", str(plan.mxf),
            "-map", "0:v:0", "-c:v", "libx264", "-profile:v", "high422", "-pix_fmt", "yuv422p10le",
            "-level:v", level, "-preset", self.o.preset, "-color_range", "tv",
            "-b:v", bitrate, "-maxrate", bitrate, "-bufsize", bitrate,
            "-g", str(n), "-bf", "2", "-fps_mode", "passthrough",
            "-x264-params", x264,
            *thr,
        ]
        if self.o.max_frames:
            args += ["-frames:v", str(frames)]
        args += ["-f", "h264", str(v_path)]
        secs = float(Fraction(frames) / fps)
        # G1: Tonspur j (Reihenfolge wie mxf2raw/bmx = info.audio[j]) kommt aus dem passenden ffmpeg-Audiostream
        amap = plan.audio_map if len(plan.audio_map) == len(wavs) else None
        if amap is None and wavs:
            raise RuntimeError("Zuordnung der Tonspuren fehlt - Clip wird nicht umgewandelt")
        for j, w in enumerate(wavs):
            args += ["-map", f"0:a:{amap[j]}", "-c:a", "pcm_s24le", "-rf64", "auto"]
            if self.o.max_frames:
                args += ["-t", f"{secs:.6f}"]
            args += [str(w)]

        last = [0]

        def on_line(line: str) -> None:
            if line.startswith("frame="):
                try:
                    last[0] = int(line.split("=", 1)[1].strip())
                except ValueError:
                    return
                self._emit_progress(plan, 0.9 * last[0] / max(1, frames), f"Transkodiere {last[0]}/{frames}", transcoded=last[0])

        res = tools.run_capture(args, cancel=self.cancel, on_stdout_line=on_line, keep_lines=300)
        if res.returncode != 0:
            tail = "\n".join(res.stderr.splitlines()[-6:])
            raise RuntimeError(f"ffmpeg fehlgeschlagen (Code {res.returncode}): {tail}")
        if res.stderr.strip():
            self.warn(f"{plan.name}: ffmpeg-Meldungen: {res.stderr.strip().splitlines()[-1]}")
        if last[0] != frames:
            raise RuntimeError(f"ffmpeg lieferte {last[0]} statt {frames} Frames")

    # ------------------------------------------------------------------ ANC
    def _extract_anc(self, plan: ClipPlan, tmpd: Path) -> Optional[Path]:
        info = plan.info
        assert info is not None
        data_idx = None
        k = 0
        for t in info.tracks:
            if t.kind == "Data":
                if t.essence_type == "ANC_Data":
                    data_idx = k
                    break
                k += 1
        if data_idx is None:
            return None
        self._emit_progress(plan, 0.9, "Extrahiere ANC-Daten ...", transcoded=plan.frames)
        prefix = str(tmpd / "anc")
        res = tools.run_capture(
            [tools.find_tool("mxf2raw"), "--ess-out", prefix, "--wrap-klv", "d",
             "--disable-audio", "--disable-video", str(plan.mxf)],
            cancel=self.cancel, keep_lines=50,
        )
        f = tmpd / f"anc_d{data_idx}.klv"
        if res.returncode != 0 or not f.is_file() or f.stat().st_size == 0:
            self.warn(f"{plan.name}: ANC-Extraktion fehlgeschlagen ({(res.stderr or res.stdout).strip()[-200:]}) - ANC wird weggelassen")
            return None
        return f

    # ------------------------------------------------------------------ raw2bmx
    def _wrap(
        self, plan: ClipPlan, fps: Fraction, frames: int, v_path: Path, wavs: list[Path],
        out: Path, anc: Optional[Path], xml: Optional[Path],
    ) -> str:
        """Gibt '' bei Erfolg zurueck, sonst die Fehlermeldung."""
        info = plan.info
        assert info is not None
        args = [
            tools.find_tool("raw2bmx"), "-t", "op1a", "-o", str(out),
            "--head-fill", "65536",  # Reserve (KLV-Fill) am Ende der Kopfdaten fuer die spaetere Angleichung
            "-f", mxfinfo.rate_arg(fps),
            "-y", info.start_tc or "00:00:00:00",
            "--mp-uid", info.mp_uid, "--fp-uid", info.fp_uid,
            "--dur", str(frames),
            "--aes-3",  # Audio-Mapping wie Sony (ec_label ...02060300, Track-Nummern 0x16NN03xx)
        ]
        if plan.kag == 512:
            args += ["--kag-size-512"]  # KAG wie die Quelle
        if plan.seq_off:
            args += ["--seq-off", str(plan.seq_off)]  # SequenceOffset der Tonspuren wie in der Quelle (29.97/59.94)
        if self.o.sony_header:
            args += prod_info_args(info)
        if self.o.set_create_date:
            cd = mxf_date_to_arg(info.ident_modified)
            if cd:
                args += ["--create-date", cd]
        if xml is not None:
            args += ["--xml-scheme-id", verify.NRT_SCHEME_ID, "--xml-lang", "und", "--embed-xml", str(xml)]
        v = info.video
        assert v is not None
        sig = _SIGNAL_STD.get(v.descriptor.get("signal_standard", "").split(" ")[0], "st274")
        siting = _COLOR_SITING.get(v.descriptor.get("color_siting", "").split(" ")[0], "cositing")
        args += ["--signal-std", sig, "--color-siting", siting, "--avc_high_422", str(v_path)]
        for a, w in zip(info.audio, wavs):
            locked = a.descriptor.get("locked", "true").lower()
            args += ["--locked", "true" if locked == "true" else "false"]
            ref = a.descriptor.get("audio_ref_level")
            if ref is not None and re.match(r"^-?\d+$", ref):
                args += ["--audio-ref", ref]
            args += ["--wave", str(w)]
        if anc is not None:
            args += ["--klv", "s", "--anc", str(anc)]
        lines: list[str] = []
        # Fortschritt beim Verpacken (17.11.2026): waehrend raw2bmx laeuft, die Groesse der entstehenden Datei abfragen
        inputs = [v_path, *wavs] + ([anc] if anc is not None else []) + ([xml] if xml is not None else [])
        stop = threading.Event()
        watcher = threading.Thread(target=self._wrap_watch, args=(plan, frames, out, inputs, stop), daemon=True,
                                   name="wrap-progress")
        watcher.start()
        try:
            res = tools.run_capture(args, cancel=self.cancel, on_stdout_line=lines.append, on_stderr_line=lines.append, keep_lines=100)
        finally:
            stop.set()
            watcher.join(timeout=2)
        if res.returncode != 0:
            msg = [l for l in (res.stdout + "\n" + res.stderr).splitlines() if l.strip()]
            return "raw2bmx Code %d: %s" % (res.returncode, " | ".join(msg[-4:]))
        return ""

    # Fortschritt beim Verpacken: Abfrageabstand (Sekunden), Anteil des Verpackens am Clip (0.92 bis 0.95 wie bisher)
    WRAP_POLL_S = 1.0
    WRAP_FRAC_FROM, WRAP_FRAC_TO = 0.92, 0.95

    @staticmethod
    def wrap_fraction(out_size: int, expected: int) -> float:
        """Anteil des Verpackens (0 bis 0.99) aus der Groesse der entstehenden Datei; 0 ohne brauchbare Erwartung."""
        if expected <= 0 or out_size <= 0:
            return 0.0
        return max(0.0, min(0.99, out_size / expected))

    def _wrap_watch(self, plan: ClipPlan, frames: int, out: Path, inputs: list, stop: threading.Event) -> None:
        """Thread waehrend raw2bmx: Groesse der entstehenden MXF im Verhaeltnis zur Summe der Eingabedateien als
        Clip-Fortschritt melden (hoechstens einmal je WRAP_POLL_S, nie rueckwaerts, hoechstens 99 %).  Jeder Fehler
        (Datei fehlt, gesperrt, Anzeige) wird ignoriert; Ablauf und Gesamtfortschritt-Gewichtung bleiben unveraendert."""
        try:
            expected = 0
            for f in inputs:
                try:
                    expected += os.stat(f).st_size
                except OSError:
                    pass
            last = -1
            while not stop.wait(self.WRAP_POLL_S):
                try:
                    frac = self.wrap_fraction(os.stat(out).st_size, expected)
                    pct = int(frac * 100)
                    if pct <= last:
                        continue
                    last = pct
                    self._emit_progress(plan, self.WRAP_FRAC_FROM + (self.WRAP_FRAC_TO - self.WRAP_FRAC_FROM) * frac,
                                        f"Verpacke MXF (raw2bmx) {pct} %", transcoded=frames)
                except Exception:  # noqa: BLE001
                    pass
        except Exception:  # noqa: BLE001
            pass


@dataclass
class CardOverview:
    root: Path
    system_kind: str = ""
    clips: int = 0
    to_convert: int = 0
    total_bytes: int = 0
    convert_bytes: int = 0
    est_target_bytes: int = 0
    frames: int = 0
    notes: list = field(default_factory=list)

    def text(self) -> str:
        t = (
            f"Kamera: {self.system_kind or 'unbekannt'} - {self.clips} Clips, davon {self.to_convert} umzuwandeln, "
            f"Gesamtgroesse {fmt_size(self.total_bytes)}, geschaetzte Zielgroesse der umgewandelten Clips "
            f"{fmt_size(self.est_target_bytes)} (statt {fmt_size(self.convert_bytes)})"
        )
        if self.notes:
            t += "\n" + "\n".join(self.notes)
        return t


def overview(opts: Options, cancel: Optional[tools.CancelToken] = None) -> CardOverview:
    """Schnelle Analyse der Quellkarte (fuer die GUI-Uebersicht)."""
    c = Converter(opts, cancel=cancel)
    src_root = card.find_xdroot(opts.source)
    mp = card.parse_mediapro((src_root / "MEDIAPRO.XML").read_bytes())
    mxfs = card.list_clip_mxf(src_root)
    plans = c.scan(src_root, mp, mxfs)
    ov = CardOverview(root=src_root, system_kind=mp.system_kind)
    unlisted = 0
    unchanged: dict = {}
    for p in plans:
        if not p.selected:
            continue
        ov.clips += 1
        size = p.mxf.stat().st_size
        ov.total_bytes += size
        if p.material is None:
            unlisted += 1
        if p.convert:
            ov.to_convert += 1
            ov.convert_bytes += size
            ov.frames += p.frames
            ov.est_target_bytes += estimate_clip_bytes(p.frames, p.info.edit_rate, len(p.info.audio), opts.bitrate_mbps)
        elif p.error:
            ov.notes.append(f"{p.name}: nicht analysierbar/umwandelbar, wird unveraendert kopiert - {p.error}")
        elif p.decision is not None:
            unchanged[p.decision.reason] = unchanged.get(p.decision.reason, 0) + 1
    for reason, n in unchanged.items():
        ov.notes.append(f"{n} Clip(s) bleiben unveraendert und werden nur kopiert: {reason}")
    if unlisted:
        ov.notes.append(f"{unlisted} Clip(s) liegen auf der Karte, stehen aber nicht in MEDIAPRO.XML.")
    return ov
