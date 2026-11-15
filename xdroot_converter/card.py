"""XDROOT-Struktur lesen und XML-Dateien textbasiert (byte-genau) anpassen."""

from __future__ import annotations

import html
import os
import re
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# --------------------------------------------------------------------------- Codec-Strings

_XAVC_I_RE = re.compile(r"^AVC100CBG_(1920)_(1080)_H422IP@(L\d{2})$")
_XAVC_L_RE = re.compile(r"^AVC50_(1920)_(1080)_H422P@(L\d{2})$")


def derive_xavcl_codec(source_codec: str) -> Optional[str]:
    """'AVC100CBG_1920_1080_H422IP@L42' -> 'AVC50_1920_1080_H422P@L42'. Unbekannte Muster -> None."""
    m = _XAVC_I_RE.match(source_codec.strip()) if source_codec else None
    if not m:
        return None
    return f"AVC50_{m.group(1)}_{m.group(2)}_H422P@{m.group(3)}"


def is_xavcl_codec(codec: str) -> bool:
    return bool(codec and _XAVC_L_RE.match(codec.strip()))


def level_from_codec(codec: str) -> Optional[str]:
    """'...@L42' -> '4.2' (fuer ffmpeg -level)."""
    m = re.search(r"@L(\d)(\d)$", codec or "")
    if not m:
        return None
    return f"{m.group(1)}.{m.group(2)}"


# --------------------------------------------------------------------------- Karte finden


def is_xdroot(d: Path) -> bool:
    return d.is_dir() and (d / "MEDIAPRO.XML").is_file() and (d / "Clip").is_dir()


def find_xdroot(path: str | os.PathLike) -> Path:
    p = Path(path)
    if not p.is_dir():
        raise FileNotFoundError(f"Ordner nicht gefunden: {p}")
    if is_xdroot(p):
        return p
    cands = [c for c in sorted(p.iterdir()) if is_xdroot(c)]
    if not cands:
        raise FileNotFoundError(
            f"Kein XDROOT gefunden in '{p}' (erwartet: Ordner mit MEDIAPRO.XML und Clip/)."
        )
    for c in cands:
        if c.name.upper() == "XDROOT":
            return c
    return cands[0]


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def check_target(src_xdroot: Path, target_dir: Path) -> Path:
    """Liefert das Ziel-XDROOT (target_dir/XDROOT); wirft ValueError bei Konflikt mit der Quelle."""
    out_root = target_dir / "XDROOT"
    s, o = src_xdroot.resolve(), out_root.resolve()
    if s == o or _is_within(o, s) or _is_within(s, o):
        raise ValueError("Der Zielordner darf nicht gleich oder innerhalb der Quelle liegen (und umgekehrt).")
    return out_root


# --------------------------------------------------------------------------- MEDIAPRO lesen

_NS = "{http://xmlns.sony.net/pro/metadata/mediaprofile}"


@dataclass
class Material:
    uri: str
    type: str = ""
    dur: Optional[int] = None
    fps: str = ""
    ch: Optional[int] = None
    video_type: str = ""
    umid: str = ""

    @property
    def rel_path(self) -> str:
        u = self.uri
        if u.startswith("./"):
            u = u[2:]
        return u


@dataclass
class MediaPro:
    system_kind: str = ""
    system_id: str = ""
    materials: list = field(default_factory=list)  # nur type="MXF"

    def find(self, rel_path: str) -> Optional[Material]:
        key = rel_path.replace("\\", "/").casefold()
        for m in self.materials:
            if m.rel_path.casefold() == key:
                return m
        return None


def parse_mediapro(data: bytes) -> MediaPro:
    root = ET.fromstring(data)
    mp = MediaPro()
    for el in root.iter():
        tag = el.tag.replace(_NS, "")
        if tag == "System":
            mp.system_kind = el.get("systemKind", "")
            mp.system_id = el.get("systemId", "")
        elif tag == "Material" and el.get("type", "").upper() == "MXF":
            dur = el.get("dur")
            ch = el.get("ch")
            mp.materials.append(
                Material(
                    uri=el.get("uri", ""),
                    type=el.get("type", ""),
                    dur=int(dur) if dur and dur.isdigit() else None,
                    fps=el.get("fps", ""),
                    ch=int(ch) if ch and ch.isdigit() else None,
                    video_type=el.get("videoType", ""),
                    umid=el.get("umid", "").upper(),
                )
            )
    return mp


@dataclass
class ClipXml:
    video_codec: str = ""
    duration: Optional[int] = None
    umid_ref: str = ""
    ltc_start: str = ""
    num_audio: Optional[int] = None


def parse_clip_xml(data: bytes) -> ClipXml:
    root = ET.fromstring(data)
    c = ClipXml()
    for el in root.iter():
        tag = el.tag.split("}")[-1]
        if tag == "VideoFrame":
            c.video_codec = el.get("videoCodec", "")
        elif tag == "Duration":
            v = el.get("value", "")
            c.duration = int(v) if v.isdigit() else None
        elif tag == "TargetMaterial":
            c.umid_ref = el.get("umidRef", "").upper()
        elif tag == "LtcChange" and not c.ltc_start:
            c.ltc_start = el.get("value", "")
        elif tag == "AudioFormat":
            v = el.get("numOfChannel", "")
            c.num_audio = int(v) if v.isdigit() else None
    return c


# --------------------------------------------------------------------------- XML patchen (Text)


