"""Pruefungen: einzelner Ziel-Clip (auch fuer Fortsetzen) und komplette Karte (--verify)."""

from __future__ import annotations

import json
import math
import os
import re
import struct
import tempfile
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Callable, Optional

from . import card, mxfheader, mxfinfo, tools

# Referenzwerte (Sony FX6, XAVC-L), siehe docs/analysis/fx6_mxf2raw.txt
XAVCL_ESSENCE_TYPE = "AVC_High_422"
XAVCL_CODING_LABEL = "urn:smpte:ul:060e2b34.0401010d.04010202.01316001"
XAVCL_EC_LABEL = "urn:smpte:ul:060e2b34.0401010a.0d010301.02106001"
AUDIO_EC_LABEL = "urn:smpte:ul:060e2b34.04010101.0d010301.02060300"
NRT_SCHEME_ID = "urn:uuid:786d4d22-8280-11de-8a39-08004678031c"
TEST_MARKER = "TESTAUSGABE.txt"


@dataclass
class ClipExpect:
    frames: int
    mp_uid: str
    fp_uid: str
    start_tc: str
    n_audio: int
    fps: Fraction
    expect_anc: bool = False
    expect_xml: bool = False
    level: Optional[str] = None  # erwartetes H.264-Level, z. B. "4.2" (None = nicht pruefen)


# --------------------------------------------------------------------------- Dateistruktur

_PK_PREFIX = bytes.fromhex("060e2b34020501")  # Partition Pack / RIP: Bytes 0-6 (Byte 7 = Version)
_PK_TAIL = bytes.fromhex("0d01020101")         # Bytes 8-12
_MAX_RIP_ENTRIES = 20000


def _is_pk_key(key: bytes, kinds: tuple = (2, 3, 4)) -> bool:
    return (len(key) == 16 and key[:7] == _PK_PREFIX and key[8:13] == _PK_TAIL
            and key[13] in kinds and key[14] in (1, 2, 3, 4) and key[15] == 0)


def _read_pack(f, offset: int, size: int) -> dict:
    """Partition Pack an ``offset`` lesen (nur wenige Bytes).  Wirft ValueError mit deutschem Text."""
    if offset < 0 or offset + 16 + 1 > size:
        raise ValueError(f"Partition Pack bei {offset} liegt ausserhalb der Datei ({size} Bytes)")
    f.seek(offset)
    head = f.read(16 + 9)
    key = head[:16]
    if not _is_pk_key(key):
        raise ValueError(f"an Offset {offset} steht kein Partition Pack")
    b = head[16]
    if b < 0x80:
        ln, nb = b, 1
    else:
        nb = (b & 0x7F) + 1
        if nb > 9 or len(head) < 16 + nb:
            raise ValueError(f"Partition Pack bei {offset}: ungueltige Laengenangabe")
        ln = int.from_bytes(head[17:16 + nb], "big")
    if ln < 88 or ln > (1 << 20):
        raise ValueError(f"Partition Pack bei {offset}: unplausible Laenge {ln}")
    if offset + 16 + nb + ln > size:
        raise ValueError(f"Partition Pack bei {offset} ist abgeschnitten")
    f.seek(offset + 16 + nb)
    v = f.read(ln)
    if len(v) != ln:
        raise ValueError(f"Partition Pack bei {offset} ist abgeschnitten")
    this, prev, footer, hbc, ibc = struct.unpack(">QQQQQ", v[8:48])
    return {"offset": offset, "kind": key[13], "status": key[14], "this": this, "prev": prev,
            "footer": footer, "hbc": hbc, "ibc": ibc, "end": offset + 16 + nb + ln}


def check_structure(
    path,
    frames: Optional[int] = None,
    n_audio: Optional[int] = None,
    fps: Optional[Fraction] = None,
) -> list[str]:
    """Schnelle Strukturpruefung einer (Ziel-)MXF ohne mxf2raw/ffmpeg - liest nur kleine Bereiche,
    auch bei sehr grossen Dateien.  Faengt abgeschnittene, nicht abgeschlossene und beschaedigte
    Dateien ab: Header-Partition geschlossen+vollstaendig, FooterPartition-Offset zeigt auf ein
    Footer-Partition-Pack, RIP am Dateiende (Laenge, Eintraege zeigen auf Partition Packs, letzter =
    Footer), Dateigroesse = Ende des RIP.  Mit ``frames``/``n_audio``/``fps`` zusaetzlich die
    Mindestgroesse (Header + Frames x Tonbytes).  Leere Liste = in Ordnung."""
    out: list[str] = []
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            out = _structure_problems(f, size, frames, n_audio, fps)
    except OSError as e:
        return [f"Datei nicht lesbar: {e}"]
    return out


def _structure_problems(f, size: int, frames, n_audio, fps) -> list[str]:
    out: list[str] = []
    try:
        hp = _read_pack(f, 0, size)
    except ValueError as e:
        return [f"Header-Partition: {e}"]
    if hp["kind"] != 2:
        return ["erste Partition ist keine Header-Partition"]
    if hp["status"] != 4:
        out.append(f"Header-Partition nicht geschlossen/vollstaendig (Status {hp['status']}) - Datei unvollstaendig geschrieben?")
    if hp["this"] != 0:
        out.append(f"Header-Partition: ThisPartition {hp['this']} statt 0")
    foff = hp["footer"]
    footer = None
    if foff == 0:
        out.append("FooterPartition im Header-Pack ist 0 (Datei nicht abgeschlossen)")
    elif foff >= size:
        out.append(f"FooterPartition-Offset {foff} liegt hinter dem Dateiende ({size} Bytes) - Datei abgeschnitten")
    else:
        try:
            footer = _read_pack(f, foff, size)
            if footer["kind"] != 4:
                out.append(f"an FooterPartition-Offset {foff} steht kein Footer-Partition-Pack")
                footer = None
            elif footer["status"] != 4:
                out.append(f"Footer-Partition nicht geschlossen/vollstaendig (Status {footer['status']})")
            elif footer["this"] != foff:
                out.append(f"Footer-Partition: ThisPartition {footer['this']} statt {foff}")
        except ValueError as e:
            out.append(f"Footer-Partition: {e}")
            footer = None
    # RIP am Dateiende
    rip_start = None
    if size < 16 + 1 + 4 + 4:
        out.append("Datei zu klein fuer ein RIP")
    else:
        f.seek(size - 4)
        (rip_len,) = struct.unpack(">I", f.read(4))
        if rip_len < 16 + 1 + 4 + 12 or rip_len > size:
            out.append(f"RIP fehlt oder ist beschaedigt (Laengenangabe {rip_len} am Dateiende) - Datei abgeschnitten?")
        else:
            f.seek(size - rip_len)
            raw = f.read(rip_len)
            ok = len(raw) == rip_len and raw[:7] == _PK_PREFIX and raw[8:13] == _PK_TAIL and raw[13:16] == b"\x11\x01\x00"
            body = b""
            if ok:
                b = raw[16]
                if b < 0x80:
                    ln, nb = b, 1
                else:
                    nb = (b & 0x7F) + 1
                    ln = int.from_bytes(raw[17:16 + nb], "big") if nb <= 9 else -1
                body = raw[16 + nb:16 + nb + ln] if ln >= 0 else b""
                ok = ln >= 4 + 12 and len(body) == ln and 16 + nb + ln == rip_len and ((ln - 4) % 12) == 0
            if not ok:
                out.append("RIP am Dateiende hat keine passende Laenge/Struktur - Datei abgeschnitten oder beschaedigt")
            else:
                rip_start = size - rip_len
                entries = [struct.unpack(">IQ", body[i:i + 12]) for i in range(0, ln - 4, 12)]
                out += _rip_problems(f, size, entries, footer, foff, rip_start)
    # Mindestgroesse: Header + Frames x Tonbytes (faengt Abschneiden, nicht genullte Essenz)
    if frames and n_audio and fps:
        try:
            per_frame = (48000 * fps.denominator) // fps.numerator * 3 * n_audio
            header_end = hp["end"] + hp["hbc"] + hp["ibc"]
            need = header_end + frames * per_frame
            if size < need:
                out.append(f"Datei zu klein: {size} Bytes, mindestens {need} erwartet ({frames} Frames Ton + Kopfdaten) - abgeschnitten?")
        except (ZeroDivisionError, AttributeError):
            pass
    return out


