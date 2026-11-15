"""XDROOT-Struktur lesen und XML-Dateien textbasiert (byte-genau) anpassen."""

from __future__ import annotations

import html
import os
import re
import shutil
import stat
import time
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


def check_target(src_xdroot: Path, target_dir: Path, chosen_source: Optional[Path] = None) -> Path:
    """Liefert das Ziel-XDROOT (target_dir/XDROOT); wirft ValueError bei Konflikt mit der Quelle.

    ``chosen_source``: der vom Nutzer GEWAEHLTE Quellordner (kann der Elternordner des XDROOT sein);
    das Ziel darf auch nicht darin liegen, sonst wuerde die Kamerakarte beschrieben."""
    out_root = target_dir / "XDROOT"
    s, o = src_xdroot.resolve(), out_root.resolve()
    if s == o or _is_within(o, s) or _is_within(s, o):
        raise ValueError("Der Zielordner darf nicht gleich oder innerhalb der Quelle liegen (und umgekehrt).")
    if chosen_source is not None:
        try:
            c = Path(chosen_source).resolve()
        except OSError:
            c = None
        if c is not None and (_is_within(o, c) or _is_within(target_dir, c)):
            raise ValueError(
                "Der Zielordner darf nicht innerhalb des gewaehlten Quellordners liegen "
                "(die Kamerakarte wuerde sonst beschrieben)."
            )
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
    media_id: str = ""  # Attached@mediaId (Kennung des Datentraegers)
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
        elif tag == "Attached":
            mp.media_id = el.get("mediaId", "")
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


TMP_SUFFIX = ".xdrctmp"
_CLIP_XML_RE = re.compile(r"^(.*)M\d\d\.XML$", re.I)


def clip_xml_stem(name: str) -> Optional[str]:
    """'PDW F5_0001M01.XML' -> 'PDW F5_0001' (sonst None)."""
    m = _CLIP_XML_RE.match(name)
    return m.group(1) if m else None


def make_writable(path) -> None:
    """Schreibbit (Besitzer) setzen, damit eine schreibgeschuetzte Quelle (Windows: Nur-Lesen-Attribut) das Ziel
    nicht schreibgeschuetzt macht.  Bei Ordnern bleibt das Betreten (x) erhalten.  Fehler werden ignoriert."""
    try:
        mode = os.stat(path).st_mode
        add = stat.S_IWUSR | (stat.S_IXUSR if stat.S_ISDIR(mode) else 0)
        if mode & add != add:
            os.chmod(path, mode | add)
    except OSError:
        pass


def copystat_writable(src, dst) -> None:
    """Zeitstempel/Rechte von ``src`` auf ``dst`` uebertragen (best effort), danach ist ``dst`` beschreibbar."""
    try:
        shutil.copystat(src, dst)
    except OSError:
        pass
    make_writable(dst)


def replace_retry(src, dst, tries: int = 10, delay: float = 0.3) -> None:
    """``os.replace`` mit Wiederholungen bei PermissionError (Windows: Virenscanner, Explorer-Vorschau oder
    Nur-Lesen-Attribut der alten Zieldatei halten die Datei kurz fest)."""
    for i in range(max(1, tries)):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i >= tries - 1:
                raise
            if i == 0 and os.path.exists(dst) and not os.access(dst, os.W_OK):
                make_writable(dst)
            time.sleep(delay)


def write_bytes_atomic(dst: Path, data: bytes, stat_from: Optional[Path] = None) -> None:
    """Datei atomar schreiben: Temp-Datei im selben Ordner, fsync, dann os.replace (mit Wiederholungen).
    Zeitstempel/Rechte von ``stat_from`` werden uebernommen (soweit moeglich), das Ziel bleibt beschreibbar."""
    dst = Path(dst)
    tmp = dst.with_name(dst.name + TMP_SUFFIX)
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                pass
        if stat_from is not None:
            copystat_writable(stat_from, tmp)
        replace_retry(tmp, dst)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def copy_file_atomic(src: Path, dst: Path) -> None:
    """Datei mit Zeitstempeln atomar kopieren (Temp-Datei im Zielordner + os.replace mit Wiederholungen)."""
    dst = Path(dst)
    tmp = dst.with_name(dst.name + TMP_SUFFIX)
    try:
        shutil.copy2(src, tmp)
        make_writable(tmp)
        replace_retry(tmp, dst)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def same_file_stat(a: Path, b: Path) -> bool:
    """Gleiche Groesse und (auf 2 s) gleicher Zeitstempel."""
    try:
        sa, sb = a.stat(), b.stat()
    except OSError:
        return False
    return sa.st_size == sb.st_size and abs(sa.st_mtime - sb.st_mtime) < 2


def remove_stray_temp_files(root: Path) -> int:
    """Liegengebliebene Temp-Dateien (nach hartem Abbruch) entfernen."""
    n = 0
    if not root.is_dir():
        return 0
    for r, _d, files in os.walk(root):
        for fn in files:
            if fn.endswith(TMP_SUFFIX):
                try:
                    (Path(r) / fn).unlink()
                    n += 1
                except OSError:
                    pass
    return n


