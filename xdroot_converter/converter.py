"""Ablaufsteuerung: Karte pruefen, Struktur kopieren, Clips umwandeln (ffmpeg + raw2bmx), XML anpassen."""

from __future__ import annotations

import dataclasses
import datetime as _dt
import json
import os
import re
import shutil
import socket
import sys
import time
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Optional

from . import card, mxfheader, mxfinfo, tools, verify

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


@dataclass
class ClipResult:
    name: str
    status: str = ""
    src_size: int = 0
    dst_size: int = 0
    frames: int = 0
    seconds: float = 0.0
    notes: list = field(default_factory=list)


@dataclass
class RunSummary:
    results: list = field(default_factory=list)
    cancelled: bool = False
    seconds: float = 0.0
    log_path: Optional[Path] = None
    out_root: Optional[Path] = None
    fatal: str = ""
    copy_errors: list = field(default_factory=list)  # [(relativer Pfad, Meldung)] beim Kopieren der Struktur

    def count(self, status: str) -> int:
        return sum(1 for r in self.results if r.status == status)

    @property
    def failed(self) -> int:
        """Fehlgeschlagene Clips plus nicht kopierbare Strukturdateien."""
        return self.count(ST_FAILED) + len(self.copy_errors)

    def incomplete_text(self) -> str:
        """Deutliche Warnung, wenn fehlgeschlagene Clips im Ziel fehlen bzw. unbrauchbar sind ('' wenn alles da)."""
        bad = [r for r in self.results if r.status == ST_FAILED]
        missing = [r.name for r in bad if not r.dst_size]
        broken = [r.name for r in bad if r.dst_size]
        parts = []
        if missing:
            parts.append(f"{len(missing)} Clip(s) fehlen: {', '.join(missing)}")
        if broken:
            parts.append(f"{len(broken)} Clip(s) im Ziel sind unbrauchbar/veraltet: {', '.join(broken)}")
        if not parts:
            return ""
        return ("ACHTUNG: Zielkarte unvollstaendig - " + "; ".join(parts)
                + " (die Clips stehen aber in MEDIAPRO.XML)")

    def text(self) -> str:
        lines = ["", "=== Zusammenfassung ==="]
        tot_src = tot_dst = 0
        for r in self.results:
            if r.status in (ST_NOT_SELECTED,):
                continue
            lines.append(
                f"{r.name}: {r.status}  {fmt_size(r.src_size)} -> {fmt_size(r.dst_size)}"
                + (f"  ({r.frames} Frames, {r.seconds:.0f} s)" if r.frames else "")
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
        lines.append(
            f"umgewandelt: {self.count(ST_CONVERTED)}, bereits fertig: {self.count(ST_SKIPPED_DONE)}, "
            f"kopiert: {self.count(ST_COPIED)}, Fehler: {self.failed}"
            + (", ABGEBROCHEN" if self.cancelled else "")
            + f" - Dauer {fmt_duration(self.seconds)}"
        )
        for rel, msg in self.copy_errors:
            lines.append(f"FEHLER beim Kopieren von {rel}: {msg}")
        inc = self.incomplete_text()
        if inc:
            lines.append(inc)
        if self.fatal:
            lines.append("FEHLER: " + self.fatal)
        return "\n".join(lines)


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
    boot = _boot_time()
    if boot is not None and started and started < boot - 5:
        return True  # Sperre stammt aus der Zeit vor dem letzten Systemstart
    return not _pid_alive(pid)


def acquire_lock(target_dir: Path) -> Path:
    """Sperrdatei im Zielordner anlegen (O_EXCL, PID + Startzeit).  Wirft TargetBusy, wenn ein anderer
    Lauf aktiv ist; eine verwaiste Sperre (Prozess existiert nicht mehr) wird uebernommen."""
    path = Path(target_dir) / LOCK_NAME
    payload = json.dumps({"pid": os.getpid(), "start": time.time(), "host": socket.gethostname()})
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
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
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

    @property
    def convert(self) -> bool:
        return bool(self.selected and self.decision and self.decision.convert and (self.new_codec or self.codec_free))

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

    # ------------------------------------------------------------------ Logging
    def _log(self, level: str, msg: str) -> None:
        self.rep.log(level, msg)
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
            except tools.Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                plan.decision = mxfinfo.SourceDecision(False, f"Analyse fehlgeschlagen: {e}")
                plan.notes.append(plan.decision.reason)
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
                if plan.convert:
                    if plan.material and plan.material.dur is not None and plan.material.dur != plan.info.duration:
                        plan.notes.append(f"MEDIAPRO dur {plan.material.dur} != MXF-Dauer {plan.info.duration}")
                    frames = plan.info.duration or 0
                    plan.frames = min(frames, self.o.max_frames) if self.o.max_frames else frames
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

    def _heal_target(self, plans: list[ClipPlan], out_root: Path) -> None:
        """Ist-Zustand herstellen: fuer jeden Clip, dessen Ziel-MXF existiert und XAVC-L ist, den neuen Codec in
        M01.XML und (spaeter) MEDIAPRO.XML eintragen - unabhaengig davon, in welchem Lauf er entstand."""
        n = 0
        for plan in plans:
            ti = self._tgt_info.get(plan.name.casefold())
            if not _is_xavcl_info(ti) or not plan.new_codec:
                continue
            if plan.material and plan.material.umid and ti.mp_uid != plan.material.umid:
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
        }

    def _load_settings(self, target_dir: Path) -> None:
        self._settings_path = target_dir / SETTINGS_NAME
        try:
            data = json.loads(self._settings_path.read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("clips"), dict):
                self._settings_store = data
        except (OSError, ValueError):
            pass

    def _save_settings(self) -> None:
        if self._settings_path is None:
            return
        try:
            card.write_bytes_atomic(
                self._settings_path, json.dumps(self._settings_store, indent=1, ensure_ascii=False).encode("utf-8"))
        except OSError as e:
            self.warn(f"Einstellungsdatei konnte nicht geschrieben werden: {e}")

    def _record_clip_settings(self, plan: ClipPlan) -> None:
        self._settings_store["clips"][plan.rel] = self._current_settings()
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
        summary = RunSummary()
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
            if self.o.max_frames:
                self.warn(f"TESTLAUF: nur die ersten {self.o.max_frames} Frames je Clip; dur/Duration werden NICHT angepasst")
            self.info(f"Einstellungen: Bitrate {self.o.bitrate_mbps} Mbit/s, Preset {self.o.preset}")
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
                if p.selected and not p.convert and p.decision is not None:
                    self.warn(f"{p.name}: wird nicht umgewandelt ({p.decision.reason}) - Datei wird unveraendert kopiert")
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
            self._frames_pending = sum(p.frames for p in todo if not self._tgt_is_l(p)) or 0

            self._check_disk_space(target_dir, plans)

            # --- ab hier wird im Ziel geschrieben
            touched = True
            tmp_root = target_dir / TMP_NAME
            self._shutil_clean(tmp_root)
            out_root.mkdir(parents=True, exist_ok=True)
            tmp_root.mkdir(parents=True, exist_ok=True)
            self._load_settings(target_dir)

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
                res = ClipResult(name=plan.name, src_size=plan.mxf.stat().st_size)
                if not plan.selected:
                    res.status = ST_NOT_SELECTED
                    res.notes.append(plan.skip_reason)
                    summary.results.append(res)
                    continue
                t_clip = time.time()
                dst = out_root / "Clip" / plan.mxf.name
                try:
                    self._process_clip(plan, src_root, out_root, tmp_root, res)
                except tools.Cancelled:
                    res.status = ST_CANCELLED
                    res.dst_size = dst.stat().st_size if dst.exists() else 0
                    summary.results.append(res)
                    raise
                except Exception as e:  # noqa: BLE001
                    res.status = ST_FAILED
                    res.notes.append(str(e))
                    self.error(f"{plan.name}: {e}")
                res.seconds = time.time() - t_clip
                try:
                    res.dst_size = dst.stat().st_size if dst.exists() else 0
                except OSError:
                    res.dst_size = 0
                summary.results.append(res)
                self.rep.clip_done(res)
                self._shutil_clean(tmp_root / plan.stem)
            self._report_resume_diffs()
            clean_run = not any(r.status == ST_FAILED for r in summary.results) and not summary.copy_errors
            # K2: ein voller Lauf macht aus einer Testausgabe wieder eine normale Ausgabe
            if clean_run and not self.o.max_frames and marker.exists():
                if any(not p.selected and (out_root / "Clip" / p.mxf.name).exists() for p in plans):
                    self.warn("Testmarker TESTAUSGABE.txt bleibt bestehen: nicht ausgewaehlte Clips im Ziel stammen evtl. aus einem Testlauf")
                else:
                    try:
                        marker.unlink()
                        self.info("Testmarker TESTAUSGABE.txt entfernt (voller Lauf, alle Clips neu geprueft)")
                    except OSError as e:
                        self.warn(f"Testmarker konnte nicht entfernt werden: {e}")
        except tools.Cancelled:
            summary.cancelled = True
            self.warn("Abgebrochen. Bereits fertige Clips bleiben erhalten; ein erneuter Lauf setzt fort.")
        except Exception as e:  # noqa: BLE001
            summary.fatal = str(e)
            self.error(f"Abbruch wegen Fehler: {e}")
        finally:
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
            release_lock(lock_path)
        return summary

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
                if self._tgt_is_l(p):
                    continue  # schon im Ziel (wird nur geprueft)
                est = estimate_clip_bytes(p.frames, p.info.edit_rate, len(p.info.audio), self.o.bitrate_mbps)
            else:
                dst = (self._out_root or target_dir) / "Clip" / p.mxf.name
                if dst.exists() and card.same_file_stat(p.mxf, dst):
                    continue
                est = p.mxf.stat().st_size
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
            work_secs = self._work_secs_done + (time.time() - self._clip_t0)
            if work_secs > 5 and work_frames > 0:
                remaining = max(0, self._frames_pending - work_frames)
                eta = remaining / (work_frames / work_secs)
        self.rep.progress(plan.name, clip_frac, min(1.0, total), eta, text)

    # ------------------------------------------------------------------ Ein Clip
    def _check_existing_umid(self, plan: ClipPlan, dst: Path) -> None:
        """G2b: Eine vorhandene Ziel-MXF, die nicht zu diesem Clip gehoert (UMID), wird nie ueberschrieben."""
        ti = self._tgt_info.get(plan.name.casefold())
        if ti is None:
            return  # nicht lesbar: gilt als beschaedigte eigene Ausgabe, wird erst nach erfolgreicher Neuerzeugung ersetzt
        want = plan.info.mp_uid if plan.info is not None else (plan.material.umid if plan.material else "")
        if want and ti.mp_uid and ti.mp_uid != want:
            raise RuntimeError(
                f"Im Ziel liegt bereits eine andere Datei mit diesem Namen (Material-Package-UMID {ti.mp_uid} statt {want}) - "
                "wird nicht ueberschrieben. Bitte einen anderen Zielordner waehlen."
            )

    def _process_clip(self, plan: ClipPlan, src_root: Path, out_root: Path, tmp_root: Path, res: ClipResult) -> None:
        dst_dir = out_root / "Clip"
        dst = dst_dir / plan.mxf.name
        res.notes.extend(plan.notes)

        if not plan.convert:
            if dst.exists() and card.same_file_stat(plan.mxf, dst):
                self.info(f"{plan.name}: unveraendert kopierter Clip ist schon im Ziel - uebersprungen")
                res.status = ST_COPIED
                res.notes.append("bereits im Ziel vorhanden (Groesse und Zeitstempel gleich)")
                self._write_clip_xml(plan, dst_dir, None)
                return
            if dst.exists():
                self._check_existing_umid(plan, dst)
            self.info(f"{plan.name}: kopiere unveraendert ({plan.decision.reason if plan.decision else 'nicht analysierbar'})")
            tmpd = tmp_root / plan.stem
            tmpd.mkdir(parents=True, exist_ok=True)
            part = tmpd / "copy.mxf"
            self._copy_cancelable(plan.mxf, part, plan)
            shutil.copystat(plan.mxf, part)
            os.replace(part, dst)
            self._write_clip_xml(plan, dst_dir, None)
            res.status = ST_COPIED
            res.frames = 0
            if plan.decision:
                res.notes.append(plan.decision.reason)
            return

        info = plan.info
        assert info is not None and plan.decision is not None and plan.decision.fps is not None
        fps = plan.decision.fps
        frames = plan.frames
        res.frames = frames
        expect_anc = bool(self.o.keep_anc and info.anc_tracks)
        embed = self._want_embed(info, plan)
        exp = make_expect(
            level=plan.level,
            frames=frames, mp_uid=info.mp_uid, fp_uid=info.fp_uid, start_tc=info.start_tc,
            n_audio=len(info.audio), fps=fps, expect_anc=expect_anc, expect_xml=embed,
        )

        # Fortsetzen: fertiger, verifizierter Ziel-Clip vorhanden?
        if dst.exists():
            self._check_existing_umid(plan, dst)
            self._emit_progress(plan, 0.0, "Pruefe vorhandene Zieldatei ...")
            probs = verify.check_clip(dst, exp, cancel=self.cancel)
            if not probs and self.o.sony_header:
                hprobs = verify.check_header(dst, plan.mxf, cancel=self.cancel, src_info=info)
                if hprobs:
                    # Nur die Kopfdaten weichen ab (z. B. Ausgabe einer aelteren Version): nachziehen statt neu transkodieren
                    try:
                        self._adopt_header(plan, dst)
                        # danach die VOLLE Pruefung (nicht nur die Kopfdaten)
                        after = verify.check_clip(dst, exp, cancel=self.cancel, source=plan.mxf, source_info=info)
                    except _HEADER_ERRORS as e:
                        after = [f"Angleichen nicht moeglich: {e}"]
                    if after:
                        probs = ["Kopfdaten: " + after[0]]
                    else:
                        self.info(f"{plan.name}: Kopfdaten nachgezogen (Datei nicht neu transkodiert)")
                        res.notes.append("Kopfdaten nachgezogen")
            if not probs:
                self.info(f"{plan.name}: bereits fertig und verifiziert - uebersprungen")
                self._finish_clip_files(plan, dst_dir)
                self._check_resume_settings(plan)
                res.status = ST_SKIPPED_DONE
                self._emit_progress(plan, 1.0, "bereits fertig")
                self._frames_done += frames
                return
            self.warn(f"{plan.name}: vorhandene Zieldatei unvollstaendig/abweichend ({probs[0]}) - wird neu erzeugt "
                      "(die alte Datei bleibt bis zum erfolgreichen Ersetzen bestehen)")

        self._clip_t0 = time.time()
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

        # 4. Wrappen (mit Rueckfall: erst ohne XML, dann ohne ANC)
        out_mxf = tmpd / "out.mxf"
        attempts = [(anc_file, xml_file)]
        if xml_file is not None:
            attempts.append((anc_file, None))
        if anc_file is not None:
            attempts.append((None, None))
        last_err = ""
        wrapped = False
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
            probs = verify.check_clip(
                out_mxf, exp, cancel=self.cancel,
                source=plan.mxf if self.o.sony_header else None, source_info=info,
            )
            if probs:
                last_err = "; ".join(probs)
                self.warn(f"{plan.name}: Pruefung des Ergebnisses fehlgeschlagen: {last_err} - Versuch {idx + 1}/{len(attempts)}")
                continue
            if expect_anc and anc_f is None:
                res.notes.append("ANC-Datenspur (Kamera-Metadaten) konnte nicht uebernommen werden und fehlt im Ziel")
                self.warn(f"{plan.name}: ANC-Spur fehlt im Ziel (Uebernahme nicht moeglich)")
            if embed and xml_f is None:
                res.notes.append("eingebettetes XML konnte nicht uebernommen werden")
                self.warn(f"{plan.name}: eingebettetes XML fehlt im Ziel")
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
        os.replace(out_mxf, dst)
        self._finish_clip_files(plan, dst_dir)
        self._record_clip_settings(plan)
        res.status = ST_CONVERTED
        self._work_frames_done += frames
        self._work_secs_done += time.time() - self._clip_t0
        self._emit_progress(plan, 1.0, "fertig")
        self._frames_done += frames
        self.info(f"{plan.name}: fertig ({fmt_size(plan.mxf.stat().st_size)} -> {fmt_size(dst.stat().st_size)})")

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
        args = [
            tools.find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-y", "-nostats", "-v", "error",
            "-progress", "pipe:1",
            "-i", str(plan.mxf),
            "-map", "0:v:0", "-c:v", "libx264", "-profile:v", "high422", "-pix_fmt", "yuv422p10le",
            "-level:v", level, "-preset", self.o.preset, "-color_range", "tv",
            "-b:v", bitrate, "-maxrate", bitrate, "-bufsize", bitrate,
            "-g", str(n), "-bf", "2", "-fps_mode", "passthrough",
            "-x264-params", x264,
        ]
        if self.o.max_frames:
            args += ["-frames:v", str(frames)]
        args += ["-f", "h264", str(v_path)]
        secs = float(Fraction(frames) / fps)
        for i, w in enumerate(wavs):
            args += ["-map", f"0:a:{i}", "-c:a", "pcm_s24le", "-rf64", "auto"]
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
            "-f", mxfinfo.rate_arg(fps),
            "-y", info.start_tc or "00:00:00:00",
            "--mp-uid", info.mp_uid, "--fp-uid", info.fp_uid,
            "--dur", str(frames),
            "--aes-3",  # Audio-Mapping wie Sony (ec_label ...02060300, Track-Nummern 0x16NN03xx)
        ]
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
        res = tools.run_capture(args, cancel=self.cancel, on_stdout_line=lines.append, on_stderr_line=lines.append, keep_lines=100)
        if res.returncode != 0:
            msg = [l for l in (res.stdout + "\n" + res.stderr).splitlines() if l.strip()]
            return "raw2bmx Code %d: %s" % (res.returncode, " | ".join(msg[-4:]))
        return ""


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
        elif p.decision is not None:
            unchanged[p.decision.reason] = unchanged.get(p.decision.reason, 0) + 1
    for reason, n in unchanged.items():
        ov.notes.append(f"{n} Clip(s) bleiben unveraendert und werden nur kopiert: {reason}")
    if unlisted:
        ov.notes.append(f"{unlisted} Clip(s) liegen auf der Karte, stehen aber nicht in MEDIAPRO.XML.")
    return ov