def _rip_problems(f, size: int, entries: list, footer, foff: int, rip_start: int) -> list[str]:
    out: list[str] = []
    if len(entries) > _MAX_RIP_ENTRIES:
        return [f"RIP mit {len(entries)} Eintraegen unplausibel"]
    offs = [o for _sid, o in entries]
    if offs != sorted(offs) or len(set(offs)) != len(offs):
        out.append("RIP: Offsets nicht aufsteigend/eindeutig")
    if not offs or offs[0] != 0:
        out.append("RIP: erster Eintrag zeigt nicht auf die Header-Partition (Offset 0)")
    packs = []
    for o in offs:
        try:
            if o >= rip_start:
                raise ValueError(f"RIP-Eintrag {o} liegt hinter dem Beginn des RIP ({rip_start})")
            pk = _read_pack(f, o, size)
            if pk["this"] != o:
                raise ValueError(f"ThisPartition {pk['this']} passt nicht zum RIP-Eintrag {o}")
            packs.append(pk)
        except ValueError as e:
            out.append(f"RIP: {e}")
    if packs and len(packs) == len(offs):
        if packs[-1]["kind"] != 4:
            out.append("RIP: letzter Eintrag ist keine Footer-Partition")
        elif foff and packs[-1]["offset"] != foff:
            out.append(f"RIP: letzter Eintrag ({packs[-1]['offset']}) != FooterPartition-Offset ({foff})")
        for a, b in zip(packs, packs[1:]):
            if a["end"] + a["hbc"] + a["ibc"] > b["offset"]:
                out.append(f"Partition bei {a['offset']}: Header-/Index-Bytes ragen in die naechste Partition")
        last = packs[-1]
        if last["end"] + last["hbc"] + last["ibc"] > rip_start:
            out.append("Footer-Partition: Header-/Index-Bytes ragen ueber das RIP hinaus")
    return out


# --------------------------------------------------------------------------- H.264-Eigenschaften


def check_h264_props(probe: "mxfinfo.ProbeResult", n_audio: Optional[int] = None, level: Optional[str] = None) -> list[str]:
    """Pflichteigenschaften des Ziel-Videos aus einem ffprobe-Ergebnis (gemeinsam fuer Fortsetzen
    und --verify): H.264 High 4:2:2, yuv422p10le, 1920x1080, tv, bt709 (Space/Transfer/Primaries),
    Level (z. B. "4.2"), Anzahl Audiostreams, Audio pcm_s24le/48000."""
    out: list[str] = []
    pv = probe.video or {}
    if pv.get("codec_name") != "h264" or pv.get("profile") != "High 4:2:2":
        out.append(f"H.264-Profil {pv.get('codec_name')}/{pv.get('profile')} statt h264/High 4:2:2")
    if pv.get("pix_fmt") != "yuv422p10le":
        out.append(f"pix_fmt {pv.get('pix_fmt')} statt yuv422p10le")
    if (pv.get("width"), pv.get("height")) != ("1920", "1080"):
        out.append(f"Aufloesung {pv.get('width')}x{pv.get('height')}")
    for k in ("color_space", "color_transfer", "color_primaries"):
        if pv.get(k) != "bt709":
            out.append(f"{k} {pv.get(k)} statt bt709")
    if pv.get("color_range") != "tv":
        out.append(f"color_range {pv.get('color_range')} statt tv")
    if level and pv.get("level") != level.replace(".", ""):
        out.append(f"H.264-Level {pv.get('level')} statt {level.replace('.', '')}")
    if n_audio is not None and len(probe.audio) != n_audio:
        out.append(f"ffprobe sieht {len(probe.audio)} Audiospuren statt {n_audio}")
    for i, a in enumerate(probe.audio):
        if a.get("codec_name") != "pcm_s24le" or a.get("sample_rate") != "48000":
            out.append(f"Audiostream {i + 1}: {a.get('codec_name')}/{a.get('sample_rate')} Hz statt pcm_s24le/48000")
    return out


def check_clip(
    path: Path,
    exp: ClipExpect,
    cancel: Optional[tools.CancelToken] = None,
    source: Optional[Path] = None,
    source_info: Optional[mxfinfo.MxfInfo] = None,
) -> list[str]:
    """Prueft einen Ziel-Clip gegen die Erwartung. Leere Liste = in Ordnung.

    Mit ``source`` (Quell-MXF) werden zusaetzlich die Sony-Kopfdaten gegen die Quelle geprueft
    (``check_header``); ohne ``source`` nur die uebrigen Eigenschaften."""
    problems: list[str] = []
    # 1. Dateistruktur (schnell): abgeschnittene/unvollstaendige Dateien sofort erkennen
    problems = [f"Struktur: {m}" for m in check_structure(path, frames=exp.frames, n_audio=exp.n_audio, fps=exp.fps)]
    if problems:
        return problems
    try:
        info = mxfinfo.read_mxf_info(str(path), cancel=cancel)
    except tools.Cancelled:
        raise
    except Exception as e:  # noqa: BLE001
        return [f"MXF nicht lesbar: {e}"]
    v = info.video
    if v is None:
        return ["keine Videospur"]
    if v.essence_type != XAVCL_ESSENCE_TYPE:
        problems.append(f"essence_type {v.essence_type!r} statt {XAVCL_ESSENCE_TYPE!r}")
    if v.descriptor.get("coding_label") != XAVCL_CODING_LABEL:
        problems.append(f"coding_label {v.descriptor.get('coding_label')!r} statt {XAVCL_CODING_LABEL!r}")
    if v.ec_label != XAVCL_EC_LABEL:
        problems.append(f"ec_label {v.ec_label!r} statt {XAVCL_EC_LABEL!r}")
    if info.duration != exp.frames:
        problems.append(f"Dauer {info.duration} Frames statt {exp.frames}")
    if info.edit_rate != exp.fps:
        problems.append(f"Bildrate {info.edit_rate} statt {exp.fps}")
    if info.mp_uid != exp.mp_uid:
        problems.append(f"Material-Package-UMID {info.mp_uid} statt {exp.mp_uid}")
    if info.fp_uid != exp.fp_uid:
        problems.append(f"File-Package-UMID {info.fp_uid} statt {exp.fp_uid}")
    if info.start_tc != exp.start_tc:
        problems.append(f"Start-Timecode {info.start_tc} statt {exp.start_tc}")
    if len(info.audio) != exp.n_audio:
        problems.append(f"{len(info.audio)} Audiospuren statt {exp.n_audio}")
    for i, a in enumerate(info.audio):
        d = a.descriptor
        if d.get("bits_per_sample") != "24" or d.get("sampling_rate") != "48000/1" or d.get("channel_count") != "1":
            problems.append(f"Audiospur {i + 1}: unerwartetes Format {d}")
        if a.duration != exp.frames:
            problems.append(f"Audiospur {i + 1}: Dauer {a.duration} statt {exp.frames}")
        if a.ec_label != AUDIO_EC_LABEL:
            problems.append(f"Audiospur {i + 1}: ec_label {a.ec_label} statt {AUDIO_EC_LABEL}")
    if v.duration != exp.frames:
        problems.append(f"Videospur: Dauer {v.duration} statt {exp.frames}")
    if exp.expect_anc and not info.anc_tracks:
        problems.append("ANC-Spur fehlt")
    if exp.expect_xml and not any(t.get("scheme_id") == NRT_SCHEME_ID for t in info.text_objects):
        problems.append("eingebettetes XML fehlt")
    # H.264-/Audio-Eigenschaften (ein ffprobe-Aufruf)
    try:
        problems += check_h264_props(mxfinfo.run_ffprobe(str(path), cancel=cancel), exp.n_audio, exp.level)
    except tools.Cancelled:
        raise
    except Exception as e:  # noqa: BLE001
        problems.append(f"ffprobe-Pruefung: {e}")
    if source is not None:
        problems += check_header(path, source, cancel=cancel, info=info, src_info=source_info)
        problems += check_audio_content(path, source, cancel=cancel, fps=info.edit_rate,
                                        duration=(source_info.duration if source_info is not None else info.duration))
    return problems