def _decode(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        raise ValueError("UTF-16-kodierte XML-Dateien werden nicht unterstuetzt.")
    # surrogateescape: verlustfreie Rueckwandlung beliebiger Bytes, BOM bleibt als Zeichen erhalten
    return data.decode("utf-8", errors="surrogateescape")


def _encode(text: str) -> bytes:
    return text.encode("utf-8", errors="surrogateescape")


_MATERIAL_TAG = re.compile(r"<Material\b[^>]*>")
_ATTR_URI = re.compile(r"""(\buri\s*=\s*)(["'])(.*?)\2""", re.S)
_ATTR_VIDEOTYPE = re.compile(r"""(\bvideoType\s*=\s*)(["'])(.*?)\2""", re.S)


def _norm_uri(u: str) -> str:
    u = html.unescape(u).replace("\\", "/")
    if u.startswith("./"):
        u = u[2:]
    return u.casefold()


def patch_mediapro(data: bytes, new_types: dict[str, str]) -> tuple[bytes, list[str]]:
    """Setzt videoType bei den Material-Eintraegen, deren uri in ``new_types`` steht.

    ``new_types``: {'Clip/NAME.MXF': 'AVC50_...'} (Gross/Kleinschreibung egal).
    Rueckgabe: (neue Bytes, Liste der geaenderten rel. Pfade).
    """
    wanted = {_norm_uri(k): v for k, v in new_types.items()}
    changed: list[str] = []
    text = _decode(data)

    def repl_tag(m: re.Match) -> str:
        tag = m.group(0)
        um = _ATTR_URI.search(tag)
        if not um:
            return tag
        key = _norm_uri(um.group(3))
        if key not in wanted:
            return tag
        vt = _ATTR_VIDEOTYPE.search(tag)
        if not vt:
            return tag
        new_tag = tag[: vt.start(3)] + html.escape(wanted[key], quote=False) + tag[vt.end(3):]
        if new_tag != tag:
            changed.append(html.unescape(um.group(3)))
        return new_tag

    out = _MATERIAL_TAG.sub(repl_tag, text)
    return _encode(out), changed


_VIDEOFRAME_TAG = re.compile(r"<VideoFrame\b[^>]*>")
_ATTR_VIDEOCODEC = re.compile(r"""(\bvideoCodec\s*=\s*)(["'])(.*?)\2""", re.S)


def patch_clip_xml(data: bytes, new_codec: str) -> tuple[bytes, bool]:
    """Setzt VideoFrame@videoCodec; alles andere bleibt Byte fuer Byte erhalten."""
    text = _decode(data)
    done = [False]

    def repl(m: re.Match) -> str:
        tag = m.group(0)
        vc = _ATTR_VIDEOCODEC.search(tag)
        if not vc:
            return tag
        new_tag = tag[: vc.start(3)] + new_codec + tag[vc.end(3):]
        if new_tag != tag:
            done[0] = True
        return new_tag

    out = _VIDEOFRAME_TAG.sub(repl, text, count=1)
    return _encode(out), done[0]


# --------------------------------------------------------------------------- Dateien


def list_clip_mxf(xdroot: Path) -> list[Path]:
    clip_dir = xdroot / "Clip"
    return sorted(
        (p for p in clip_dir.iterdir() if p.is_file() and p.suffix.upper() == ".MXF"),
        key=lambda p: p.name.casefold(),
    )


def related_pattern(stem: str) -> re.Pattern:
    """Dateien, die zu einem Clip gehoeren: <stem>.<ext> oder <stem><Buchstabe><2 Ziffern>.<ext>."""
    return re.compile("^" + re.escape(stem) + r"([A-Za-z]\d{2})?\.[A-Za-z0-9]+$", re.I)


def copy_structure(
    src: Path,
    dst: Path,
    skip_stems: Optional[set[str]] = None,
    log: Optional[Callable[[str], None]] = None,
    cancel_check: Optional[Callable[[], None]] = None,
) -> int:
    """Kopiert alle Nicht-MXF-Dateien (und leere Ordner) 1:1 mit Zeitstempeln. Gibt Dateianzahl zurueck.

    ``skip_stems``: Clip-Namen (ohne Endung, casefold), deren Dateien ausgelassen werden (nur in Clip/ und Thmbnl/).
    """
    skip_pats = [related_pattern(s) for s in (skip_stems or set())]
    count = 0
    dirs: list[tuple[Path, Path]] = []
    for root, dnames, fnames in os.walk(src):
        rootp = Path(root)
        rel = rootp.relative_to(src)
        target_dir = dst / rel
        target_dir.mkdir(parents=True, exist_ok=True)
        dirs.append((rootp, target_dir))
        top = rel.parts[0].casefold() if rel.parts else ""
        for fn in fnames:
            if cancel_check:
                cancel_check()
            if top == "clip" and fn.upper().endswith(".MXF"):
                continue
            if top in ("clip", "thmbnl") and any(p.match(fn) for p in skip_pats):
                continue
            s, d = rootp / fn, target_dir / fn
            if d.exists():
                try:
                    ss, ds = s.stat(), d.stat()
                    if ss.st_size == ds.st_size and abs(ss.st_mtime - ds.st_mtime) < 2:
                        count += 1
                        continue
                except OSError:
                    pass
            shutil.copy2(s, d)
            count += 1
    for s_dir, d_dir in reversed(dirs):  # Ordner-Zeitstempel zuletzt
        try:
            shutil.copystat(s_dir, d_dir)
        except OSError:
            pass
    return count
