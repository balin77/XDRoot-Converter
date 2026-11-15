"""tkinter-Oberfläche (Deutsch).

Die reine Logik (Bitraten-Prüfung, Übersichts-Schlüssel, Abschlussmeldung ...) steht
als tkinter-freie Funktionen am Anfang der Datei und ist ohne GUI testbar.
"""

from __future__ import annotations

import datetime
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Optional

try:  # tkinter kann fehlen (Standard-Python ohne tcl/tk) - Logik bleibt importierbar
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk
except ImportError:  # pragma: no cover
    tk = None  # type: ignore[assignment]
    filedialog = messagebox = ttk = None  # type: ignore[assignment]

from . import __version__, card, converter, tools

PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]

BITRATE_MIN = 10.0
BITRATE_MAX = 100.0
LOG_MAX_LINES = 5000
CLOSE_WAIT_S = 30.0
MAX_LISTED_FAILED = 10
MAX_INCOMPLETE_CHARS = 400
ANALYSE_TEXT = "Analysiere Quelle …"
HINT_FOLDERS = ("Die Quelle wird nur gelesen und nie verändert. Als Ziel einen leeren Ordner wählen; "
                "darin entsteht der Ordner XDROOT.")
HINT_BITRATE = "Kennung in den Metadaten bleibt AVC50"
HIDDEN_LEVELS = ("detail",)


# =========================================================================== reine Logik

def parse_bitrate(text: str) -> float:
    """Bitrate in Mbit/s aus dem Eingabefeld; ValueError mit deutscher Meldung bei ungültiger Eingabe."""
    raw = (text or "").strip().replace(",", ".")
    try:
        v = float(raw)
    except ValueError:
        raise ValueError(
            f"Die Bitrate „{(text or '').strip()}“ ist keine Zahl. "
            f"Bitte einen Wert zwischen {BITRATE_MIN:.0f} und {BITRATE_MAX:.0f} Mbit/s eingeben."
        ) from None
    if v != v or not (BITRATE_MIN <= v <= BITRATE_MAX):
        raise ValueError(
            f"Die Bitrate {text.strip()} Mbit/s liegt außerhalb des erlaubten Bereichs "
            f"({BITRATE_MIN:.0f} bis {BITRATE_MAX:.0f} Mbit/s)."
        )
    return v


def overview_key(src: str, skip_unlisted: bool, bitrate_text: str) -> tuple:
    """Schlüssel, unter dem eine Übersichts-Analyse gilt (Quelle + Haken + Bitrate)."""
    try:
        br: object = parse_bitrate(bitrate_text)
    except ValueError:
        br = None
    return ((src or "").strip(), bool(skip_unlisted), br)


def overview_display_text(text: str, skip_unlisted: bool) -> str:
    """Übersichtstext; weist auf den Haken hin, wenn nicht gelistete Clips gemeldet werden."""
    if not skip_unlisted and "MEDIAPRO.XML" in text and "nicht in MEDIAPRO.XML" in text:
        text += "\nTipp: Unter „Einstellungen“ lassen sich nicht gelistete Clips überspringen."
    return text


def target_has_xdroot(dst: str) -> bool:
    try:
        return bool(dst) and (Path(dst) / "XDROOT").exists()
    except OSError:
        return False


def target_xdroot_has_files(dst: str) -> bool:
    """Liegen im Ziel-XDROOT (dst/XDROOT) tatsächlich Dateien oder Ordner?"""
    try:
        d = Path(dst) / "XDROOT"
        if not d.is_dir():
            return False
        with os.scandir(d) as it:
            return any(True for _ in it)
    except OSError:
        return False


def target_is_xdroot(dst: str) -> bool:
    """Ist der gewählte Zielordner selbst ein XDROOT (MEDIAPRO.XML oder Name XDROOT mit Clip/)?"""
    try:
        p = Path(dst)
        if not p.is_dir():
            return False
        if (p / "MEDIAPRO.XML").is_file():
            return True
        return p.name.upper() == "XDROOT" and (p / "Clip").is_dir()
    except OSError:
        return False


_TRANSCODE_PREFIXES = ("Starte Transkodierung", "Transkodiere")