# --------------------------------------------------------------------------- Kopfdaten (Runde 2)

_AUDIO_DESC_KEYS = ("sampling_rate", "bits_per_sample", "block_align", "channel_count", "locked", "audio_ref_level")
_IDENT_FIELDS = (
    ("company_name", "ident_company"),
    ("product_name", "ident_product"),
    ("version_string", "ident_version_string"),
    ("product_uid", "ident_product_uid"),
    ("product_version", "ident_product_version"),
    ("toolkit_version", "ident_toolkit_version"),
    ("platform", "ident_platform"),
)


def _by_kind(info: mxfinfo.MxfInfo) -> dict:
    return {"Picture": [t for t in info.tracks if t.kind == "Picture"],
            "Sound": [t for t in info.tracks if t.kind == "Sound"],
            "Data": [t for t in info.tracks if t.kind == "Data"]}


def compare_info_header(info: mxfinfo.MxfInfo, src: mxfinfo.MxfInfo) -> list[str]:
    """Kopfdaten-Vergleich auf Basis der mxf2raw-Felder (reine Logik): Identification, Track-IDs
    (Material und File Package), Track-Nummern der Tonspuren, Audio-Deskriptor-Felder."""
    out: list[str] = []
    for label, attr in _IDENT_FIELDS:
        a, b = getattr(info, attr), getattr(src, attr)
        if a != b:
            out.append(f"Identification {label}: {a!r} statt {b!r} (Quelle)")
    ti, si = _by_kind(info), _by_kind(src)
    for kind, name in (("Picture", "Video"), ("Sound", "Audio"), ("Data", "Daten")):
        for i, (t, s) in enumerate(zip(ti[kind], si[kind]), 1):
            tag = f"{name}spur" + (f" {i}" if kind != "Picture" else "")
            if t.mp_track_id != s.mp_track_id:
                out.append(f"{tag}: Track-ID im Material Package {t.mp_track_id} statt {s.mp_track_id} (Quelle)")
            if t.fp_track_id != s.fp_track_id:
                out.append(f"{tag}: Track-ID im File Package {t.fp_track_id} statt {s.fp_track_id} (Quelle)")
            if kind == "Sound":
                if t.fp_track_number != s.fp_track_number:
                    out.append(f"{tag}: Track-Nummer {t.fp_track_number or '?'} statt {s.fp_track_number or '?'} (Quelle)")
                for k in _AUDIO_DESC_KEYS:
                    if t.descriptor.get(k) != s.descriptor.get(k):
                        out.append(f"{tag}: Deskriptor {k} {t.descriptor.get(k)!r} statt {s.descriptor.get(k)!r} (Quelle)")
    return out


def check_sony_pattern(info: mxfinfo.MxfInfo) -> list[str]:
    """Plausibilitaet ohne Quelle: sieht die Datei in den Kopfdaten wie eine Sony-Kameradatei aus
    (Muster der FX6-Referenz: Video-Track-ID 2, Audio 3..N+2, ANC N+3, kein bmx/BBC/Toolkit)?"""
    out: list[str] = []
    if info.ident_company.strip().upper() == "BBC":
        out.append(f"Identification company_name {info.ident_company!r} (bmx-Kennung)")
    if info.ident_product.strip().lower() == "bmx":
        out.append(f"Identification product_name {info.ident_product!r} (bmx-Kennung)")
    if info.ident_toolkit_version:
        out.append(f"Identification toolkit_version {info.ident_toolkit_version!r} vorhanden (Sony schreibt keins)")
    if info.ident_platform:
        out.append(f"Identification platform {info.ident_platform!r} vorhanden (Sony schreibt keins)")
    k = _by_kind(info)
    expected: list = []  # (Spur, erwartete ID, Name)
    for t in k["Picture"][:1]:
        expected.append((t, 2, "Videospur"))
    n_audio = len(k["Sound"])
    for i, t in enumerate(k["Sound"]):
        expected.append((t, 3 + i, f"Audiospur {i + 1}"))
    for i, t in enumerate(k["Data"]):
        if t.essence_type == "ANC_Data":
            expected.append((t, n_audio + 3, "ANC-Spur"))
    for t, want, name in expected:
        if t.mp_track_id != want:
            out.append(f"{name}: Track-ID im Material Package {t.mp_track_id} statt {want}")
        if t.fp_track_id != want:
            out.append(f"{name}: Track-ID im File Package {t.fp_track_id} statt {want}")
    return out


def check_header(
    path: Path,
    source: Path,
    cancel: Optional[tools.CancelToken] = None,
    info: Optional[mxfinfo.MxfInfo] = None,
    src_info: Optional[mxfinfo.MxfInfo] = None,
    notes: Optional[list] = None,
) -> list[str]:
    """Pruefung der Sony-Kopfdaten eines Ziel-Clips gegen die Quell-MXF.  Liefert nur Probleme
    der Kopfdaten (getrennt von den uebrigen Pruefungen, damit "nur Kopfdaten nachziehen" moeglich ist).
    Hinweise (keine Abweichungen) werden, falls ``notes`` angegeben ist, dort angehaengt."""
    out: list[str] = []
    try:
        # compare_with_source beginnt seine Saetze mit "Abweichung: " - nicht doppelt praefixen
        out += [m[len("Abweichung: "):] if m.startswith("Abweichung: ") else m
                for m in mxfheader.compare_with_source(path, source)]
    except tools.Cancelled:
        raise
    except (mxfheader.HeaderPatchError, mxfheader.MxfParseError, OSError) as e:
        out.append(f"Kopfdaten nicht pruefbar: {e}")
    out += check_header_direct(path, source, notes=notes)
    try:
        if info is None:
            info = mxfinfo.read_mxf_info(str(path), cancel=cancel)
        if src_info is None:
            src_info = mxfinfo.read_mxf_info(str(source), cancel=cancel)
    except tools.Cancelled:
        raise
    except Exception as e:  # noqa: BLE001
        out.append(f"Kopfdaten (mxf2raw) nicht lesbar: {e}")
        return out
    out += compare_info_header(info, src_info)
    return out


