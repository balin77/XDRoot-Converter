"""Parsing der Ausgaben von mxf2raw -i und ffprobe sowie Bildraten-Hilfen."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from fractions import Fraction
from typing import Optional

from . import tools

# --------------------------------------------------------------------------- Bildrate


def parse_fraction(text: str) -> Fraction:
    """'50/1', '30000/1001', '25' -> Fraction."""
    text = text.strip()
    if "/" in text:
        n, d = text.split("/", 1)
        d_i = int(d)
        if d_i == 0:
            raise ValueError(f"Ungueltige Rate: {text}")
        return Fraction(int(n), d_i)
    return Fraction(int(text), 1)


def rate_arg(fps: Fraction) -> str:
    """Bildrate als Argument fuer raw2bmx -f ('50' oder '30000/1001')."""
    if fps.denominator == 1:
        return str(fps.numerator)
    return f"{fps.numerator}/{fps.denominator}"


def gop_length(fps: Fraction) -> int:
    """GOP-Laenge N (Vielfaches von M=3), etwa 0,5 s: 50p -> 24, 25p -> 12, 29.97p -> 15, 59.94p -> 30."""
    n = 3 * round(float(fps) / 6.0)
    return max(3, int(n))


def frames_to_seconds(frames: int, fps: Fraction) -> Fraction:
    return Fraction(frames) / fps


# --------------------------------------------------------------------------- mxf2raw -i


@dataclass
class MxfTrack:
    index: int
    kind: str = ""  # Picture / Sound / Data
    essence_type: str = ""
    ec_label: str = ""
    duration: Optional[int] = None
    track_number: str = ""  # TrackNumber im File Source Package ('0x16040300')
    mp_track_id: Optional[int] = None  # Track-ID im Material Package
    fp_track_id: Optional[int] = None  # Track-ID im File Source Package
    fp_track_number: str = ""  # == track_number (explizit benannt)
    descriptor: dict = field(default_factory=dict)
    anc_elements: list = field(default_factory=list)  # [(line, did, sdid)]


@dataclass
class MxfInfo:
    path: str = ""
    mp_uid: str = ""  # 64 Hex-Zeichen, Grossbuchstaben
    fp_uid: str = ""
    mp_created: str = ""  # 'YYYY-MM-DD hh:mm:ss.mmm'
    fp_created: str = ""
    ident_modified: str = ""
    ident_company: str = ""
    ident_product: str = ""  # Leerzeichen am Ende bleiben erhalten ("Mem ")
    ident_version_string: str = ""
    ident_product_uid: str = ""
    ident_product_version: str = ""
    ident_toolkit_version: str = ""
    ident_platform: str = ""
    ident_generation_uid: str = ""
    edit_rate: Optional[Fraction] = None
    duration: Optional[int] = None  # Frames
    start_tc: str = ""  # StartTimecodes material
    start_tc_file: str = ""
    frame_wrapped: Optional[bool] = None
    op_label: str = ""
    tracks: list = field(default_factory=list)
    text_objects: list = field(default_factory=list)
    warnings: list = field(default_factory=list)

    @property
    def video(self) -> Optional[MxfTrack]:
        for t in self.tracks:
            if t.kind == "Picture":
                return t
        return None

    @property
    def audio(self) -> list:
        return [t for t in self.tracks if t.kind == "Sound"]

    @property
    def data(self) -> list:
        return [t for t in self.tracks if t.kind == "Data"]

    @property
    def anc_tracks(self) -> list:
        return [t for t in self.tracks if t.essence_type == "ANC_Data"]


_KV = re.compile(r"^(\s*)([A-Za-z_][\w ]*?)\s*:\s?(.*)$")
_COUNT = re.compile(r"count='(\d+)'")


def normalize_umid(text: str) -> str:
    """'urn:smpte:umid:060a2b34.0101...' oder Hex mit Punkten -> 64 Hex-Zeichen, Grossbuchstaben."""
    t = text.strip()
    if t.lower().startswith("urn:smpte:umid:"):
        t = t[len("urn:smpte:umid:"):]
    t = t.replace(".", "").replace("-", "").replace(" ", "")
    if t.lower().startswith("0x"):
        t = t[2:]
    return t.upper()


def parse_mxf2raw(text: str, path: str = "") -> MxfInfo:
    info = MxfInfo(path=path)
    ctx = ""  # ident / mp / fp / clip / tc / track / textobj / logs
    track: Optional[MxfTrack] = None
    desc_active = False
    pkg_part = ""  # 'mat' / 'fs' innerhalb eines Tracks (Material:/FileSource:)
    cur_text: Optional[dict] = None

    for raw in text.splitlines():
        raw_line = raw.rstrip("\r\n")  # Leerzeichen am Ende gehoeren zum Wert ("Mem ")
        line = raw.rstrip()
        if not line.strip():
            continue
        stripped = line.strip()
        indent = len(line) - len(line.lstrip(" "))

        # Abschnittsmarker
        if stripped.startswith("Identification #"):
            ctx = "ident"
            # Es zaehlt die letzte Identification (Sony/bmx schreiben meist nur eine)
            info.ident_modified = info.ident_company = info.ident_product = ""
            info.ident_version_string = info.ident_product_uid = info.ident_product_version = ""
            info.ident_toolkit_version = info.ident_platform = info.ident_generation_uid = ""
            continue
        if stripped.startswith("MaterialPackage #"):
            ctx = "mp"
            continue
        if stripped.startswith("FileSourcePackage #"):
            ctx = "fp"
            continue
        if indent == 0 and stripped == "Clip":
            ctx, track = "clip", None
            continue
        if indent == 0 and stripped.startswith("LogMessages"):
            ctx, track = "logs", None
            continue
        if ctx == "logs":
            m = re.match(r"(warning|error)\s*:\s*(.*)", stripped)
            if m:
                info.warnings.append(f"{m.group(1)}: {m.group(2)}")
            continue
        m = re.match(r"Track #(\d+):", stripped)
        if m and indent == 4:
            track = MxfTrack(index=int(m.group(1)))
            info.tracks.append(track)
            ctx = "track"
            desc_active = False
            pkg_part = ""
            continue
        if stripped.startswith("TextObjects:") and indent == 2:
            ctx, track = "textobj", None
            continue
        if stripped.startswith("TextObject #"):
            cur_text = {}
            info.text_objects.append(cur_text)
            continue
        if stripped == "StartTimecodes:":
            ctx = "tc"
            continue

        if ctx == "track" and track is not None:
            if stripped == "Material:":
                pkg_part = "mat"
                continue
            if stripped == "FileSource:":
                pkg_part = "fs"
                continue
            if stripped.endswith("Descriptor:"):
                desc_active = True
                pkg_part = ""
                continue
            m2 = re.match(r"Element #(\d+):", stripped)
            if m2:
                track.anc_elements.append({})
                continue

        km = _KV.match(raw_line)
        if not km:
            continue
        key, val = km.group(2).strip(), km.group(3).strip()
        rawval = km.group(3)  # unveraendert (Leerzeichen am Ende erhalten)

        if ctx == "ident":
            if key == "modified_date":
                info.ident_modified = val
            elif key == "company_name":
                info.ident_company = rawval
            elif key == "product_name":
                info.ident_product = rawval
            elif key == "version_string":
                info.ident_version_string = rawval
            elif key == "product_uid":
                info.ident_product_uid = val
            elif key == "product_version":
                info.ident_product_version = val
            elif key == "toolkit_version":
                info.ident_toolkit_version = val
            elif key == "platform":
                info.ident_platform = rawval
            elif key == "generation_uid":
                info.ident_generation_uid = val
        elif ctx == "mp":
            if key == "package_uid" and not info.mp_uid:
                info.mp_uid = normalize_umid(val)
            elif key == "creation_date" and not info.mp_created:
                info.mp_created = val
        elif ctx == "fp":
            if key == "package_uid" and not info.fp_uid:
                info.fp_uid = normalize_umid(val)
            elif key == "creation_date" and not info.fp_created:
                info.fp_created = val
        elif ctx == "tc":
            if key == "material":
                info.start_tc = val
            elif key == "file_source":
                info.start_tc_file = val
        elif ctx == "clip":
            if key == "edit_rate" and info.edit_rate is None:
                info.edit_rate = parse_fraction(val)
            elif key == "duration" and info.duration is None:
                cm = _COUNT.search(val)
                if cm:
                    info.duration = int(cm.group(1))
        elif ctx == "track" and track is not None:
            if not desc_active:
                if key == "essence_kind" and not track.kind:
                    track.kind = val
                elif key == "essence_type" and not track.essence_type:
                    track.essence_type = val
                elif key == "ec_label" and not track.ec_label:
                    track.ec_label = val
                elif key == "duration" and track.duration is None:
                    cm = _COUNT.search(val)
                    if cm:
                        track.duration = int(cm.group(1))
                elif key == "track_number" and val.startswith("0x") and not track.track_number:
                    track.track_number = val
                    track.fp_track_number = val
                elif key == "track_id" and pkg_part in ("mat", "fs"):
                    try:
                        tid = int(val)
                    except ValueError:
                        tid = None
                    if pkg_part == "mat" and track.mp_track_id is None:
                        track.mp_track_id = tid
                    elif pkg_part == "fs" and track.fp_track_id is None:
                        track.fp_track_id = tid
            elif key in ("line_number", "wrapping_type", "sample_coding", "did", "sdid", "description") and track.anc_elements:
                track.anc_elements[-1][key] = val
            else:
                track.descriptor.setdefault(key, val)
        elif ctx == "textobj" and cur_text is not None:
            cur_text[key] = rawval if key in ("description", "mime_type", "lang_code") else val
        if indent == 4 and key == "frame_wrapped":
            info.frame_wrapped = val.lower() == "true"
        if key == "op_label" and not info.op_label:
            info.op_label = val
    return info


def read_mxf_info(path: str, cancel: Optional[tools.CancelToken] = None) -> MxfInfo:
    res = tools.run_capture([tools.find_tool("mxf2raw"), "-i", path], cancel=cancel)
    if res.returncode != 0 or "Clip" not in res.stdout:
        raise RuntimeError(f"mxf2raw -i fehlgeschlagen fuer {path}: {res.stderr.strip()[-400:] or res.stdout.strip()[-400:]}")
    return parse_mxf2raw(res.stdout, path)


# --------------------------------------------------------------------------- ffprobe


@dataclass
class ProbeResult:
    streams: list = field(default_factory=list)  # list[dict]
    format: dict = field(default_factory=dict)

    @property
    def video(self) -> Optional[dict]:
        for s in self.streams:
            if s.get("codec_type") == "video":
                return s
        return None

    @property
    def audio(self) -> list:
        return [s for s in self.streams if s.get("codec_type") == "audio"]


def parse_ffprobe(text: str) -> ProbeResult:
    """Parst die Standard-Ausgabe von ffprobe -show_streams -show_format ([STREAM]-Bloecke)."""
    res = ProbeResult()
    cur: Optional[dict] = None
    kind = ""
    for raw in text.splitlines():
        line = raw.strip()
        if line in ("[STREAM]", "[FORMAT]"):
            cur = {}
            kind = line
            continue
        if line in ("[/STREAM]", "[/FORMAT]"):
            if cur is not None:
                if kind == "[STREAM]":
                    res.streams.append(cur)
                else:
                    res.format = cur
            cur = None
            continue
        if cur is not None and "=" in line:
            k, v = line.split("=", 1)
            cur[k] = v
    return res


def run_ffprobe(path: str, cancel: Optional[tools.CancelToken] = None, extra: Optional[list] = None) -> ProbeResult:
    args = [tools.find_tool("ffprobe"), "-v", "error", "-show_streams", "-show_format"]
    if extra:
        args += extra
    args.append(path)
    res = tools.run_capture(args, cancel=cancel)
    if res.returncode != 0:
        raise RuntimeError(f"ffprobe fehlgeschlagen fuer {path}: {res.stderr.strip()[-400:]}")
    return parse_ffprobe(res.stdout)


@dataclass
class SourceDecision:
    convert: bool
    reason: str
    fps: Optional[Fraction] = None


def decide_source(mxf: MxfInfo, probe: ProbeResult) -> SourceDecision:
    """Soll dieser Clip umgewandelt werden? Nur XAVC-I (H.264 Intra) 1920x1080 progressiv."""
    v = probe.video
    if v is None or mxf.video is None:
        return SourceDecision(False, "kein Video")
    codec = v.get("codec_name", "")
    profile = v.get("profile", "")
    w, h = v.get("width", ""), v.get("height", "")
    if codec != "h264":
        return SourceDecision(False, f"Codec {codec or '?'} (kein H.264)")
    if "Intra" not in profile:
        return SourceDecision(False, f"H.264 {profile or '?'} ist kein Intra-Profil (bereits Long GOP?)")
    if (w, h) != ("1920", "1080"):
        return SourceDecision(False, f"Aufloesung {w}x{h} wird in Version 1 nicht umgewandelt")
    fo = v.get("field_order", "progressive")
    if fo not in ("progressive", "unknown", ""):
        return SourceDecision(False, f"Halbbilder ({fo}) werden in Version 1 nicht umgewandelt")
    rate = v.get("r_frame_rate") or v.get("avg_frame_rate") or ""
    try:
        fps = parse_fraction(rate)
    except Exception:
        fps = mxf.edit_rate
    if fps is None or fps <= 0:
        return SourceDecision(False, "Bildrate unbekannt")
    if mxf.edit_rate is not None and mxf.edit_rate != fps:
        return SourceDecision(False, f"Bildrate uneinheitlich (MXF {mxf.edit_rate}, ffprobe {fps})")
    if not mxf.audio:
        return SourceDecision(False, "keine Audiospuren")
    if len(probe.audio) != len(mxf.audio):
        return SourceDecision(False, f"Audiospuren uneinheitlich (MXF {len(mxf.audio)}, ffprobe {len(probe.audio)})")
    return SourceDecision(True, "XAVC-Intra 1080 progressiv", fps)


def to_json(info: MxfInfo) -> str:  # fuer Fehlersuche
    return json.dumps(info, default=lambda o: o.__dict__, indent=1)