def progress_texts(name: str, text: str, total_frac: float, eta_s: Optional[float]) -> tuple[str, str]:
    """(Zeile über dem Clip-Balken, Zeile über dem Gesamt-Balken)."""
    clip = f"{name}  {text}".strip() if (name or text) else ""
    if eta_s is None:
        # Restzeit nur schätzbar, sobald transkodiert wird (nicht in Analyse-/Prüf-/Kopierphase)
        rest = ", Restzeit wird berechnet …" if (text or "").startswith(_TRANSCODE_PREFIXES) else ""
    elif eta_s <= 0:
        rest = ""
    else:
        rest = f", Restzeit ca. {converter.fmt_duration(eta_s)}"
    return clip, f"Gesamt {total_frac * 100:.1f} %{rest}"


def _fatal_hint(fatal: str) -> str:
    low = fatal.lower()
    if "andere karte" in low or "anderen karte" in low:
        return "Bitte einen leeren Ordner als Ziel wählen."
    if "anderen lauf" in low:
        return ("Bitte warten, bis der andere Lauf beendet ist. Läuft sicher kein anderer Lauf, "
                "die in der Meldung genannte Sperrdatei löschen.")
    return ""


MAX_REASON_CHARS = 100


def _fail_reason(r) -> str:
    """Grund eines fehlgeschlagenen Clips (erste Notiz, gekürzt) als Anhang ' – Grund' oder ''."""
    notes = getattr(r, "notes", None) or []
    if not notes:
        return ""
    txt = " ".join(str(notes[0]).split())
    if not txt:
        return ""
    if len(txt) > MAX_REASON_CHARS:
        txt = txt[:MAX_REASON_CHARS].rstrip() + " …"
    return f" – {txt}"


def completion_message(summary) -> tuple[str, str, str]:
    """Meldungsfenster nach einem Lauf: (art, titel, text); art = info | warning | error."""
    log_path = getattr(summary, "log_path", None)
    log_part = f"Protokolldatei:\n{log_path}" if log_path else ""
    if summary.fatal:
        if summary.results:
            parts = ["Die Umwandlung wurde mit einem fatalen Fehler abgebrochen:\n" + str(summary.fatal)]
        else:
            parts = ["Die Umwandlung konnte nicht durchgeführt werden:\n" + str(summary.fatal)]
        hint = _fatal_hint(str(summary.fatal))
        if hint:
            parts.append(hint)
        if summary.results:
            parts.append(
                f"bis dahin - umgewandelt: {summary.count(converter.ST_CONVERTED)}, "
                f"bereits fertig: {summary.count(converter.ST_SKIPPED_DONE)}, "
                f"kopiert: {summary.count(converter.ST_COPIED)}, "
                f"Fehler: {summary.failed}")
            try:
                inc = summary.incomplete_text()
            except Exception:  # noqa: BLE001
                inc = ""
            if inc:
                if len(inc) > MAX_INCOMPLETE_CHARS:
                    inc = inc[:MAX_INCOMPLETE_CHARS].rstrip() + " … (siehe Protokoll)"
                parts.append(inc)
            parts.append("Die Zielkarte ist unvollständig. Ein erneuter Lauf mit demselben Zielordner setzt fort.")
        if log_part:
            parts.append(log_part)
        return "error", "Umwandlung fehlgeschlagen", "\n\n".join(parts)

    failed = [r for r in summary.results if r.status == converter.ST_FAILED]
    copy_errors = list(getattr(summary, "copy_errors", None) or [])
    counts = (
        f"umgewandelt: {summary.count(converter.ST_CONVERTED)}, "
        f"bereits fertig: {summary.count(converter.ST_SKIPPED_DONE)}, "
        f"kopiert: {summary.count(converter.ST_COPIED)}, "
        f"Fehler: {summary.failed}"
    )
    parts: list[str] = []
    if summary.cancelled:
        kind, title = "warning", "Abgebrochen"
        parts.append("Die Umwandlung wurde abgebrochen. Bereits fertige Clips bleiben erhalten.")
        parts.append("Die Zielkarte ist unvollständig. Ein erneuter Lauf mit demselben Zielordner setzt fort.")
    elif failed or copy_errors:
        kind, title = "warning", "Fertig mit Fehlern"
        bits = []
        if failed:
            bits.append(f"{len(failed)} Clip(s) sind fehlgeschlagen")
        if copy_errors:
            bits.append(f"{len(copy_errors)} Datei(en) konnten nicht kopiert werden")
        parts.append("Die Umwandlung ist beendet, aber " + " und ".join(bits) + ".")
    else:
        kind, title = "info", "Fertig"
        parts.append("Die Umwandlung ist erfolgreich beendet.")
    parts.append(counts)
    if failed:
        lines = ["Fehlgeschlagene Clips:"]
        for r in failed[:MAX_LISTED_FAILED]:
            lines.append(f"  • {r.name}{_fail_reason(r)}")
        if len(failed) > MAX_LISTED_FAILED:
            lines.append(f"  … und {len(failed) - MAX_LISTED_FAILED} weitere (siehe Protokoll)")
        parts.append("\n".join(lines))
    if copy_errors:
        lines = [f"{len(copy_errors)} Datei(en) konnten nicht kopiert werden:"]
        for rel, msg in copy_errors[:MAX_LISTED_FAILED]:
            lines.append(f"  • {rel}: {msg}")
        if len(copy_errors) > MAX_LISTED_FAILED:
            lines.append(f"  … und {len(copy_errors) - MAX_LISTED_FAILED} weitere (siehe Protokoll)")
        parts.append("\n".join(lines))
    inc = ""
    try:
        inc = summary.incomplete_text()
    except Exception:  # noqa: BLE001
        inc = ""
    if inc:
        if len(inc) > MAX_INCOMPLETE_CHARS:
            inc = inc[:MAX_INCOMPLETE_CHARS].rstrip() + " … (siehe Protokoll)"
        parts.append(inc)
    if log_part:
        parts.append(log_part)
    if kind == "info":
        parts.append("Empfehlung: Bitte das Ergebnis prüfen (z. B. einige Clips abspielen), "
                     "bevor die Quellkarte gelöscht oder überschrieben wird.")
    return kind, title, "\n\n".join(parts)


