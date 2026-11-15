"""tkinter-Oberflaeche (Deutsch)."""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from typing import Optional

from . import __version__, converter, tools

PRESETS = ["ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"]


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


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title(f"XDROOT-Converter {__version__} - XAVC-I nach XAVC-L")
        self.geometry("860x680")
        self.minsize(720, 520)
        self.q: "queue.Queue" = queue.Queue()
        self.worker: Optional[threading.Thread] = None
        self.cancel_token: Optional[tools.CancelToken] = None
        self.overview_token = 0
        self._after_id: Optional[str] = None
        self._closing = False

        self.var_src = tk.StringVar()
        self.var_dst = tk.StringVar()
        self.var_bitrate = tk.StringVar(value="50")
        self.var_preset = tk.StringVar(value="medium")
        self.var_skip_unlisted = tk.BooleanVar(value=False)
        self.var_status = tk.StringVar(value="Bereit.")
        self.var_overview = tk.StringVar(value="Bitte Quellordner waehlen.")
        self.var_clip_label = tk.StringVar(value="")
        self.var_total_label = tk.StringVar(value="")
        self.settings_open = tk.BooleanVar(value=False)

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._after_id = self.after(100, self._poll)

    # ------------------------------------------------------------------ Aufbau
    def _build(self) -> None:
        pad = {"padx": 8, "pady": 4}
        root = ttk.Frame(self, padding=8)
        root.pack(fill="both", expand=True)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(7, weight=1)

        ttk.Label(root, text="Quellordner:").grid(row=0, column=0, sticky="w", **pad)
        e_src = ttk.Entry(root, textvariable=self.var_src)
        e_src.grid(row=0, column=1, sticky="ew", **pad)
        ttk.Button(root, text="Durchsuchen...", command=self._browse_src).grid(row=0, column=2, **pad)
        e_src.bind("<Return>", lambda _e: self._refresh_overview())
        e_src.bind("<FocusOut>", lambda _e: self._refresh_overview())

        ttk.Label(root, text="Zielordner:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(root, textvariable=self.var_dst).grid(row=1, column=1, sticky="ew", **pad)
        ttk.Button(root, text="Durchsuchen...", command=self._browse_dst).grid(row=1, column=2, **pad)

        ttk.Label(root, textvariable=self.var_overview, wraplength=780, justify="left").grid(
            row=2, column=0, columnspan=3, sticky="w", **pad)

        # Einstellungen (einklappbar)
        self.btn_settings = ttk.Button(root, text="Einstellungen anzeigen", command=self._toggle_settings)
        self.btn_settings.grid(row=3, column=0, sticky="w", **pad)
        self.frm_settings = ttk.LabelFrame(root, text="Einstellungen", padding=8)
        ttk.Label(self.frm_settings, text="Bitrate (Mbit/s):").grid(row=0, column=0, sticky="w", padx=4, pady=2)
        ttk.Spinbox(self.frm_settings, from_=10, to=100, increment=5, width=6, textvariable=self.var_bitrate).grid(
            row=0, column=1, sticky="w", padx=4, pady=2)
        ttk.Label(self.frm_settings, text="x264-Preset:").grid(row=0, column=2, sticky="w", padx=(16, 4), pady=2)
        ttk.Combobox(self.frm_settings, values=PRESETS, textvariable=self.var_preset, width=12, state="readonly").grid(
            row=0, column=3, sticky="w", padx=4, pady=2)
        ttk.Checkbutton(
            self.frm_settings, text="Clips, die nicht in MEDIAPRO.XML stehen, ueberspringen",
            variable=self.var_skip_unlisted,
        ).grid(row=1, column=0, columnspan=4, sticky="w", padx=4, pady=2)

        bf = ttk.Frame(root)
        bf.grid(row=4, column=0, columnspan=3, sticky="w", **pad)
        self.btn_start = ttk.Button(bf, text="Konvertieren", command=self._start)
        self.btn_start.pack(side="left", padx=(0, 8))
        self.btn_cancel = ttk.Button(bf, text="Abbrechen", command=self._cancel, state="disabled")
        self.btn_cancel.pack(side="left")

        ttk.Label(root, textvariable=self.var_clip_label).grid(row=5, column=0, columnspan=3, sticky="w", padx=8)
        self.pb_clip = ttk.Progressbar(root, maximum=1000)
        self.pb_clip.grid(row=5, column=0, columnspan=3, sticky="ew", padx=8, pady=(22, 2))
        ttk.Label(root, textvariable=self.var_total_label).grid(row=6, column=0, columnspan=3, sticky="w", padx=8)
        self.pb_total = ttk.Progressbar(root, maximum=1000)
        self.pb_total.grid(row=6, column=0, columnspan=3, sticky="ew", padx=8, pady=(22, 6))

        logf = ttk.Frame(root)
        logf.grid(row=7, column=0, columnspan=3, sticky="nsew", padx=8, pady=4)
        logf.rowconfigure(0, weight=1)
        logf.columnconfigure(0, weight=1)
        self.txt = tk.Text(logf, height=12, wrap="word", state="disabled", font=("Consolas", 9))
        sb = ttk.Scrollbar(logf, orient="vertical", command=self.txt.yview)
        self.txt.configure(yscrollcommand=sb.set)
        self.txt.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self.txt.tag_configure("warn", foreground="#9a6700")
        self.txt.tag_configure("error", foreground="#b00020")

        ttk.Label(root, textvariable=self.var_status, relief="sunken", anchor="w").grid(
            row=8, column=0, columnspan=3, sticky="ew", padx=8, pady=(4, 0))

    def _toggle_settings(self) -> None:
        if self.settings_open.get():
            self.frm_settings.grid_remove()
            self.btn_settings.configure(text="Einstellungen anzeigen")
            self.settings_open.set(False)
        else:
            self.frm_settings.grid(row=3, column=1, columnspan=2, sticky="ew", padx=8, pady=4)
            self.btn_settings.configure(text="Einstellungen verbergen")
            self.settings_open.set(True)

    # ------------------------------------------------------------------ Ordner
    def _browse_src(self) -> None:
        d = filedialog.askdirectory(title="Quellordner (XDROOT oder Elternordner) waehlen")
        if d:
            self.var_src.set(d)
            self._refresh_overview()

    def _browse_dst(self) -> None:
        d = filedialog.askdirectory(title="Zielordner waehlen (darin entsteht XDROOT)")
        if d:
            self.var_dst.set(d)

    _last_overview_src = ""

    def _refresh_overview(self) -> None:
        src = self.var_src.get().strip()
        if not src or src == self._last_overview_src or (self.worker and self.worker.is_alive()):
            return
        self._last_overview_src = src
        self.overview_token += 1
        token = self.overview_token
        self.var_overview.set("Analysiere Quelle ...")

        opts = converter.Options(
            source=src, target="", bitrate_mbps=self._bitrate(),
            unlisted="skip" if self.var_skip_unlisted.get() else "convert",
        )

        def work() -> None:
            try:
                ov = converter.overview(opts)
                self.q.put(("overview", token, ov.text()))
            except Exception as e:  # noqa: BLE001
                self.q.put(("overview", token, f"Quelle nicht nutzbar: {e}"))

        threading.Thread(target=work, daemon=True).start()

    def _bitrate(self) -> float:
        try:
            v = float(self.var_bitrate.get().replace(",", "."))
        except ValueError:
            return 50.0
        return min(max(v, 5.0), 200.0)

    # ------------------------------------------------------------------ Lauf
    def _start(self) -> None:
        src, dst = self.var_src.get().strip(), self.var_dst.get().strip()
        if not src or not dst:
            messagebox.showwarning("XDROOT-Converter", "Bitte Quell- und Zielordner angeben.")
            return
        try:
            tools.check_tools()
        except Exception as e:  # noqa: BLE001
            messagebox.showerror("XDROOT-Converter", str(e))
            return
        opts = converter.Options(
            source=src, target=dst, bitrate_mbps=self._bitrate(), preset=self.var_preset.get() or "medium",
            unlisted="skip" if self.var_skip_unlisted.get() else "convert",
        )
        self.cancel_token = tools.CancelToken()
        conv = converter.Converter(opts, QueueReporter(self.q), self.cancel_token)
        self.btn_start.configure(state="disabled")
        self.btn_cancel.configure(state="normal")
        self.pb_clip["value"] = 0
        self.pb_total["value"] = 0
        self.var_status.set("Laeuft ...")

        def work() -> None:
            try:
                summary = conv.run()
                self.q.put(("finished", summary))
            except Exception as e:  # noqa: BLE001
                self.q.put(("crashed", str(e)))

        self.worker = threading.Thread(target=work, daemon=True)
        self.worker.start()

    def _cancel(self) -> None:
        if self.cancel_token is not None:
            self.cancel_token.cancel()
            self.var_status.set("Abbruch angefordert - laufende Prozesse werden beendet ...")
            self.btn_cancel.configure(state="disabled")

    def _append(self, text: str, tag: str = "") -> None:
        self.txt.configure(state="normal")
        self.txt.insert("end", text + "\n", tag)
        self.txt.see("end")
        self.txt.configure(state="disabled")

    def _poll(self) -> None:
        if self._closing:
            return
        try:
            for _ in range(200):
                item = self.q.get_nowait()
                kind = item[0]
                if kind == "log":
                    _k, level, msg = item
                    self._append(msg, {"warn": "warn", "error": "error"}.get(level, ""))
                elif kind == "progress":
                    _k, name, cf, tf, eta, text = item
                    self.pb_clip["value"] = cf * 1000
                    self.pb_total["value"] = tf * 1000
                    self.var_clip_label.set(f"{name}  {text}")
                    eta_s = f", Restzeit ca. {converter.fmt_duration(eta)}" if eta else ""
                    self.var_total_label.set(f"Gesamt {tf * 100:.1f} %{eta_s}")
                elif kind == "overview":
                    _k, token, text = item
                    if token == self.overview_token:
                        self.var_overview.set(text)
                elif kind == "finished":
                    summary = item[1]
                    self._finish(summary)
                elif kind == "crashed":
                    self._append("Unerwarteter Fehler: " + item[1], "error")
                    self._reset_buttons("Fehler.")
        except queue.Empty:
            pass
        self._after_id = self.after(100, self._poll)

    def _finish(self, summary: "converter.RunSummary") -> None:
        if summary.cancelled:
            msg = "Abgebrochen."
        elif summary.fatal:
            msg = "Fehler: " + summary.fatal
        elif summary.failed:
            msg = f"Fertig mit {summary.failed} Fehler(n) - siehe Protokoll."
        else:
            msg = "Fertig."
            self.pb_clip["value"] = 1000
            self.pb_total["value"] = 1000
        self._reset_buttons(msg)
        if summary.log_path:
            self._append(f"Protokoll: {summary.log_path}")

    def _reset_buttons(self, status: str) -> None:
        self.btn_start.configure(state="normal")
        self.btn_cancel.configure(state="disabled")
        self.var_status.set(status)

    def _on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno("XDROOT-Converter", "Es laeuft noch eine Umwandlung. Abbrechen und beenden?"):
                return
            if self.cancel_token is not None:
                self.cancel_token.cancel()
            self.worker.join(timeout=15)
        self._closing = True
        if self._after_id:
            try:
                self.after_cancel(self._after_id)
            except Exception:
                pass
        self.destroy()


def main() -> None:
    App().mainloop()


if __name__ == "__main__":
    main()
