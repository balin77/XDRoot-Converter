"""Ablaufsteuerung: Karte pruefen, Struktur kopieren, Clips umwandeln (ffmpeg + raw2bmx), XML anpassen."""

from __future__ import annotations

import datetime as _dt
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Optional

from . import card, mxfinfo, tools, verify

TMP_NAME = "_tmp_xdroot_converter"
LOG_NAME = "konvertierung.log"

# Status je Clip
ST_CONVERTED = "umgewandelt"
ST_COPIED = "unveraendert kopiert"
ST_SKIPPED_DONE = "uebersprungen (bereits fertig)"
ST_NOT_SELECTED = "nicht ausgewaehlt"
ST_FAILED = "FEHLER"
ST_CANCELLED = "abgebrochen"

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


class Reporter:
    """Standard-Berichterstatter (tut nichts); GUI/CLI leiten ab."""

    def log(self, level: str, msg: str) -> None:  # level: info / warn / error
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

    def count(self, status: str) -> int:
        return sum(1 for r in self.results if r.status == status)

    @property
    def failed(self) -> int:
        return self.count(ST_FAILED)

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

    @property
    def convert(self) -> bool:
        return bool(self.selected and self.decision and self.decision.convert and self.new_codec)

    @property
    def name(self) -> str:
        return self.mxf.name