def finish_status(summary) -> str:
    if summary.fatal:
        return "Fehler: " + str(summary.fatal)
    if summary.cancelled:
        return "Abgebrochen."
    if summary.failed:
        return f"Fertig mit {summary.failed} Fehler(n) – siehe Protokoll."
    return "Fertig."


# =========================================================================== GUI

class QueueReporter(converter.Reporter):
    """Schickt alle Meldungen aus dem Arbeitsthread per Queue an die GUI."""

    def __init__(self, q: "queue.Queue") -> None:
        self.q = q

    def log(self, level: str, msg: str) -> None:
        self.q.put(("log", level, msg))

    def progress(self, clip_name, clip_frac, total_frac, eta_s, text="") -> None:
        self.q.put(("progress", clip_name, clip_frac, total_frac, eta_s, text))

    def clip_done(self, result) -> None:
        self.q.put(("clip_done", result))


_Base = tk.Tk if tk is not None else object


class App(_Base):  # type: ignore[misc,valid-type]
    def __init__(self) -> None:
        super().__init__()
        self.title(f"XDROOT-Converter {__version__} – XAVC-I nach XAVC-L")
        try:
            s = max(1.0, float(self.winfo_fpixels("1i")) / 96.0)
        except Exception:  # noqa: BLE001
            s = 1.0
        try:
            sw, sh = int(self.winfo_screenwidth()), int(self.winfo_screenheight())
        except Exception:  # noqa: BLE001
            sw, sh = 10 ** 6, 10 ** 6
        w = max(300, min(int(860 * s), sw - 80))
        h = max(300, min(int(680 * s), sh - 120))
        self.minsize(min(int(720 * s), w), min(int(520 * s), h))
        self.geometry(f"{w}x{h}")
        self.q: "queue.Queue" = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.cancel_token: Optional[tools.CancelToken] = None
        self.ov_thread: Optional[threading.Thread] = None
        self.ov_cancel: Optional[tools.CancelToken] = None
        self.overview_token = 0
        self._last_overview_key: Optional[tuple] = None
        self._after_id: Optional[str] = None
        self._ov_after_id: Optional[str] = None
        self._closing = False          # Fenster wird geschlossen, sobald der Worker fertig ist
        self._close_started = 0.0
        self._close_asked = 0.0
        self._locked_widgets: list = []
        self._ui_running = False       # Knöpfe stehen im Lauf-Zustand
        self._last_total = 0.0

        self.var_src = tk.StringVar()
        self.var_dst = tk.StringVar()
        self.var_bitrate = tk.StringVar(value="50")
        self.var_preset = tk.StringVar(value="medium")
        self.var_skip_unlisted = tk.BooleanVar(value=False)
        self.var_status = tk.StringVar(value="Bereit.")
        self.var_overview = tk.StringVar(value="Bitte Quellordner wählen.")
        self.var_br_hint = tk.StringVar(value="")
        self.var_clip_label = tk.StringVar(value="")
        self.var_total_label = tk.StringVar(value="")
        self.settings_open = tk.BooleanVar(value=False)

        self._build()
        self.var_skip_unlisted.trace_add("write", lambda *_a: self._schedule_overview(0))
        self.var_bitrate.trace_add("write", lambda *_a: self._schedule_overview(700))
        self.var_bitrate.trace_add("write", lambda *_a: self._update_br_hint())
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._after_id = self.after(100, self._poll)

    # ------------------------------------------------------------------ Aufbau
    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}
        root = ttk.Frame(self, padding=8)
        root.pack(fill="both", expand=True)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(8, weight=1)
        lock = self._locked_widgets

        ttk.Label(root, text="Quellordner:").grid(row=0, column=0, sticky="w", **pad)
        e_src = ttk.Entry(root, textvariable=self.var_src)
        e_src.grid(row=0, column=1, sticky="ew", **pad)
        b_src = ttk.Button(root, text="Durchsuchen …", command=self._browse_src)
        b_src.grid(row=0, column=2, **pad)
        e_src.bind("<Return>", lambda _e: self._refresh_overview())
        e_src.bind("<FocusOut>", lambda _e: self._refresh_overview())

        ttk.Label(root, text="Zielordner:").grid(row=1, column=0, sticky="w", **pad)
        e_dst = ttk.Entry(root, textvariable=self.var_dst)
        e_dst.grid(row=1, column=1, sticky="ew", **pad)
        b_dst = ttk.Button(root, text="Durchsuchen …", command=self._browse_dst)
        b_dst.grid(row=1, column=2, **pad)

        self.lbl_hint = ttk.Label(root, text=HINT_FOLDERS, wraplength=700, justify="left", foreground="#555555")
        self.lbl_hint.grid(row=2, column=0, columnspan=3, sticky="ew", padx=8, pady=(0, 4))
        self.lbl_hint.bind("<Configure>", lambda e: self.lbl_hint.configure(wraplength=max(200, e.width - 8)))

        self.lbl_overview = ttk.Label(root, textvariable=self.var_overview, wraplength=700, justify="left")
        self.lbl_overview.grid(row=3, column=0, columnspan=3, sticky="ew", **pad)
        self.lbl_overview.bind("<Configure>", lambda e: self.lbl_overview.configure(wraplength=max(200, e.width - 8)))

        # Einstellungen (einklappbar)
        self.btn_settings = ttk.Button(root, text="Einstellungen anzeigen", command=self._toggle_settings)
        self.btn_settings.grid(row=4, column=0, sticky="w", **pad)
        self.frm_settings = ttk.LabelFrame(root, text="Einstellungen", padding=8)
        ttk.Label(self.frm_settings, text="Bitrate (Mbit/s):").grid(row=0, column=0, sticky="w", padx=4, pady=2)
        sp = ttk.Spinbox(self.frm_settings, from_=10, to=100, increment=5, width=6, textvariable=self.var_bitrate)
        sp.grid(row=0, column=1, sticky="w", padx=4, pady=2)
        ttk.Label(self.frm_settings, text="x264-Preset:").grid(row=0, column=2, sticky="w", padx=(16, 4), pady=2)
        cb = ttk.Combobox(self.frm_settings, values=PRESETS, textvariable=self.var_preset, width=12, state="readonly")
        cb.grid(row=0, column=3, sticky="w", padx=4, pady=2)
        ttk.Label(self.frm_settings, textvariable=self.var_br_hint, foreground="#9a6700").grid(
            row=2, column=0, columnspan=4, sticky="w", padx=4, pady=2)
        ck = ttk.Checkbutton(
            self.frm_settings, text="Clips, die nicht in MEDIAPRO.XML stehen, überspringen",
            variable=self.var_skip_unlisted,
        )
        ck.grid(row=1, column=0, columnspan=4, sticky="w", padx=4, pady=2)
        # (Widget, Zustand im freigegebenen Betrieb)
        lock.extend([(e_src, "normal"), (b_src, "normal"), (e_dst, "normal"), (b_dst, "normal"),
                     (sp, "normal"), (cb, "readonly"), (ck, "normal")])

        bf = ttk.Frame(root)
        bf.grid(row=5, column=0, columnspan=3, sticky="w", **pad)
        self.btn_start = ttk.Button(bf, text="Konvertieren", command=self._start)
        self.btn_start.pack(side="left", padx=(0, 8))
        self.btn_cancel = ttk.Button(bf, text="Abbrechen", command=self._cancel, state="disabled")
        self.btn_cancel.pack(side="left")

        ttk.Label(root, textvariable=self.var_clip_label).grid(row=6, column=0, columnspan=3, sticky="w", padx=8)
        self.pb_clip = ttk.Progressbar(root, maximum=1000)
        self.pb_clip.grid(row=6, column=0, columnspan=3, sticky="ew", padx=8, pady=(22, 2))
        ttk.Label(root, textvariable=self.var_total_label).grid(row=7, column=0, columnspan=3, sticky="w", padx=8)
        self.pb_total = ttk.Progressbar(root, maximum=1000)
        self.pb_total.grid(row=7, column=0, columnspan=3, sticky="ew", padx=8, pady=(22, 6))

        logf = ttk.Frame(root)
        logf.grid(row=8, column=0, columnspan=3, sticky="nsew", padx=8, pady=4)
        logf.rowconfigure(0, weight=1)
        logf.columnconfigure(0, weight=1)
        self.txt = tk.Text(logf, height=12, wrap="word", state="disabled", font=("Consolas", 9))
        sb = ttk.Scrollbar(logf, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.txt.tag_configure("warn", foreground="#9a6700")
        self.txt.tag_configure("error", foreground="#b00020")
        self.txt.tag_configure("sep", foreground="#666666")

        ttk.Label(root, textvariable=self.var_status, relief="sunken", anchor="w").grid(
            row=9, column=0, columnspan=3, sticky="ew", padx=8, pady=(4, 0))

    def _toggle_settings(self) -> None:
        if self.settings_open.get():
            self.frm_settings.grid_remove()
            self.btn_settings.configure(text="Einstellungen anzeigen")
            self.settings_open.set(False)
        else:
            self.frm_settings.grid(row=4, column=1, columnspan=2, sticky="ew", padx=8, pady=4)
            self.btn_settings.configure(text="Einstellungen verbergen")
            self.settings_open.set(True)

    def _update_br_hint(self) -> None:
        try:
            br = parse_bitrate(self.var_bitrate.get())
        except ValueError:
            self.var_br_hint.set("")
            return
        self.var_br_hint.set(HINT_BITRATE if br != 50 else "")

    def _set_inputs(self, enabled: bool) -> None:
        for w, normal in self._locked_widgets:
            try:
                w.configure(state=normal if enabled else "disabled")
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ Ordner / Übersicht
    def _browse_src(self) -> None:
        d = filedialog.askdirectory(title="Quellordner (XDROOT oder Elternordner) wählen")
        if d:
            self.var_src.set(d)
            self._refresh_overview()

    def _browse_dst(self) -> None:
        d = filedialog.askdirectory(title="Zielordner wählen (darin entsteht XDROOT)")
        if d:
            self.var_dst.set(d)

    def _is_running(self) -> bool:
        return bool(self.worker and self.worker.is_alive())

    def _schedule_overview(self, delay_ms: int) -> None:
        if self._ov_after_id:
            try:
                self.after_cancel(self._ov_after_id)
            except Exception:  # noqa: BLE001
                pass
            self._ov_after_id = None
        if self._closing:
            return
        self._ov_after_id = self.after(delay_ms, self._refresh_overview)

    def _cancel_overview(self) -> None:
        if self.ov_cancel is not None:
            self.ov_cancel.cancel()
        self.overview_token += 1  # späte Ergebnisse verwerfen

    def _refresh_overview(self) -> None:
        self._ov_after_id = None
        src = self.var_src.get().strip()
        if not src or self._is_running() or self._closing:
            return
        skip = self.var_skip_unlisted.get()
        key = overview_key(src, skip, self.var_bitrate.get())
        if key == self._last_overview_key:
            return
        try:
            bitrate = parse_bitrate(self.var_bitrate.get())
        except ValueError as e:
            self._last_overview_key = None
            self.var_overview.set(str(e))
            return
        self._cancel_overview()
        self._last_overview_key = key
        token = self.overview_token
        cancel = tools.CancelToken()
        self.ov_cancel = cancel
        self.var_overview.set(ANALYSE_TEXT)

        opts = converter.Options(
            source=src, target="", bitrate_mbps=bitrate, unlisted="skip" if skip else "convert",
        )

        def work() -> None:
            try:
                ov = converter.overview(opts, cancel=cancel)
                self.q.put(("overview", token, overview_display_text(ov.text(), skip), True))
            except Exception as e:  # noqa: BLE001
                if cancel.cancelled:
                    return
                self.q.put(("overview", token, f"Quelle nicht nutzbar: {e}", False))

        self.ov_thread = threading.Thread(target=work, daemon=True)
        self.ov_thread.start()

    # ------------------------------------------------------------------ Lauf
    def _start(self) -> None:
        if self._is_running() or self._closing:
            return
        src, dst = self.var_src.get().strip(), self.var_dst.get().strip()
        if not src or not dst:
            messagebox.showwarning("XDROOT-Converter", "Bitte Quell- und Zielordner angeben.")
            return
        try:
            bitrate = parse_bitrate(self.var_bitrate.get())
        except ValueError as e:
            messagebox.showwarning("XDROOT-Converter", str(e))
            return
        try:
            tools.check_tools()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("XDROOT-Converter", str(e))
            return
        # Ziel ist selbst ein XDROOT? Dann den Elternordner anbieten (ohne Seiteneffekte:
        # das Zielfeld wird erst gesetzt, wenn alle Prüfungen und Rückfragen positiv waren)
        use_parent = target_is_xdroot(dst)
        if use_parent:
            if Path(dst).parent == Path(dst):
                messagebox.showwarning("XDROOT-Converter", "Der gewählte Zielordner ist selbst ein XDROOT. "
                                       "Bitte einen anderen Ordner als Ziel wählen.")
                return
            eff_dst = str(Path(dst).parent)
        else:
            eff_dst = dst
        # Grundprüfungen (Quelle ≠ Ziel) vor jeder Rückfrage, mit dem tatsächlich verwendeten Ziel
        try:
            src_root = card.find_xdroot(src)
        except Exception as e:  # noqa: BLE001
            messagebox.showwarning("XDROOT-Converter", f"Quelle nicht nutzbar: {e}")
            return
        try:
            card.check_target(src_root, Path(eff_dst), chosen_source=Path(src))
        except Exception as e:  # noqa: BLE001
            messagebox.showwarning("XDROOT-Converter", str(e))
            return
        if use_parent:
            if not messagebox.askyesno(
                    "XDROOT-Converter",
                    f"Der gewählte Zielordner ist selbst ein XDROOT. "
                    f"Soll der übergeordnete Ordner „{eff_dst}“ als Ziel verwendet werden?"):
                return
        if target_xdroot_has_files(eff_dst):
            if not messagebox.askyesno(
                    "XDROOT-Converter",
                    "Der Zielordner enthält bereits ein XDROOT (z. B. von einem früheren Lauf). "
                    "Fertige Clips werden übersprungen, fehlende ergänzt. Fortfahren?"):
                return
        dst = eff_dst
        if use_parent:
            self.var_dst.set(dst)
        opts = converter.Options(
            source=src, target=dst, bitrate_mbps=bitrate, preset=self.var_preset.get() or "medium",
            unlisted="skip" if self.var_skip_unlisted.get() else "convert",
        )
        # Übersichts-Analyse beenden; der Worker wartet auf ihr Ende (kein paralleler Scan)
        self._cancel_overview()
        self._last_overview_key = None
        prev_ov = self.ov_thread
        self.cancel_token = tools.CancelToken()
        conv = converter.Converter(opts, QueueReporter(self.q), self.cancel_token)
        self._set_inputs(False)
        self.btn_start.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.pb_clip["value"] = 0
        self.pb_total["value"] = 0
        self.var_clip_label.set("")
        self.var_total_label.set("")
        self.var_status.set("Läuft …")
        self._ui_running = True
        self._last_total = 0.0
        if self.var_overview.get() == ANALYSE_TEXT:
            self.var_overview.set("Umwandlung läuft – die Übersicht wird danach aktualisiert.")
        stamp = datetime.datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        self._append(f"──────── Neuer Lauf {stamp} ────────", "sep")

        def work() -> None:
            try:
                if prev_ov is not None and prev_ov.is_alive():
                    prev_ov.join(timeout=60)
                summary = conv.run()
                self.q.put(("finished", summary))
            except Exception as e:  # noqa: BLE001
                self.q.put(("crashed", f"{type(e).__name__}: {e}"))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _cancel(self) -> None:
        if self.cancel_token is not None and self._is_running():
            self.cancel_token.cancel()
            self.var_status.set("Breche ab … laufende Prozesse werden beendet.")
            self.btn_cancel.configure(state="disabled")

    # ------------------------------------------------------------------ Log
    def _append(self, text: str, tag: str = "") -> None:
        self.txt.configure(state="normal")
        self.txt.insert("end", text + "\n", tag)
        try:
            lines = int(self.txt.index("end-1c").split(".")[0])
            if lines > LOG_MAX_LINES:
                self.txt.delete("1.0", f"{lines - LOG_MAX_LINES + 1}.0")
        except Exception:  # noqa: BLE001
            pass
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _log_internal_error(self, what: str, exc: BaseException) -> None:
        try:
            self._append(f"Interner Fehler in der Oberfläche ({what}): {type(exc).__name__}: {exc}", "error")
        except Exception:  # noqa: BLE001
            pass

    # ------------------------------------------------------------------ Polling
    def _handle(self, item: tuple) -> None:
        kind = item[0]
        if kind == "log":
            _k, level, msg = item
            if level in HIDDEN_LEVELS:
                return
            self._append(msg, {"warn": "warn", "error": "error"}.get(level, ""))
        elif kind == "progress":
            _k, name, cf, tf, eta, text = item
            self.pb_clip["value"] = max(0.0, min(1.0, cf or 0.0)) * 1000
            self.pb_total["value"] = max(0.0, min(1.0, tf or 0.0)) * 1000
            self._last_total = max(0.0, min(1.0, tf or 0.0))
            clip_txt, total_txt = progress_texts(name, text, tf or 0.0, eta)
            self.var_clip_label.set(clip_txt)
            self.var_total_label.set(total_txt)
        elif kind == "overview":
            _k, token, text, ok = item
            if token == self.overview_token:
                self.var_overview.set(text)
                if not ok:
                    self._last_overview_key = None  # erneute Analyse erlauben (z. B. Karte nachträglich eingesteckt)
        elif kind == "finished":
            self._finish(item[1])
        elif kind == "crashed":
            self._append("Unerwarteter Fehler: " + item[1], "error")
            self.var_clip_label.set("")
            self.var_total_label.set("")
            self._reset_buttons("Fehler.")
            if not self._closing:
                self.after(50, lambda m=item[1]: messagebox.showerror(
                    "XDROOT-Converter", "Unerwarteter Fehler:\n" + m))

    def _poll(self) -> None:
        if self._destroyed:
            return
        try:
            for _ in range(200):
                try:
                    item = self.q.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle(item)
                except Exception as e:  # noqa: BLE001
                    self._log_internal_error(str(item[0]) if item else "?", e)
            self._watchdog()
            if self._closing:
                self._poll_closing()
        except Exception as e:  # noqa: BLE001
            self._log_internal_error("poll", e)
        finally:
            if not self._destroyed:
                self._after_id = self.after(100, self._poll)

    _destroyed = False

    def _watchdog(self) -> None:
        """Worker-Thread tot, Queue leer, Knöpfe noch im Lauf-Zustand -> Oberfläche zurücksetzen."""
        if self._ui_running and not self._is_running() and self.q.empty():
            self._append("Interner Fehler: Der Arbeitsthread wurde unerwartet beendet.", "error")
            self._reset_buttons("Fehler.")
            if not self._closing:
                self.after(50, lambda: messagebox.showerror(
                    "XDROOT-Converter",
                    "Der Arbeitsthread wurde unerwartet beendet. Bitte das Protokoll prüfen; "
                    "ein erneuter Lauf mit demselben Zielordner setzt fort."))

    def _finish(self, summary: "converter.RunSummary") -> None:
        try:
            ok = not (summary.cancelled or summary.fatal or summary.failed)
            if ok:
                self.pb_clip["value"] = 1000
                self.pb_total["value"] = 1000
                self.var_clip_label.set("")
                self.var_total_label.set("Gesamt 100 %")
            elif summary.fatal:
                self.pb_clip["value"] = 0
                self.pb_total["value"] = 0
                self.var_clip_label.set("")
                self.var_total_label.set("")
            else:
                self.var_clip_label.set("")
                self.var_total_label.set(progress_texts("", "", self._last_total, 0)[1])
            # Die Zusammenfassung steht bereits im Protokoll (converter.run schreibt sie über log)
            if summary.log_path:
                self._append(f"Protokoll: {summary.log_path}")
        except Exception as e:  # noqa: BLE001
            self._log_internal_error("finish", e)
        finally:
            self._reset_buttons(finish_status(summary))
        if not self._closing:
            self.after(50, lambda s=summary: self._show_result(s))

    def _show_result(self, summary) -> None:
        kind, title, text = completion_message(summary)
        show = {"info": messagebox.showinfo, "warning": messagebox.showwarning, "error": messagebox.showerror}[kind]
        show(title, text)

    def _reset_buttons(self, status: str) -> None:
        self._ui_running = False
        try:
            self._set_inputs(True)
            self.btn_start.configure(state="normal")
            self.btn_cancel.configure(state="disabled")
            self.var_status.set(status)
        finally:
            # Übersicht neu anstoßen (Text könnte noch „Analysiere …“ zeigen)
            self._last_overview_key = None
            if not self._closing:
                self._schedule_overview(300)

    # ------------------------------------------------------------------ Schließen
    def _on_close(self) -> None:
        if self._closing:
            return
        if self._is_running():
            if not messagebox.askyesno("XDROOT-Converter", "Es läuft noch eine Umwandlung. Abbrechen und beenden?"):
                return
            self._closing = True
            self._close_started = self._close_asked = time.monotonic()
            if self.cancel_token is not None:
                self.cancel_token.cancel()
            self._cancel_overview()
            self._set_inputs(False)
            self.btn_start.configure(state="disabled")
            self.btn_cancel.configure(state="disabled")
            self.var_status.set("Breche ab … das Fenster schließt sich, sobald alles beendet ist.")
            return
        self._cancel_overview()
        self._destroy()

    def _poll_closing(self) -> None:
        if not self._is_running():
            self._destroy()
            return
        if time.monotonic() - self._close_asked > CLOSE_WAIT_S:
            self._close_asked = time.monotonic()
            if messagebox.askyesno(
                "XDROOT-Converter",
                "Das Beenden dauert länger als erwartet. Programm jetzt hart beenden?\n"
                "(Eine angefangene Ausgabedatei kann dabei unvollständig bleiben.)",
            ):
                self._destroy()

    def _destroy(self) -> None:
        if self._destroyed:
            return
        self._destroyed = True
        for aid in (self._after_id, self._ov_after_id):
            if aid:
                try:
                    self.after_cancel(aid)
                except Exception:  # noqa: BLE001
                    pass
        self.destroy()


def _set_dpi_awareness() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        try:
            import ctypes

            ctypes.windll.user32.SetProcessDPIAware()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass


def main() -> None:
    if tk is None:
        raise RuntimeError(
            "tkinter ist in dieser Python-Installation nicht vorhanden. Bitte Python von python.org "
            "installieren und dabei „tcl/tk and IDLE“ ausgewählt lassen."
        )
    _set_dpi_awareness()
    App().mainloop()


if __name__ == "__main__":
    main()