_ZERO_UMIDS = (bytes(32), bytes(16))
_IDENT_ITEMS = ("CompanyName", "ProductName", "VersionString", "ProductUID", "ProductVersion",
                "ToolkitVersion", "Platform")


def _track_ids(pkg) -> dict:
    out: dict = {}
    for t in pkg.tracks:
        if t.kind in ("timecode", "picture", "sound", "data"):
            out.setdefault(t.kind, []).append(t.track_id)
    return {k: sorted(x for x in v if x is not None) for k, v in out.items()}


def check_header_direct(path: Path, source: Path, notes: Optional[list] = None) -> list[str]:
    """Direkte, vom Patcher (``compare_with_source``) unabhaengige Pruefung der Kopfdaten ueber
    ``mxfheader.read_header`` (nur Lesen): je Package gleiche Track-IDs von Bild-/Ton-/Daten-Tracks
    wie die Quelle; jeder Timecode-Track des Ziels traegt eine ID, die auch ein Timecode-Track der
    Quelle im selben Package hat (zusaetzliche Timecode-Tracks der Quelle sind nur ein Hinweis, er
    wird an ``notes`` angehaengt, falls angegeben); jeder SourceClip im Material Package zeigt auf
    einen existierenden Track des File Packages; jede LinkedTrackID eines Deskriptors existiert;
    Identification-Felder (die ERSTE Identification, maßgeblich ist die urspruengliche Erzeugung)
    byte-gleich zur Quelle."""
    out: list[str] = []
    try:
        ht, hs = mxfheader.read_header(path), mxfheader.read_header(source)
        st, ss = mxfheader.resolve(ht), mxfheader.resolve(hs)
    except (mxfheader.MxfParseError, mxfheader.HeaderPatchError, OSError, ValueError, KeyError, IndexError) as e:
        return [f"Kopfdaten (direkt) nicht lesbar: {e}"]
    try:
        for label, tp, sp in (("Material Package", st.material, ss.material), ("File Package", st.file, ss.file)):
            if tp is None or sp is None:
                if (tp is None) != (sp is None):
                    out.append(f"{label}: im Ziel {'fehlt' if tp is None else 'vorhanden'}, in der Quelle {'vorhanden' if tp is None else 'fehlt'}")
                continue
            a, b = _track_ids(tp), _track_ids(sp)
            for kind in ("timecode", "picture", "sound", "data"):
                x, y = a.get(kind, []), b.get(kind, [])
                if kind == "data" and not x:
                    continue  # ANC fehlt im Ziel: wird getrennt gemeldet (ggf. gewollt bei --no-anc)
                if kind == "timecode":
                    # jeder Ziel-Timecode-Track muss eine ID eines Timecode-Tracks der Quelle haben;
                    # weitere Timecode-Tracks der Quelle fehlen im Ziel bewusst (nur Hinweis)
                    if any(i not in y for i in x):
                        out.append(f"{label}: Track-IDs {kind} {x} statt {y} (Quelle)")
                    elif len(y) > len(x) and notes is not None:
                        notes.append(f"{label}: Quelle hat zusaetzliche Timecode-Tracks "
                                     f"(IDs {[i for i in y if i not in x]}), im Ziel nicht vorhanden")
                    continue
                if x != y:
                    out.append(f"{label}: Track-IDs {kind} {x} statt {y} (Quelle)")
        if st.file is not None:
            fids = {t.track_id for t in st.file.tracks}
            fuid = st.file.uid
            if st.material is not None:
                for t in st.material.tracks:
                    for c in t.clips:
                        pid = ht.get(c, "SourceClip.SourcePackageID")
                        tid = ht.u32(c, "SourceClip.SourceTrackID")
                        if pid is None or pid in _ZERO_UMIDS or pid != fuid:
                            continue
                        if tid not in fids:
                            out.append(f"Material Package: SourceClip (Track {t.track_id}) verweist auf nicht vorhandenen "
                                       f"Track {tid} des File Packages")
            for d in st.file_descriptors():
                for ref in ("FileDescriptor.LinkedTrackID", "AES3AudioDescriptor.LinkedTimecodeTrackID"):
                    v = ht.u32(d, ref)
                    if v is not None and v not in fids:
                        out.append(f"Deskriptor {ref.split('.')[1]} {v} zeigt auf keinen Track des File Packages")
        it = st.identifications[0] if st.identifications else None
        isr = ss.identifications[0] if ss.identifications else None
        if it is None or isr is None:
            if (it is None) != (isr is None):
                out.append("Identification: nur in " + ("der Quelle" if it is None else "im Ziel") + " vorhanden")
        else:
            for f in _IDENT_ITEMS:
                x, y = ht.get(it, "Identification." + f), hs.get(isr, "Identification." + f)
                if x != y:
                    out.append(f"Identification {f}: {x.hex() if x is not None else '(fehlt)'} statt "
                               f"{y.hex() if y is not None else '(fehlt)'} (Quelle, Hex)")
    except tools.Cancelled:
        raise
    except Exception as e:  # noqa: BLE001
        out.append(f"Kopfdaten (direkt) Pruefung fehlgeschlagen: {e}")
    return out


# --------------------------------------------------------------------------- Tonspuren-Inhalt


def _extract_audio(path, start: int, dur: int, directory: str, tag: str,
                   cancel: Optional[tools.CancelToken]) -> list[bytes]:
    """Rohe PCM-Daten (24 Bit) aller Tonspuren von ``dur`` Edit-Units ab ``start`` per mxf2raw --ess-out.
    mxf2raw liest dabei nur die Tonspuren des gewaehlten Bereichs (Video/Daten abgeschaltet); die Dateien
    heissen ``<prefix>_a<N>.raw`` mit N = Tonspur in der Reihenfolge von ``mxf2raw -i``.  Gibt die Inhalte
    in dieser Reihenfolge zurueck; wirft RuntimeError."""
    prefix = os.path.join(directory, tag)
    res = tools.run_capture(
        [tools.find_tool("mxf2raw"), "--ess-out", prefix, "--start", str(start), "--dur", str(dur),
         "--disable-video", "--disable-data", str(path)],
        cancel=cancel, keep_lines=30,
    )
    found = []
    for name in os.listdir(directory):
        m = re.fullmatch(re.escape(tag) + r"_a(\d+)\.raw", name)
        if m:
            found.append((int(m.group(1)), name))
    if res.returncode != 0 or not found:
        tail = (res.stderr or res.stdout).strip()[-200:]
        raise RuntimeError(f"mxf2raw --ess-out fuer {os.path.basename(str(path))} fehlgeschlagen: {tail or 'keine Tondaten'}")
    out = []
    for _n, name in sorted(found):
        fp = os.path.join(directory, name)
        with open(fp, "rb") as f:
            out.append(f.read())
        os.remove(fp)  # gleich wieder freigeben (nur wenige MB, aber Verzeichnis wird wiederverwendet)
    return out


