"""Pruefungen: einzelner Ziel-Clip (auch fuer Fortsetzen) und komplette Karte (--verify)."""

from __future__ import annotations

import re
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
    if source is not None:
        problems += check_header(path, source, cancel=cancel, info=info, src_info=source_info)
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
) -> list[str]:
    """Pruefung der Sony-Kopfdaten eines Ziel-Clips gegen die Quell-MXF.  Liefert nur Probleme
    der Kopfdaten (getrennt von den uebrigen Pruefungen, damit "nur Kopfdaten nachziehen" moeglich ist)."""
    out: list[str] = []
    try:
        out += [m for m in mxfheader.compare_with_source(path, source)]
    except tools.Cancelled:
        raise
    except (mxfheader.HeaderPatchError, mxfheader.MxfParseError, OSError) as e:
        out.append(f"Kopfdaten nicht pruefbar: {e}")
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
            prob(f"MXF nicht lesbar: {e}")
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
            # ffprobe
            try:
                pr = mxfinfo.run_ffprobe(str(mxf_path), cancel=cancel)
                pv = pr.video or {}
                if pv.get("codec_name") != "h264" or pv.get("profile") != "High 4:2:2":
                    prob(f"H.264-Profil {pv.get('codec_name')}/{pv.get('profile')} statt h264/High 4:2:2")
                if pv.get("pix_fmt") != "yuv422p10le":
                    prob(f"pix_fmt {pv.get('pix_fmt')} statt yuv422p10le")
                if (pv.get("width"), pv.get("height")) != ("1920", "1080"):
                    prob(f"Aufloesung {pv.get('width')}x{pv.get('height')}")
                for k in ("color_space", "color_transfer", "color_primaries"):
                    if pv.get(k) != "bt709":
                        prob(f"{k} {pv.get(k)} statt bt709")
                if pv.get("color_range") != "tv":
                    prob(f"color_range {pv.get('color_range')} statt tv")
                exp_level = card.level_from_codec(mat.video_type) if mat else None
                if exp_level and pv.get("level") != exp_level.replace(".", ""):
                    prob(f"H.264-Level {pv.get('level')} statt {exp_level.replace('.', '')}")
                if len(pr.audio) != len(info.audio):
                    prob(f"ffprobe sieht {len(pr.audio)} Audiospuren statt {len(info.audio)}")
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
                        prob(f"ANC-Spur: Ziel {'ja' if info.anc_tracks else 'nein'}, Quelle {'ja' if si.anc_tracks else 'nein'}")
                    elif si.anc_tracks and len(info.anc_tracks[0].anc_elements) != len(si.anc_tracks[0].anc_elements):
                        rep.notes.append(
                            f"{rel}: ANC-Manifest {len(info.anc_tracks[0].anc_elements)} Elemente (Quelle {len(si.anc_tracks[0].anc_elements)})"
                        )
                    if bool(info.text_objects) != bool(si.text_objects):
                        rep.notes.append(
                            f"{rel}: eingebettetes XML: Ziel {'ja' if info.text_objects else 'nein'}, Quelle {'ja' if si.text_objects else 'nein'}"
                        )
                    if info.ident_modified != si.ident_modified:
                        rep.notes.append(f"{rel}: Erzeugungsdatum Ziel {info.ident_modified} / Quelle {si.ident_modified}")
                    if is_l:
                        for m in check_header(mxf_path, src_mxf, cancel=cancel, info=info, src_info=si):
                            prob(f"Kopfdaten: {m}")
        if len(rep.problems) == before:
            rep.clips_ok += 1

    if gop_ok:
        rep.notes.append(f"GOP-Laenge N={sorted(set(gop_ok))} bei {len(gop_ok)} Clips (Keyframe-Abstaende der ersten 400 Pakete geprueft)")
    # restliche Dateien gegen Quelle
    if src_root is not None:
        _compare_files(src_root, tgt, mp, src_mp, rep)
    return rep


def _xavcl_codec_of_xml(data: bytes) -> str:
    """videoCodec einer Ziel-M01.XML, wenn sie XAVC-L nennt (Clip wurde umgewandelt, steht aber
    evtl. nicht in MEDIAPRO.XML); sonst ''."""
    try:
        c = card.parse_clip_xml(data).video_codec
    except Exception:  # noqa: BLE001
        return ""
    return c if card.is_xavcl_codec(c) else ""


def _compare_files(src: Path, tgt: Path, tgt_mp: "card.MediaPro", src_mp: "card.MediaPro", rep: VerifyReport) -> None:
    import os

    converted_xavcl = {m.rel_path.casefold(): m.video_type for m in tgt_mp.materials if card.is_xavcl_codec(m.video_type)}
    n_files = 0
    missing: list[str] = []
    for root, _d, files in os.walk(src):
        rel_root = Path(root).relative_to(src)
        for fn in files:
            rel = (rel_root / fn).as_posix()
            if rel.upper().startswith("CLIP/") and fn.upper().endswith(".MXF"):
                continue
            t = tgt / rel
            n_files += 1
            if not t.is_file():
                missing.append(rel)
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
    if missing:
        shown = ", ".join(missing[:3]) + (" ..." if len(missing) > 3 else "")
        rep.notes.append(f"{len(missing)} Nicht-MXF-Datei(en) der Quelle fehlen im Ziel (Clips bewusst uebersprungen?): {shown}")
    rep.notes.append(f"{n_files - len(missing)} Nicht-MXF-Dateien der Quelle verglichen")