def check_existing_target(src_root: Path, out_root: Path, src_mp: MediaPro) -> None:
    """Prueft, dass ein bereits vorhandenes Ziel-XDROOT zur selben Karte gehoert.  Wirft ValueError
    (Abbruch VOR jeder Aenderung).  Nicht vorhandenes oder leeres Ziel ist in Ordnung."""
    if not out_root.exists():
        return
    prefix = "Der Zielordner enthaelt bereits eine andere Karte oder fremde Daten: "
    hint = (" Das tritt auch auf, wenn auf der Quellkarte seit dem letzten Lauf Clips geloescht wurden. "
            "Dann bitte einen neuen, leeren Zielordner waehlen.")
    if not out_root.is_dir():
        raise ValueError(prefix + f"'{out_root}' ist kein Ordner.")
    src_clip_names = {p.name.casefold() for p in list_clip_mxf(src_root)}
    tgt_mxfs = list_clip_mxf(out_root) if (out_root / "Clip").is_dir() else []
    mp_file = out_root / "MEDIAPRO.XML"
    if not mp_file.is_file():
        if tgt_mxfs:
            raise ValueError(prefix + f"{len(tgt_mxfs)} MXF-Datei(en) in Clip/ ohne MEDIAPRO.XML ({tgt_mxfs[0].name} ...). "
                             "Bitte einen leeren oder neuen Zielordner waehlen.")
        # Fremde Dateien (nicht aus der Quelle) in einem XDROOT ohne MEDIAPRO.XML
        for r, _d, files in os.walk(out_root):
            for fn in files:
                if fn.endswith(TMP_SUFFIX):
                    continue
                rel = (Path(r) / fn).relative_to(out_root)
                if not (src_root / rel).exists():
                    raise ValueError(prefix + f"{rel.as_posix()} gehoert nicht zur Quelle und es gibt keine MEDIAPRO.XML. "
                                     "Bitte einen leeren oder neuen Zielordner waehlen.")
        return
    try:
        tmp = parse_mediapro(mp_file.read_bytes())
    except Exception as e:  # noqa: BLE001
        raise ValueError(prefix + f"MEDIAPRO.XML im Ziel ist nicht lesbar ({e}). "
                         "Bitte einen leeren oder neuen Zielordner waehlen.") from e
    if src_mp.system_id and tmp.system_id and src_mp.system_id != tmp.system_id:
        raise ValueError(prefix + f"systemId {tmp.system_id} im Ziel, {src_mp.system_id} in der Quelle.")
    if src_mp.media_id and tmp.media_id and src_mp.media_id != tmp.media_id:
        raise ValueError(prefix + f"mediaId {tmp.media_id} im Ziel, {src_mp.media_id} in der Quelle.")
    src_umids = {m.umid for m in src_mp.materials if m.umid}
    foreign = [m for m in tmp.materials if m.umid and m.umid not in src_umids]
    if foreign:
        raise ValueError(prefix + f"{len(foreign)} Clip(s) der MEDIAPRO.XML im Ziel stehen nicht in der Quelle "
                         f"(z. B. {foreign[0].rel_path})." + hint)
    for p in tgt_mxfs:
        if p.name.casefold() not in src_clip_names:
            raise ValueError(prefix + f"Clip/{p.name} gehoert nicht zur Quelle." + hint)


def copy_structure(
    src: Path,
    dst: Path,
    skip_stems: Optional[set[str]] = None,
    log: Optional[Callable[[str], None]] = None,
    cancel_check: Optional[Callable[[], None]] = None,
    errors: Optional[list] = None,
    skip_xml: bool = True,
) -> int:
    """Kopiert alle Nicht-MXF-Dateien (und leere Ordner) 1:1 mit Zeitstempeln. Gibt Dateianzahl zurueck.

    ``skip_stems``: Clip-Namen (ohne Endung, casefold), deren Dateien ausgelassen werden (nur in Clip/ und Thmbnl/).
    ``skip_xml`` (Standard): ``MEDIAPRO.XML`` und ``Clip/*M??.XML`` werden NICHT kopiert; sie schreibt der
    Ablauf selbst (zustandsabhaengig, damit ein Folgelauf fertige Clips nicht auf XAVC-I zuruecksetzt).
    ``errors``: Liste, in die Fehler je Datei als ``(relativer Pfad, Meldung)`` gesammelt werden; ohne diese
    Liste wird der erste Fehler ausgeloest.  Abbruch (cancel_check) wird immer weitergereicht.
    """
    skip_pats = [related_pattern(s) for s in (skip_stems or set())]
    count = 0
    dirs: list[tuple[Path, Path]] = []

    def fail(rel: str, e: Exception) -> None:
        if errors is None:
            raise e
        errors.append((rel, str(e)))
        if log:
            log(f"{rel}: {e}")

    for root, dnames, fnames in os.walk(src, onerror=lambda e: fail(str(getattr(e, "filename", src)), e)):
        rootp = Path(root)
        rel = rootp.relative_to(src)
        target_dir = dst / rel
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            fail(rel.as_posix() or ".", e)
            continue
        dirs.append((rootp, target_dir))
        top = rel.parts[0].casefold() if rel.parts else ""
        for fn in fnames:
            if cancel_check:
                cancel_check()
            if top == "clip" and fn.upper().endswith(".MXF"):
                continue
            if skip_xml and not rel.parts and fn.upper() == "MEDIAPRO.XML":
                continue
            if skip_xml and top == "clip" and len(rel.parts) == 1 and clip_xml_stem(fn) is not None:
                continue
            if top in ("clip", "thmbnl") and any(p.match(fn) for p in skip_pats):
                continue
            s, d = rootp / fn, target_dir / fn
            try:
                if d.exists() and same_file_stat(s, d):
                    count += 1
                    continue
                copy_file_atomic(s, d)
                count += 1
            except OSError as e:
                fail((rel / fn).as_posix(), e)
    for s_dir, d_dir in reversed(dirs):  # Ordner-Zeitstempel zuletzt
        copystat_writable(s_dir, d_dir)
    return count