def _compare_audio(target: list[bytes], source: list[bytes], where: str = "") -> list[str]:
    out: list[str] = []
    if len(target) != len(source):
        return [f"Tonspuren{where}: {len(target)} extrahiert statt {len(source)} (Quelle)"]
    for i, (t, s) in enumerate(zip(target, source), 1):
        n = min(len(t), len(s))  # Testausgaben (--max-frames) sind kuerzer: gemeinsame Laenge
        if n == 0:
            if len(t) != len(s):
                out.append(f"Tonspur {i}{where}: keine Audiodaten lesbar (Ziel {len(t)} Bytes, Quelle {len(s)} Bytes)")
            continue
        if t[:n] == s[:n]:
            continue
        msg = f"Tonspur {i}{where}: Inhalt weicht von der Quelle ab (Spuren vertauscht?)"
        others = [k for k, o in enumerate(source, 1) if k != i and o[:n] == t[:n]]
        if others:
            msg += f" - entspricht dem Inhalt von Quell-Tonspur {', '.join(str(k) for k in others)}"
        out.append(msg)
    return out


def check_audio_content(
    target,
    source,
    cancel: Optional[tools.CancelToken] = None,
    seconds: float = 1.0,
    fps: Optional[Fraction] = None,
    duration: Optional[int] = None,
) -> list[str]:
    """Vergleicht den Ton-INHALT: je Tonspur die ersten ``seconds`` Sekunden von Ziel und Quelle
    (mxf2raw --ess-out mit --start/--dur, Video/Daten abgeschaltet, in einem temporaeren Verzeichnis,
    das danach geloescht wird) byte-genau, Spur fuer Spur in der Reihenfolge von mxf2raw.  Faengt vertauschte
    oder falsch zugeordnete Tonspuren, die in den Kopfdaten nicht sichtbar sind.  Bei ``--max-frames``-
    Testausgaben ist das Ziel kuerzer: es zaehlt die gemeinsame Laenge.  Sind in diesem Fenster nicht alle
    Quellspuren verschieden (z. B. Stille), werden zusaetzlich Fenster bei 1/3 und 2/3 der Dauer geprueft.
    ``fps``/``duration`` (Bildrate, Frames der Quelle) sparen einen mxf2raw-Aufruf, wenn bekannt.
    Leere Liste = in Ordnung."""
    try:
        if fps is None or not duration:
            si = mxfinfo.read_mxf_info(str(source), cancel=cancel)
            fps = fps or si.edit_rate
            duration = duration or si.duration
        n = max(1, math.ceil(seconds * float(fps or 25)))
        with tempfile.TemporaryDirectory(prefix="xdroot_audiochk_") as td:
            src = _extract_audio(source, 0, n, td, "s", cancel)
            tgt = _extract_audio(target, 0, n, td, "t", cancel)
            out = _compare_audio(tgt, src)
            if not out and len(src) > 1 and len(set(src)) < len(src) and duration:
                for frac, label in ((1 / 3, " (Fenster bei 1/3)"), (2 / 3, " (Fenster bei 2/3)")):
                    start = int(duration * frac)
                    if start < 1 or start >= duration:
                        continue
                    try:
                        s2 = _extract_audio(source, start, n, td, "s", cancel)
                        t2 = _extract_audio(target, start, n, td, "t", cancel)
                    except RuntimeError:
                        continue  # z. B. kuerzere Testausgabe: Fenster liegt hinter dem Ende des Ziels
                    out += _compare_audio(t2, s2, label)
                    if out or len(set(s2)) == len(s2):
                        break
            return out
    except tools.Cancelled:
        raise
    except Exception as e:  # noqa: BLE001
        return [f"Tonspuren-Inhalt nicht pruefbar: {e}"]


def tail_decode_check(path: str, frames: int, fps: Optional[Fraction], cancel: Optional[tools.CancelToken] = None,
                      tail_frames: int = 25) -> list[str]:
    """Kurzer Decode-Test am ENDE des Clips (ca. die letzten ``tail_frames`` Frames, Video und Ton):
    faengt Clips mit fehlendem/defektem Ende auch ohne --deep.  Springt per Zeit-Seek (bmx-Indextabelle)
    eine halbe Frame-Dauer vor das erste Test-Frame; erwartet mindestens so viele dekodierte Frames."""
    if not frames or not fps or fps <= 0:
        return []
    n = min(tail_frames, frames)
    start = frames - n
    seek = max(0.0, (float(start) - 0.5) / float(fps))
    got = [0]

    def on_line(line: str) -> None:
        if line.startswith("frame="):
            try:
                got[0] = int(line.split("=", 1)[1].strip())
            except ValueError:
                pass

    res = tools.run_capture(
        [tools.find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error", "-nostats",
         "-progress", "pipe:1", "-ss", f"{seek:.6f}", "-i", path, "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"],
        cancel=cancel, on_stdout_line=on_line, keep_lines=200,
    )
    out: list[str] = []
    errs = [l for l in res.stderr.splitlines() if l.strip()]
    if errs:
        out.append(f"Decodierfehler am Clip-Ende: {errs[:3]}")
    elif res.returncode != 0:
        out.append(f"ffmpeg Rueckgabewert {res.returncode} beim Decodieren des Clip-Endes")
    if got[0] < n:
        out.append(f"Clip-Ende: nur {got[0]} von {n} erwarteten Frames dekodierbar (Ende fehlt/defekt?)")
    return out


# --------------------------------------------------------------------------- Tiefenpruefung


def deep_decode_check(path: str, cancel: Optional[tools.CancelToken] = None) -> tuple[int, list[str]]:
    """Dekodiert Video und Audio komplett; liefert (Videoframes, Fehlerzeilen)."""
    frames = [0]

    def on_line(line: str) -> None:
        if line.startswith("frame="):
            try:
                frames[0] = int(line.split("=", 1)[1].strip())
            except ValueError:
                pass

    res = tools.run_capture(
        [tools.find_tool("ffmpeg"), "-hide_banner", "-nostdin", "-v", "error", "-nostats",
         "-progress", "pipe:1", "-i", path, "-map", "0:v:0", "-map", "0:a?", "-f", "null", "-"],
        cancel=cancel,
        on_stdout_line=on_line,
        keep_lines=200,
    )
    errs = [l for l in res.stderr.splitlines() if l.strip()]
    if res.returncode != 0 and not errs:
        errs = [f"ffmpeg Rueckgabewert {res.returncode}"]
    return frames[0], errs


def measure_gop(path: str, packets: int = 400, cancel: Optional[tools.CancelToken] = None) -> list[int]:
    """Abstaende der Schluesselbilder (in Paketen, Dekodierreihenfolge) in den ersten ``packets`` Paketen."""
    res = tools.run_capture(
        [tools.find_tool("ffprobe"), "-v", "error", "-select_streams", "v:0",
         "-read_intervals", f"%+#{packets}", "-show_entries", "packet=flags", "-of", "csv=p=0", path],
        cancel=cancel,
    )
    keys = [i for i, l in enumerate(res.stdout.splitlines()) if l.strip().startswith("K")]
    return [b - a for a, b in zip(keys, keys[1:])]


# --------------------------------------------------------------------------- Karte pruefen