class Converter:
    def __init__(self, opts: Options, reporter: Optional[Reporter] = None, cancel: Optional[tools.CancelToken] = None):
        self.o = opts
        self.rep = reporter or Reporter()
        self.cancel = cancel or tools.CancelToken()
        self._logf = None
        self._t0 = time.time()
        self._frames_total = 0
        self._frames_done = 0

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

    def warn(self, m: str) -> None:
        self._log("warn", m)

    def error(self, m: str) -> None:
        self._log("error", m)

    # ------------------------------------------------------------------ Analyse
    def scan(self, src_root: Path, mp: card.MediaPro, mxfs: list[Path]) -> list[ClipPlan]:
        plans: list[ClipPlan] = []
        for i, mxf in enumerate(mxfs):
            self.cancel.check()
            plan = ClipPlan(mxf=mxf, stem=mxf.stem, material=mp.find(f"Clip/{mxf.name}"))
            if self.o.only and not any(t.casefold() in mxf.stem.casefold() for t in self.o.only):
                plan.selected = False
                plan.skip_reason = "Filter --only"
            elif plan.material is None and self.o.unlisted == "skip":
                plan.selected = False
                plan.skip_reason = "nicht in MEDIAPRO.XML gelistet (uebersprungen)"
            plans.append(plan)
            if not plan.selected:
                continue
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
            # XML des Clips
            for p in sorted((src_root / "Clip").iterdir()):
                if re.match(re.escape(mxf.stem) + r"M\d\d\.XML$", p.name, re.I):
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
            if xml_codec and mp_codec and xml_codec != mp_codec:
                plan.notes.append(f"videoCodec in M01.XML ({xml_codec}) und videoType in MEDIAPRO.XML ({mp_codec}) unterscheiden sich")
            if plan.decision.convert:
                plan.new_codec = card.derive_xavcl_codec(plan.src_codec) or ""
                if not plan.new_codec:
                    plan.decision = mxfinfo.SourceDecision(
                        False, f"unbekanntes Codec-Muster {plan.src_codec!r} - wird nicht geraten, Clip bleibt unveraendert"
                    )
                else:
                    if plan.material and plan.material.dur is not None and plan.material.dur != plan.info.duration:
                        plan.notes.append(f"MEDIAPRO dur {plan.material.dur} != MXF-Dauer {plan.info.duration}")
                    frames = plan.info.duration or 0
                    plan.frames = min(frames, self.o.max_frames) if self.o.max_frames else frames
        return plans

    # ------------------------------------------------------------------ Hauptablauf
    def run(self) -> RunSummary:
        summary = RunSummary()
        t0 = time.time()
        out_root: Optional[Path] = None
        tmp_root: Optional[Path] = None
        src_root: Optional[Path] = None
        mp_bytes: Optional[bytes] = None
        mediapro_updates: dict[str, str] = {}
        try:
            tools.check_tools()
            src_root = card.find_xdroot(self.o.source)
            target_dir = Path(self.o.target)
            out_root = card.check_target(src_root, target_dir)
            target_dir.mkdir(parents=True, exist_ok=True)
            summary.out_root = out_root
            summary.log_path = target_dir / LOG_NAME
            self._logf = open(summary.log_path, "a", encoding="utf-8")
            self.info(f"Start: Quelle {src_root} -> Ziel {out_root}")
            if self.o.max_frames:
                self.warn(f"TESTLAUF: nur die ersten {self.o.max_frames} Frames je Clip; dur/Duration werden NICHT angepasst")
            self.info(f"Einstellungen: Bitrate {self.o.bitrate_mbps} Mbit/s, Preset {self.o.preset}")
            if self.o.bitrate_mbps != 50:
                self.warn("Bitrate ungleich 50 Mbit/s: Die Kennung 'AVC50' in den XML-Dateien entspricht dann nicht der Bitrate.")

            mp_bytes = (src_root / "MEDIAPRO.XML").read_bytes()
            mp = card.parse_mediapro(mp_bytes)
            mxfs = card.list_clip_mxf(src_root)
            self.info(f"{len(mxfs)} Clips gefunden ({mp.system_kind or 'Kamera unbekannt'}), {len(mp.materials)} davon in MEDIAPRO.XML")

            self.info("Analysiere Clips ...")
            plans = self.scan(src_root, mp, mxfs)
            # `scan` hat bereits Plaene angelegt; Reihenfolge wie Dateiliste
            todo = [p for p in plans if p.convert]
            self._frames_total = sum(p.frames for p in todo) or 1
            for p in plans:
                if p.selected and not p.convert and p.decision is not None:
                    self.warn(f"{p.name}: wird nicht umgewandelt ({p.decision.reason}) - Datei wird unveraendert kopiert")
                for n in p.notes:
                    self.warn(f"{p.name}: {n}")
            self.info(f"Umzuwandeln: {len(todo)} Clips, {self._frames_total} Frames")

            self._check_disk_space(target_dir, plans)

            out_root.mkdir(parents=True, exist_ok=True)
            tmp_root = target_dir / TMP_NAME
            tmp_root.mkdir(parents=True, exist_ok=True)

            skip_stems = {p.stem.casefold() for p in plans if not p.selected}
            self.info("Kopiere Nicht-MXF-Dateien ...")
            n = card.copy_structure(src_root, out_root, skip_stems=skip_stems, cancel_check=self.cancel.check)
            self.info(f"{n} Dateien kopiert")
            if self.o.max_frames:
                (target_dir / verify.TEST_MARKER).write_text(
                    f"TESTAUSGABE - nur die ersten Frames je Clip, dur/Duration nicht angepasst\nmax_frames={self.o.max_frames}\n",
                    encoding="utf-8",
                )

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
                try:
                    self._process_clip(plan, src_root, out_root, tmp_root, res)
                except tools.Cancelled:
                    res.status = ST_CANCELLED
                    summary.results.append(res)
                    raise
                except Exception as e:  # noqa: BLE001
                    res.status = ST_FAILED
                    res.notes.append(str(e))
                    self.error(f"{plan.name}: {e}")
                res.seconds = time.time() - t_clip
                if res.status in (ST_CONVERTED, ST_SKIPPED_DONE) and plan.new_codec:
                    mediapro_updates[f"Clip/{plan.mxf.name}"] = plan.new_codec
                dst = out_root / "Clip" / plan.mxf.name
                res.dst_size = dst.stat().st_size if dst.exists() else 0
                summary.results.append(res)
                self.rep.clip_done(res)
                self._shutil_clean(tmp_root / plan.stem)
        except tools.Cancelled:
            summary.cancelled = True
            self.warn("Abgebrochen. Bereits fertige Clips bleiben erhalten; ein erneuter Lauf setzt fort.")
        except Exception as e:  # noqa: BLE001
            summary.fatal = str(e)
            self.error(f"Abbruch wegen Fehler: {e}")
        finally:
            # MEDIAPRO.XML immer passend zum tatsaechlichen Zustand schreiben
            try:
                if out_root is not None and out_root.exists() and src_root is not None and mp_bytes is not None:
                    new_mp, changed = card.patch_mediapro(mp_bytes, mediapro_updates)
                    (out_root / "MEDIAPRO.XML").write_bytes(new_mp)
                    try:
                        shutil.copystat(src_root / "MEDIAPRO.XML", out_root / "MEDIAPRO.XML")
                    except OSError:
                        pass
                    self.info(f"MEDIAPRO.XML angepasst ({len(changed)} videoType-Eintraege)")
            except Exception as e:  # noqa: BLE001
                self.error(f"MEDIAPRO.XML konnte nicht geschrieben werden: {e}")
            if tmp_root is not None:
                self._shutil_clean(tmp_root)
                try:
                    tmp_root.rmdir()
                except OSError:
                    pass
            summary.seconds = time.time() - t0
            self.info(summary.text())
            if self._logf is not None:
                try:
                    self._logf.close()
                except Exception:
                    pass
                self._logf = None
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

    def _check_disk_space(self, target_dir: Path, plans: list[ClipPlan]) -> None:
        need = 0
        biggest = 0
        for p in plans:
            if not p.selected:
                continue
            if p.convert:
                est = estimate_clip_bytes(p.frames, p.info.edit_rate, len(p.info.audio), self.o.bitrate_mbps)
                need += est
                biggest = max(biggest, est)
            else:
                need += p.mxf.stat().st_size
                biggest = max(biggest, p.mxf.stat().st_size)
        need = int((need + biggest) * 1.05) + 200 * 1024 * 1024
        free = shutil.disk_usage(target_dir).free
        self.info(f"Speicherbedarf geschaetzt {fmt_size(need)}, frei {fmt_size(free)}")
        if free < need:
            raise RuntimeError(
                f"Zu wenig freier Speicher im Zielordner: benoetigt ca. {fmt_size(need)}, frei {fmt_size(free)}."
            )

    def _emit_progress(self, plan: ClipPlan, clip_frac: float, text: str = "") -> None:
        clip_frac = max(0.0, min(1.0, clip_frac))
        done = self._frames_done + clip_frac * plan.frames
        total = done / self._frames_total if self._frames_total else 0.0
        elapsed = time.time() - self._t0
        eta = elapsed * (1 - total) / total if total > 0.01 and elapsed > 5 else None
        self.rep.progress(plan.name, clip_frac, min(1.0, total), eta, text)

    # ------------------------------------------------------------------ Ein Clip
    def _process_clip(self, plan: ClipPlan, src_root: Path, out_root: Path, tmp_root: Path, res: ClipResult) -> None:
        dst_dir = out_root / "Clip"
        dst = dst_dir / plan.mxf.name
        res.notes.extend(plan.notes)

        if not plan.convert:
            self.info(f"{plan.name}: kopiere unveraendert ({plan.decision.reason if plan.decision else 'nicht analysierbar'})")
            tmpd = tmp_root / plan.stem
            tmpd.mkdir(parents=True, exist_ok=True)
            part = tmpd / "copy.mxf"
            self._copy_cancelable(plan.mxf, part)
            shutil.copystat(plan.mxf, part)
            os.replace(part, dst)
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
        exp = verify.ClipExpect(
            frames=frames, mp_uid=info.mp_uid, fp_uid=info.fp_uid, start_tc=info.start_tc,
            n_audio=len(info.audio), fps=fps, expect_anc=expect_anc, expect_xml=embed,
        )

        # Fortsetzen: fertiger, verifizierter Ziel-Clip vorhanden?
        if dst.exists():
            probs = verify.check_clip(dst, exp, cancel=self.cancel)
            if not probs:
                self.info(f"{plan.name}: bereits fertig und verifiziert - uebersprungen")
                self._patch_clip_xml(plan, dst_dir)
                res.status = ST_SKIPPED_DONE
                self._emit_progress(plan, 1.0, "bereits fertig")
                self._frames_done += frames
                return
            self.warn(f"{plan.name}: vorhandene Zieldatei unvollstaendig/abweichend ({probs[0]}) - wird neu erzeugt")
            dst.unlink()

        tmpd = tmp_root / plan.stem
        self._shutil_clean(tmpd)
        tmpd.mkdir(parents=True, exist_ok=True)
        self.info(f"{plan.name}: {frames} Frames, {float(fps):.3f} fps, {len(info.audio)} Audiospuren, GOP N={mxfinfo.gop_length(fps)}")

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
            self._emit_progress(plan, 0.92, "Verpacke MXF ...")
            if out_mxf.exists():
                out_mxf.unlink()
            err = self._wrap(plan, fps, frames, v_path, wavs, out_mxf, anc_f, xml_f)
            if err:
                last_err = err
                self.warn(f"{plan.name}: raw2bmx fehlgeschlagen ({err.splitlines()[-1] if err else '?'}) - Versuch {idx + 1}/{len(attempts)}")
                continue
            exp.expect_anc = anc_f is not None
            exp.expect_xml = xml_f is not None
            probs = verify.check_clip(out_mxf, exp, cancel=self.cancel)
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

        # 5. Atomar ins Ziel, XML anpassen
        for p in [v_path, *wavs]:
            try:
                p.unlink()
            except OSError:
                pass
        os.replace(out_mxf, dst)
        self._patch_clip_xml(plan, dst_dir)
        res.status = ST_CONVERTED
        self._emit_progress(plan, 1.0, "fertig")
        self._frames_done += frames
        self.info(f"{plan.name}: fertig ({fmt_size(plan.mxf.stat().st_size)} -> {fmt_size(dst.stat().st_size)})")

    def _want_embed(self, info: mxfinfo.MxfInfo, plan: ClipPlan) -> bool:
        if plan.xml_path is None or self.o.embed_xml == "never":
            return False
        if self.o.embed_xml == "always":
            return True
        return any(t.get("scheme_id") == verify.NRT_SCHEME_ID for t in info.text_objects)

    def _copy_cancelable(self, src: Path, dst: Path) -> None:
        with open(src, "rb") as fi, open(dst, "wb") as fo:
            while True:
                self.cancel.check()
                buf = fi.read(16 * 1024 * 1024)
                if not buf:
                    break
                fo.write(buf)

    def _patch_clip_xml(self, plan: ClipPlan, dst_dir: Path) -> None:
        if plan.xml_path is None:
            return
        data = plan.xml_path.read_bytes()
        new, changed = card.patch_clip_xml(data, plan.new_codec)
        d = dst_dir / plan.xml_path.name
        d.write_bytes(new)
        try:
            shutil.copystat(plan.xml_path, d)
        except OSError:
            pass
        if not changed:
            self.warn(f"{plan.xml_path.name}: videoCodec war nicht zu aendern (bereits {plan.new_codec}?)")

    def _prepare_embed_xml(self, plan: ClipPlan, tmpd: Path) -> Optional[Path]:
        try:
            data = plan.xml_path.read_bytes()  # type: ignore[union-attr]
            new, _ = card.patch_clip_xml(data, plan.new_codec)
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
        level = card.level_from_codec(plan.new_codec) or "4.2"
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
                self._emit_progress(plan, 0.9 * last[0] / max(1, frames), f"Transkodiere {last[0]}/{frames}")

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
        self._emit_progress(plan, 0.9, "Extrahiere ANC-Daten ...")
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