@dataclass
class VerifyReport:
    problems: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    clips_checked: int = 0
    clips_ok: int = 0

    @property
    def ok(self) -> bool:
        return not self.problems

    def text(self) -> str:
        lines = []
        for n in self.notes:
            lines.append("  Hinweis: " + n)
        for p in self.problems:
            lines.append("  ABWEICHUNG: " + p)
        lines.append(
            f"{self.clips_checked} Clips geprueft, {self.clips_ok} ohne Abweichung, {len(self.problems)} Abweichung(en)."
        )
        return "\n".join(lines)


def _fps_matches(label: str, rate: Optional[Fraction]) -> bool:
    m = re.match(r"^(\d+(?:\.\d+)?)[pi]?$", label.strip())
    if not m or rate is None:
        return True
    return abs(float(m.group(1)) - float(rate)) < 0.02


def _read_test_marker(target_dir: Path) -> Optional[int]:
    f = target_dir / TEST_MARKER
    if not f.is_file():
        return None
    for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
        m = re.match(r"\s*max_frames\s*=\s*(\d+)", line)
        if m:
            return int(m.group(1))
    return None


SETTINGS_NAME = "konvertierung_einstellungen.json"  # wie converter.SETTINGS_NAME (hier nicht importiert)
# Felder je Clip, die "ANC wurde bewusst/zwangsweise nicht uebernommen" anzeigen (defensiv gelesen,
# unbekannte Felder werden ignoriert)
_ANC_DROPPED_FLAGS = ("anc_failed", "anc_dropped", "anc_skipped", "anc_omitted", "anc_missing", "anc_uebernahme_gescheitert")


def _anc_dropped_clips(target_dir: Path) -> dict:
    """{Clip-Pfad klein geschrieben ('clip/name.mxf'): Grund} der Clips, bei denen laut
    ``konvertierung_einstellungen.json`` im Zielordner ANC nicht uebernommen wurde (``keep_anc`` false oder
    ein Fehlerfeld wie ``anc_failed``).  Fehlende/unlesbare Datei = leer."""
    try:
        data = json.loads((target_dir / SETTINGS_NAME).read_text(encoding="utf-8"))
        clips = data.get("clips") if isinstance(data, dict) else None
        if not isinstance(clips, dict):
            return {}
    except (OSError, ValueError):
        return {}
    out: dict = {}
    for rel, entry in clips.items():
        if not isinstance(entry, dict) or not isinstance(rel, str):
            continue
        if entry.get("keep_anc") is False:
            out[rel.replace("\\", "/").casefold()] = "ANC-Uebernahme war abgeschaltet (--no-anc)"
        elif any(entry.get(k) is True or (isinstance(entry.get(k), str) and entry.get(k)) for k in _ANC_DROPPED_FLAGS):
            out[rel.replace("\\", "/").casefold()] = "ANC-Uebernahme ist beim Umwandeln gescheitert (laut Einstellungsdatei)"
    return out


def _same_content(a: Path, b: Path, chunk: int = 4 * 1024 * 1024) -> bool:
    """Byte-gleich (Groesse + Inhalt, blockweise gelesen); False bei jedem Fehler."""
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        with open(a, "rb") as fa, open(b, "rb") as fb:
            while True:
                x, y = fa.read(chunk), fb.read(chunk)
                if x != y:
                    return False
                if not x:
                    return True
    except OSError:
        return False


def _selection_record(target_dir: Path) -> Optional[dict]:
    """``auswahl`` (``{"unlisted": "skip"|"convert", "only": [...]}``) des letzten Laufs laut
    ``konvertierung_einstellungen.json`` im Zielordner; None = nicht vorhanden/unlesbar/ohne Auswahl.
    Defensiv gelesen: unbekannte oder falsch typisierte Felder werden ignoriert."""
    try:
        data = json.loads((target_dir / SETTINGS_NAME).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    sel = data.get("auswahl") if isinstance(data, dict) else None
    if not isinstance(sel, dict):
        return None
    only = sel.get("only")
    only = [t for t in only if isinstance(t, str) and t] if isinstance(only, list) else []
    unlisted = sel.get("unlisted") if sel.get("unlisted") in ("skip", "convert") else "convert"
    return {"unlisted": unlisted, "only": only}


def verify_card(
    target: str,
    source: Optional[str] = None,
    deep: bool = False,
    log: Callable[[str], None] = lambda s: None,
    cancel: Optional[tools.CancelToken] = None,
) -> VerifyReport:
    rep = VerifyReport()
    tgt = card.find_xdroot(target)
    test_frames = _read_test_marker(tgt.parent)
    if test_frames:
        rep.notes.append(f"Testausgabe (--max-frames {test_frames}): Dauer-Vergleiche mit MEDIAPRO/Quelle werden angepasst.")

    mp = card.parse_mediapro((tgt / "MEDIAPRO.XML").read_bytes())
    src_root = card.find_xdroot(source) if source else None
    src_mp = card.parse_mediapro((src_root / "MEDIAPRO.XML").read_bytes()) if src_root else None

    mxfs = card.list_clip_mxf(tgt)
    anc_dropped = _anc_dropped_clips(tgt.parent)
    gop_ok: list[int] = []
    listed = {m.rel_path.casefold() for m in mp.materials}
    for m in mp.materials:
        if not (tgt / m.rel_path).is_file():
            rep.problems.append(f"{m.rel_path}: in MEDIAPRO.XML gelistet, aber Datei fehlt im Ziel")

    for mxf_path in mxfs:
        if cancel:
            cancel.check()
        rel = f"Clip/{mxf_path.name}"
        stem = mxf_path.stem
        rep.clips_checked += 1
        before = len(rep.problems)

        def prob(msg: str, _rel=rel) -> None:
            rep.problems.append(f"{_rel}: {msg}")

        mat = mp.find(rel)
        if mat is None:
            rep.notes.append(f"{rel}: nicht in MEDIAPRO.XML gelistet")
        try:
            info = mxfinfo.read_mxf_info(str(mxf_path), cancel=cancel)
        except tools.Cancelled:
            raise
        except Exception as e:  # noqa: BLE001
            if src_root is not None and _same_content(src_root / "Clip" / mxf_path.name, mxf_path):
                # nicht analysierbare Quelle (Muell, 0 Byte, abgebrochene Aufnahme): der Konverter kopiert sie unveraendert
                first = (str(e).strip().splitlines() or ["?"])[0][:120]
                rep.notes.append(f"{rel}: nicht analysierbar, unveraendert uebernommen (byte-gleich mit der Quelle): {first}")
                rep.clips_ok += 1
                continue
            prob(f"MXF nicht lesbar: {e}")
            for m in check_structure(mxf_path):
                prob(f"Struktur: {m}")
            continue
        v = info.video
        is_l = v is not None and v.essence_type == XAVCL_ESSENCE_TYPE
        # XML des Clips
        xml_files = [p for p in (tgt / "Clip").iterdir() if re.match(re.escape(stem) + r"M\d\d\.XML$", p.name, re.I)]
        cx = card.parse_clip_xml(xml_files[0].read_bytes()) if xml_files else None
        if cx is None:
            src_xml = [p for p in (src_root / "Clip").iterdir()
                       if re.match(re.escape(stem) + r"M\d\d\.XML$", p.name, re.I)] if src_root is not None else None
            if src_xml == []:
                rep.notes.append(f"{rel}: keine M01.XML (auch in der Quelle nicht vorhanden)")
            else:
                prob("keine M01.XML vorhanden")
        if mat is not None:
            if mat.umid != info.mp_uid:
                prob(f"MEDIAPRO umid {mat.umid} != MXF Material-Package {info.mp_uid}")
            if mat.dur is not None and not test_frames and mat.dur != info.duration:
                prob(f"MEDIAPRO dur {mat.dur} != MXF Dauer {info.duration}")
            if mat.ch is not None and mat.ch != len(info.audio):
                prob(f"MEDIAPRO ch {mat.ch} != MXF Audiospuren {len(info.audio)}")
            if not _fps_matches(mat.fps, info.edit_rate):
                prob(f"MEDIAPRO fps {mat.fps} != MXF {info.edit_rate}")
            if card.is_xavcl_codec(mat.video_type) != is_l:
                prob(f"MEDIAPRO videoType {mat.video_type} passt nicht zu MXF essence_type {v.essence_type if v else None}")
            if cx is not None and cx.video_codec != mat.video_type:
                prob(f"M01.XML videoCodec {cx.video_codec} != MEDIAPRO videoType {mat.video_type}")
        if cx is not None:
            if cx.umid_ref != info.mp_uid:
                prob(f"M01.XML umidRef {cx.umid_ref} != MXF Material-Package {info.mp_uid}")
            if cx.duration is not None and not test_frames and cx.duration != info.duration:
                prob(f"M01.XML Duration {cx.duration} != MXF Dauer {info.duration}")
            if cx.num_audio is not None and cx.num_audio != len(info.audio):
                prob(f"M01.XML numOfChannel {cx.num_audio} != MXF Audiospuren {len(info.audio)}")
            if card.is_xavcl_codec(cx.video_codec) != is_l:
                prob(f"M01.XML videoCodec {cx.video_codec} passt nicht zu MXF essence_type {v.essence_type if v else None}")
        if not is_l:
            rep.notes.append(f"{rel}: kein XAVC-L ({v.essence_type if v else 'ohne Video'}) - unveraendert uebernommen?")
        else:
            if v.descriptor.get("coding_label") != XAVCL_CODING_LABEL:
                prob(f"coding_label {v.descriptor.get('coding_label')}")
            if v.ec_label != XAVCL_EC_LABEL:
                prob(f"ec_label {v.ec_label}")
            for i, a in enumerate(info.audio):
                d = a.descriptor
                if d.get("bits_per_sample") != "24" or d.get("sampling_rate") != "48000/1":
                    prob(f"Audiospur {i + 1}: {d}")
            if any(a.duration != info.duration for a in info.audio):
                prob("Audiospuren haben nicht dieselbe Dauer wie das Video")
            for m in check_structure(mxf_path, frames=info.duration, n_audio=len(info.audio), fps=info.edit_rate):
                prob(f"Struktur: {m}")
            # ffprobe
            try:
                pr = mxfinfo.run_ffprobe(str(mxf_path), cancel=cancel)
                exp_level = card.level_from_codec(mat.video_type if mat else (cx.video_codec if cx else ""))
                for m in check_h264_props(pr, len(info.audio), exp_level):
                    prob(m)
                if info.edit_rate is not None:
                    want = mxfinfo.gop_length(info.edit_rate)
                    gaps = measure_gop(str(mxf_path), cancel=cancel)
                    if gaps and set(gaps) != {want}:
                        prob(f"GOP-Abstaende {sorted(set(gaps))} statt {want}")
                    elif gaps:
                        gop_ok.append(want)
            except tools.Cancelled:
                raise
            except Exception as e:  # noqa: BLE001
                prob(f"ffprobe-Pruefung: {e}")
            if src_root is None:
                # ohne Quelle: Plausibilitaet gegen das Sony-Muster
                for m in check_sony_pattern(info):
                    prob(f"Kopfdaten: {m}")
            if not deep:
                try:
                    for m in tail_decode_check(str(mxf_path), info.duration, info.edit_rate, cancel=cancel):
                        prob(m)
                except tools.Cancelled:
                    raise
                except Exception as e:  # noqa: BLE001
                    prob(f"Decode-Test am Clip-Ende: {e}")
            if deep:
                log(f"  Tiefenpruefung (kompletter Decode): {rel}")
                n, errs = deep_decode_check(str(mxf_path), cancel=cancel)
                if errs:
                    prob(f"Decodierfehler: {errs[:3]}")
                if n != info.duration:
                    prob(f"Decode ergab {n} Frames statt {info.duration}")

        # Vergleich mit Quelle
        if src_root is not None:
            src_mxf = src_root / "Clip" / mxf_path.name
            if not src_mxf.is_file():
                prob("Quell-Clip nicht gefunden")
            else:
                try:
                    si = mxfinfo.read_mxf_info(str(src_mxf), cancel=cancel)
                except tools.Cancelled:
                    raise
                except Exception as e:  # noqa: BLE001
                    prob(f"Quell-MXF nicht lesbar: {e}")
                    si = None
                if si is not None:
                    exp_frames = min(test_frames, si.duration) if test_frames else si.duration
                    if info.duration != exp_frames:
                        prob(f"Dauer {info.duration} != Quelle {exp_frames}")
                    if info.mp_uid != si.mp_uid:
                        prob(f"Material-Package-UMID {info.mp_uid} != Quelle {si.mp_uid}")
                    if info.fp_uid != si.fp_uid:
                        prob(f"File-Package-UMID {info.fp_uid} != Quelle {si.fp_uid}")
                    if info.start_tc != si.start_tc:
                        prob(f"Start-TC {info.start_tc} != Quelle {si.start_tc}")
                    if info.edit_rate != si.edit_rate:
                        prob(f"Bildrate {info.edit_rate} != Quelle {si.edit_rate}")
                    if len(info.audio) != len(si.audio):
                        prob(f"Audiospuren {len(info.audio)} != Quelle {len(si.audio)}")
                    if bool(info.anc_tracks) != bool(si.anc_tracks):
                        why = anc_dropped.get(rel.casefold()) if si.anc_tracks and not info.anc_tracks else None
                        if why:
                            rep.notes.append(f"{rel}: ANC-Spur fehlt im Ziel, Quelle hat eine - {why}")
                        else:
                            extra = " (gewollt bei --no-anc)" if si.anc_tracks and not info.anc_tracks else ""
                            prob(f"ANC-Spur: Ziel {'ja' if info.anc_tracks else 'nein'}, Quelle {'ja' if si.anc_tracks else 'nein'}{extra}"
                                 + (" - in der Einstellungsdatei steht nicht, dass ANC bewusst weggelassen wurde"
                                    if extra else ""))
                    elif si.anc_tracks and len(info.anc_tracks[0].anc_elements) != len(si.anc_tracks[0].anc_elements):
                        prob(f"ANC-Manifest {len(info.anc_tracks[0].anc_elements)} Elemente statt {len(si.anc_tracks[0].anc_elements)} (Quelle)")
                    if bool(info.text_objects) != bool(si.text_objects):
                        rep.notes.append(
                            f"{rel}: eingebettetes XML: Ziel {'ja' if info.text_objects else 'nein'}, Quelle {'ja' if si.text_objects else 'nein'}"
                        )
                    if info.ident_modified != si.ident_modified:
                        rep.notes.append(f"{rel}: Erzeugungsdatum Ziel {info.ident_modified} / Quelle {si.ident_modified}")
                    if is_l:
                        hint_notes: list = []
                        for m in check_header(mxf_path, src_mxf, cancel=cancel, info=info, src_info=si,
                                              notes=hint_notes):
                            prob(f"Kopfdaten: {m}")
                        rep.notes += [f"{rel}: {h}" for h in hint_notes]
                        for m in check_audio_content(mxf_path, src_mxf, cancel=cancel, fps=info.edit_rate,
                                                     duration=si.duration):
                            prob(m)
        if len(rep.problems) == before:
            rep.clips_ok += 1

    if gop_ok:
        rep.notes.append(f"GOP-Laenge N={sorted(set(gop_ok))} bei {len(gop_ok)} Clips (Keyframe-Abstaende der ersten 400 Pakete geprueft)")
    # restliche Dateien gegen Quelle
    if src_root is not None:
        _compare_files(src_root, tgt, mp, src_mp, rep, selection=_selection_record(tgt.parent),
                       has_settings=(tgt.parent / SETTINGS_NAME).is_file())
    return rep


def _xavcl_codec_of_xml(data: bytes) -> str:
    """videoCodec einer Ziel-M01.XML, wenn sie XAVC-L nennt (Clip wurde umgewandelt, steht aber
    evtl. nicht in MEDIAPRO.XML); sonst ''."""
    try:
        c = card.parse_clip_xml(data).video_codec
    except Exception:  # noqa: BLE001
        return ""
    return c if card.is_xavcl_codec(c) else ""


def _compare_files(src: Path, tgt: Path, tgt_mp: "card.MediaPro", src_mp: "card.MediaPro", rep: VerifyReport,
                   selection: Optional[dict] = None, has_settings: bool = False) -> None:
    import os

    converted_xavcl = {m.rel_path.casefold(): m.video_type for m in tgt_mp.materials if card.is_xavcl_codec(m.video_type)}
    n_files = 0
    missing: list[str] = []  # fehlt im Ziel, ohne dass der zugehoerige Clip bewusst fehlt
    skipped: list[str] = []  # fehlt im Ziel, gehoert zu einem im Ziel fehlenden Clip (--only / --unlisted skip)
    # Clips, deren MXF im Ziel nicht vorhanden ist: ihre Begleitdateien in Clip/ und Thmbnl/
    # werden vom Konverter bewusst nicht kopiert (card.copy_structure)
    tgt_clip = tgt / "Clip"
    tgt_mxf = {p.name.casefold() for p in tgt_clip.iterdir()} if tgt_clip.is_dir() else set()
    src_clip = src / "Clip"
    absent_clips: list[Path] = []
    if src_clip.is_dir():
        absent_clips = [p for p in sorted(src_clip.iterdir())
                        if p.is_file() and card.is_clip_mxf_name(p.name) and p.name.casefold() not in tgt_mxf]
    absent_pats = [card.related_pattern(p.stem) for p in absent_clips]
    for root, _d, files in os.walk(src):
        rel_root = Path(root).relative_to(src)
        for fn in files:
            rel = (rel_root / fn).as_posix()
            if rel.upper().startswith("CLIP/") and card.is_clip_mxf_name(fn):
                continue  # Clip-MXF werden einzeln geprueft ('._*.MXF' o. ae. sind Begleitdateien und werden verglichen)
            t = tgt / rel
            n_files += 1
            if not t.is_file():
                top = rel_root.parts[0].casefold() if rel_root.parts else ""
                if top in ("clip", "thmbnl") and any(pt.match(fn) for pt in absent_pats):
                    skipped.append(rel)
                else:
                    missing.append(rel)
                    rep.problems.append(f"{rel}: Datei der Quelle fehlt im Ziel")
                continue
            sb, tb = (Path(root) / fn).read_bytes(), t.read_bytes()
            if sb == tb:
                continue
            if rel.upper() == "MEDIAPRO.XML":
                exp, _ = card.patch_mediapro(sb, converted_xavcl)
                if exp != tb:
                    rep.problems.append("MEDIAPRO.XML: weicht ausser bei videoType von der Quelle ab")
            elif re.search(r"M\d\d\.XML$", fn, re.I) and rel_root.as_posix().casefold() == "clip":
                stem_m = re.match(r"(.*?)M\d\d\.XML$", fn, re.I)
                key = f"clip/{stem_m.group(1)}.mxf".casefold() if stem_m else ""
                new_codec = converted_xavcl.get(key) or _xavcl_codec_of_xml(tb)
                if new_codec:
                    exp, _ = card.patch_clip_xml(sb, new_codec)
                    if exp != tb:
                        rep.problems.append(f"{rel}: weicht ausser bei videoCodec von der Quelle ab")
                else:
                    rep.problems.append(f"{rel}: Datei unterscheidet sich von der Quelle")
            else:
                rep.problems.append(f"{rel}: Datei unterscheidet sich von der Quelle")
    if absent_clips:
        # In MEDIAPRO.XML gelistete, im Ziel fehlende Clips meldet verify_card bereits einzeln als Abweichung.  Nicht
        # gelistete fehlende Clips sind eine Abweichung, ausser die Einstellungsdatei belegt, dass sie bewusst nicht
        # ausgewaehlt wurden (--only bzw. --unlisted skip im letzten Lauf).
        only = (selection or {}).get("only") or []
        skip_unlisted = (selection or {}).get("unlisted") == "skip"
        deliberate: list[str] = []
        for p in absent_clips:
            rel = f"Clip/{p.name}"
            if src_mp.find(rel) is not None or tgt_mp.find(rel) is not None:
                continue
            if only and not any(t.casefold() in p.stem.casefold() for t in only):
                deliberate.append(rel)
            elif skip_unlisted:
                deliberate.append(rel)
            elif selection is not None:
                rep.problems.append(f"{rel}: Quell-Clip (nicht in MEDIAPRO.XML gelistet) fehlt im Ziel - laut "
                                    f"Einstellungsdatei nicht bewusst ausgelassen")
            elif has_settings:
                rep.problems.append(f"{rel}: Quell-Clip (nicht in MEDIAPRO.XML gelistet) fehlt im Ziel - die "
                                    f"Einstellungsdatei enthaelt keine Angabe zur Auswahl (bewusst uebersprungen mit "
                                    f"--only/--unlisted skip?)")
            else:
                rep.problems.append(f"{rel}: Quell-Clip (nicht in MEDIAPRO.XML gelistet) fehlt im Ziel "
                                    f"(bewusst uebersprungen mit --only/--unlisted skip?)")
        shown = ", ".join(skipped[:3]) + (" ..." if len(skipped) > 3 else "")
        if deliberate:
            rep.notes.append(f"{len(deliberate)} Quell-Clip(s) fehlen im Ziel, laut Einstellungsdatei bewusst nicht ausgewaehlt "
                             f"(--only / --unlisted skip): {', '.join(deliberate[:3])}" + (" ..." if len(deliberate) > 3 else ""))
        if skipped:
            rep.notes.append(f"{len(skipped)} zu fehlenden Clips gehoerende Begleitdatei(en) fehlen ebenfalls: {shown}")
    rep.notes.append(f"{n_files - len(missing) - len(skipped)} Nicht-MXF-Dateien der Quelle verglichen")
