"""Invoice Renamer - desktop app around invoice_renamer.py.

Same rules and OCR as the command-line script; adds a dashboard with live progress,
a results table with scan preview, an activity log and undo for the session.

    python invoice_renamer_gui.py          (or run InvoiceRenamer.exe)
"""

from __future__ import annotations

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

# A windowed exe has no console; give libraries that print somewhere harmless to write.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import customtkinter as ctk
from PIL import Image, ImageOps

import invoice_renamer as core

APP_NAME = "Invoice Renamer"
VERSION = "1.0"
DEVELOPER = "CyberMAK 2026"
SETTINGS_FILE = Path(os.environ.get("APPDATA", str(Path.home()))) / "InvoiceRenamer" / "settings.json"
DEFAULTS = dict(
    folder=r"D:\Work\AAW\New Samples", log_dir=r"C:\RPA", review_dir="",
    mode="Watch", interval=30, dry_run=False,
    prefix="SKM", types=",".join(core.DEFAULT_KNOWN_TYPES),
    ms_above=core.DEFAULT_MS_ABOVE, settle=5, appearance="Dark",
)

GREEN, BLUE, AMBER, RED, GREY, PURPLE = "#2FA572", "#3B8ED0", "#E0A526", "#E05D5D", "#8A8F98", "#9B7BD4"
STATUS = {  # status -> (label, colour)
    "renamed": ("Renamed", GREEN),
    "would_rename": ("Would rename", BLUE),
    "needs_review": ("Needs review", AMBER),
    "error": ("Error", RED),
    "unchanged": ("Already OK", GREY),
    "undone": ("Undone", PURPLE),
}
FILTERS = {"All": None, "Renamed": {"renamed", "would_rename", "unchanged"},
           "Review": {"needs_review"}, "Errors": {"error"}, "Undone": {"undone"}}
PREVIEW_HEADER = (0.45, 0.05, 1.00, 0.47)  # a bit wider than the OCR crop, for context


def resource(name: str) -> Path:
    """Files bundled with the exe live in _MEIPASS; next to the script otherwise."""
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / name


def open_page(path: Path) -> Image.Image:
    """First page of an image or PDF, as a PIL image."""
    if path.suffix.lower() != ".pdf":
        return Image.open(path)
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    try:
        return pdf[0].render(scale=150 / 72).to_pil()
    except Exception as e:
        raise OSError(f"cannot render PDF: {e}") from e
    finally:
        pdf.close()


def load_settings() -> dict:
    try:
        return {**DEFAULTS, **json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))}
    except (OSError, ValueError):
        return dict(DEFAULTS)


def save_settings(cfg: dict) -> None:
    try:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    except OSError:
        pass


class QueueLogHandler(logging.Handler):
    """Sends the core script's log lines to the GUI's activity tab."""

    def __init__(self, events: queue.Queue):
        super().__init__()
        self.events = events

    def emit(self, record: logging.LogRecord) -> None:
        self.events.put(("log", record.levelno, record.getMessage()))


# --------------------------------------------------------------------------- worker
class Worker(threading.Thread):
    """Runs the renamer off the UI thread and reports back through a queue."""

    def __init__(self, cfg: SimpleNamespace, events: queue.Queue, cache: dict):
        super().__init__(daemon=True)
        self.cfg, self.events, self.cache = cfg, events, cache
        self.stop_event = threading.Event()

    def emit(self, *event) -> None:
        self.events.put(event)

    def run(self) -> None:
        try:
            self._run()
        except Exception as e:  # never let the thread die silently
            core.log.exception("Run failed")
            self.emit("fatal", f"{type(e).__name__}: {e}")
        finally:
            self.emit("stopped")

    def _reader(self) -> core.InvoiceReader:
        key = (tuple(self.cfg.types), self.cfg.ms_above)
        if self.cache.get("key") != key:  # loading the OCR models takes a moment; reuse them
            self.emit("status", "Loading OCR engine…")
            self.cache["reader"] = core.InvoiceReader(list(key[0]), ms_above=key[1])
            self.cache["key"] = key
        return self.cache["reader"]

    def _run(self) -> None:
        c = self.cfg
        folder = Path(c.folder)
        args = SimpleNamespace(prefix=c.prefix, include_renamed=False, settle=c.settle,
                               dry_run=c.dry_run, review_dir=c.review_dir or None,
                               no_log=False, log_dir=c.log_dir)
        reader = self._reader()
        seen: set[Path] = set()

        def record(row: dict) -> None:
            if not args.dry_run:
                core.write_log(Path(args.log_dir), [row])
            self.emit("row", dict(row, _folder=str(folder), _review=c.review_dir))

        while not self.stop_event.is_set():
            self.emit("status", "Scanning folder…")
            todo = core.pending_files(folder, args, seen)
            if todo:
                self.emit("pass_start", len(todo))
                for i, path in enumerate(todo, 1):
                    if self.stop_event.is_set():
                        break
                    self.emit("file_start", path.name, i, len(todo))
                    core._process_one(path, reader, args, seen, record)
                self.emit("pass_done")
            if c.mode != "Watch":
                break
            end = time.time() + c.interval
            while not self.stop_event.is_set():
                left = end - time.time()
                if left <= 0:
                    break
                self.emit("waiting", int(left + 0.999))
                self.stop_event.wait(min(1.0, left))


# --------------------------------------------------------------------------- widgets
class StatCard(ctk.CTkFrame):
    def __init__(self, master, title: str, colour: str, icon: str):
        super().__init__(master, corner_radius=14, height=96)
        self.grid_columnconfigure(1, weight=1)
        ctk.CTkFrame(self, width=5, height=40, corner_radius=3, fg_color=colour).grid(
            row=0, column=0, rowspan=2, sticky="ns", padx=(10, 12), pady=14)
        self.value = ctk.CTkLabel(self, text="0", font=ctk.CTkFont("Segoe UI", 30, "bold"),
                                  text_color=colour, anchor="w")
        self.value.grid(row=0, column=1, sticky="w", pady=(10, 0))
        self.caption = ctk.CTkLabel(self, text=f"{icon}  {title}", anchor="w",
                                    font=ctk.CTkFont("Segoe UI", 13),
                                    text_color=("gray35", "gray70"))
        self.caption.grid(row=1, column=1, sticky="w", pady=(0, 12))

    def set(self, value, caption: str | None = None) -> None:
        self.value.configure(text=str(value))
        if caption:
            self.caption.configure(text=caption)


# --------------------------------------------------------------------------- app
class App(ctk.CTk):
    def __init__(self):
        self.settings = load_settings()
        ctk.set_appearance_mode(self.settings["appearance"])
        ctk.set_default_color_theme("blue")
        super().__init__()
        self.title(f"{APP_NAME}  ·  AAW scanned invoices  ·  Developed by {DEVELOPER}")
        self.geometry("1440x860")
        self.minsize(1180, 720)
        ico = resource("app.ico")
        if ico.exists():
            self.iconbitmap(str(ico))
            self.after(300, lambda: self.iconbitmap(str(ico)))  # CTk resets it on start

        self.events: queue.Queue = queue.Queue()
        self.reader_cache: dict = {}
        self.worker: Worker | None = None
        self.rows: list[dict] = []
        self.pass_total = self.pass_done = 0
        self.pass_started = 0.0
        self.run_started = 0.0
        self.selected: dict | None = None
        self.preview_img = None

        handler = QueueLogHandler(self.events)
        core.log.addHandler(handler)
        core.log.setLevel(logging.INFO)
        core.log.propagate = False

        self.f_title = ctk.CTkFont("Segoe UI", 22, "bold")
        self.f_h2 = ctk.CTkFont("Segoe UI", 15, "bold")
        self.f_body = ctk.CTkFont("Segoe UI", 13)
        self.f_small = ctk.CTkFont("Segoe UI", 12)

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_sidebar()
        self._build_main()
        self._style_tree()
        self._refresh_stats()
        self._count_waiting()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.after(80, self._poll)

    # ---------------- sidebar
    def _build_sidebar(self) -> None:
        s = self.settings
        side = ctk.CTkFrame(self, width=330, corner_radius=0, fg_color=("gray92", "gray13"))
        side.grid(row=0, column=0, sticky="nsew")
        side.grid_propagate(False)
        side.grid_columnconfigure(0, weight=1)
        side.grid_rowconfigure(1, weight=1)

        head = ctk.CTkFrame(side, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=22, pady=(22, 8))
        ctk.CTkLabel(head, text="🧾  " + APP_NAME, font=self.f_title, anchor="w").pack(anchor="w")
        ctk.CTkLabel(head, text="Scans  →  ORDER-INVOICE-TYPE", font=self.f_small,
                     text_color=("gray40", "gray60"), anchor="w").pack(anchor="w", pady=(2, 0))

        body = ctk.CTkScrollableFrame(side, fg_color="transparent")
        body.grid(row=1, column=0, sticky="nsew", padx=10)
        body.grid_columnconfigure(0, weight=1)
        self.inputs: list = []

        def section(text: str):
            ctk.CTkLabel(body, text=text.upper(), font=ctk.CTkFont("Segoe UI", 11, "bold"),
                         text_color=("gray45", "gray55"), anchor="w").pack(
                fill="x", padx=12, pady=(16, 4))

        def folder_row(label: str, key: str, optional: bool = False):
            ctk.CTkLabel(body, text=label, font=self.f_body, anchor="w").pack(fill="x", padx=12)
            row = ctk.CTkFrame(body, fg_color="transparent")
            row.pack(fill="x", padx=12, pady=(2, 8))
            var = tk.StringVar(value=s[key])
            entry = ctk.CTkEntry(row, textvariable=var, height=34,
                                 placeholder_text="(optional)" if optional else "")
            entry.pack(side="left", fill="x", expand=True)
            btn = ctk.CTkButton(row, text="Browse", width=68, height=34,
                                command=lambda: self._browse(var))
            btn.pack(side="left", padx=(6, 0))
            self.inputs += [entry, btn]
            return var

        section("Folders")
        self.v_folder = folder_row("Scans folder", "folder")
        self.v_folder.trace_add("write", lambda *_: self.after(400, self._count_waiting))
        self.v_log = folder_row("Log folder", "log_dir")
        self.v_review = folder_row("Move unclear scans to", "review_dir", optional=True)

        section("Mode")
        self.v_mode = tk.StringVar(value=s["mode"])
        seg = ctk.CTkSegmentedButton(body, values=["Run once", "Watch"], variable=self.v_mode,
                                     height=34, command=lambda _: self._mode_changed())
        seg.pack(fill="x", padx=12, pady=(2, 8))
        self.inputs.append(seg)
        self.interval_row = ctk.CTkFrame(body, fg_color="transparent")
        self.interval_row.pack(fill="x", padx=12, pady=(0, 6))
        ctk.CTkLabel(self.interval_row, text="Check every", font=self.f_body).pack(side="left")
        self.v_interval = tk.StringVar(value=str(s["interval"]))
        e = ctk.CTkEntry(self.interval_row, textvariable=self.v_interval, width=60, height=30,
                         justify="center")
        e.pack(side="left", padx=8)
        ctk.CTkLabel(self.interval_row, text="seconds", font=self.f_body).pack(side="left")
        self.inputs.append(e)
        self.v_dry = tk.BooleanVar(value=s["dry_run"])
        sw = ctk.CTkSwitch(body, text="Preview only (don't rename)", variable=self.v_dry,
                           font=self.f_body)
        sw.pack(fill="x", padx=12, pady=(6, 4))
        self.inputs.append(sw)

        self.adv_open = False
        self.adv_btn = ctk.CTkButton(body, text="▸  Advanced rules", anchor="w",
                                     fg_color="transparent", hover_color=("gray85", "gray20"),
                                     text_color=("gray20", "gray85"), font=self.f_body,
                                     command=self._toggle_advanced)
        self.adv_btn.pack(fill="x", padx=6, pady=(14, 0))
        self.adv = ctk.CTkFrame(body, fg_color=("gray88", "gray17"), corner_radius=10)
        self.adv.grid_columnconfigure(1, weight=1)

        def adv_field(r: int, label: str, key: str, tip: str):
            ctk.CTkLabel(self.adv, text=label, font=self.f_body, anchor="w").pack(
                fill="x", padx=12, pady=(10, 2))
            var = tk.StringVar(value=str(s[key]))
            ent = ctk.CTkEntry(self.adv, textvariable=var, height=30)
            ent.pack(fill="x", padx=12)
            ctk.CTkLabel(self.adv, text=tip, font=ctk.CTkFont("Segoe UI", 11), anchor="w",
                         text_color=("gray45", "gray55"), justify="left", wraplength=260).pack(
                fill="x", padx=12, pady=(2, 0))
            self.inputs.append(ent)
            return var

        self.v_prefix = adv_field(0, "File prefix", "prefix", "Only files starting with this. Empty = all.")
        self.v_types = adv_field(1, "Order types", "types", "Known types, comma separated.")
        self.v_ms = adv_field(2, "MS above", "ms_above", "Orders above this are always MS. -1 = off.")
        self.v_settle = adv_field(3, "Settle (s)", "settle", "Skip files changed in the last N seconds.")
        ctk.CTkFrame(self.adv, height=10, fg_color="transparent").pack()
        self.adv_after = ctk.CTkFrame(body, height=1, fg_color="transparent")
        self.adv_after.pack(fill="x")

        foot = ctk.CTkFrame(side, fg_color="transparent")
        foot.grid(row=2, column=0, sticky="ew", padx=22, pady=(8, 18))
        foot.grid_columnconfigure(0, weight=1)
        self.start_btn = ctk.CTkButton(foot, text="▶   Start", height=48, corner_radius=12,
                                       font=ctk.CTkFont("Segoe UI", 16, "bold"),
                                       fg_color=GREEN, hover_color="#258a5e",
                                       command=self._start_stop)
        self.start_btn.grid(row=0, column=0, sticky="ew")
        theme = ctk.CTkFrame(foot, fg_color="transparent")
        theme.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        ctk.CTkLabel(theme, text="Theme", font=self.f_small,
                     text_color=("gray40", "gray60")).pack(side="left")
        self.v_theme = tk.StringVar(value=s["appearance"])
        ctk.CTkOptionMenu(theme, values=["Dark", "Light", "System"], variable=self.v_theme,
                          width=110, height=28, command=self._set_theme).pack(side="right")
        ctk.CTkLabel(foot, text=f"Developed by {DEVELOPER}", font=ctk.CTkFont("Segoe UI", 12, "bold"),
                     text_color=("gray35", "gray65")).grid(row=2, column=0, sticky="w", pady=(12, 0))
        ctk.CTkLabel(foot, text=f"v{VERSION}  ·  offline OCR", font=ctk.CTkFont("Segoe UI", 11),
                     text_color=("gray50", "gray45")).grid(row=3, column=0, sticky="w")
        self._mode_changed()

    # ---------------- main area
    def _build_main(self) -> None:
        main = ctk.CTkFrame(self, fg_color="transparent")
        main.grid(row=0, column=1, sticky="nsew", padx=22, pady=18)
        main.grid_columnconfigure(0, weight=1)
        main.grid_rowconfigure(3, weight=1)

        top = ctk.CTkFrame(main, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew")
        ctk.CTkLabel(top, text="Dashboard", font=self.f_title).pack(side="left")
        self.pill = ctk.CTkLabel(top, text="●  Idle", corner_radius=14, height=30,
                                 fg_color=("gray85", "gray22"), font=self.f_body)
        self.pill.pack(side="right", ipadx=12)

        cards = ctk.CTkFrame(main, fg_color="transparent")
        cards.grid(row=1, column=0, sticky="ew", pady=(14, 12))
        for i in range(5):
            cards.grid_columnconfigure(i, weight=1, uniform="cards")
        self.c_wait = StatCard(cards, "Waiting in folder", BLUE, "⏳")
        self.c_done = StatCard(cards, "Renamed", GREEN, "✔")
        self.c_review = StatCard(cards, "Needs review", AMBER, "⚠")
        self.c_err = StatCard(cards, "Errors", RED, "✖")
        self.c_speed = StatCard(cards, "Seconds per file", PURPLE, "⚡")
        for i, c in enumerate([self.c_wait, self.c_done, self.c_review, self.c_err, self.c_speed]):
            c.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 6, 0 if i == 4 else 6))
        self.c_speed.set("–")

        prog = ctk.CTkFrame(main, corner_radius=14)
        prog.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        prog.grid_columnconfigure(0, weight=1)
        self.l_current = ctk.CTkLabel(prog, text="Ready. Choose a folder and press Start.",
                                      font=self.f_h2, anchor="w")
        self.l_current.grid(row=0, column=0, sticky="ew", padx=18, pady=(14, 6))
        self.l_pct = ctk.CTkLabel(prog, text="", font=self.f_h2, anchor="e")
        self.l_pct.grid(row=0, column=1, sticky="e", padx=18, pady=(14, 6))
        self.bar = ctk.CTkProgressBar(prog, height=14, corner_radius=7, progress_color=GREEN)
        self.bar.grid(row=1, column=0, columnspan=2, sticky="ew", padx=18)
        self.bar.set(0)
        self.l_eta = ctk.CTkLabel(prog, text=" ", font=self.f_small, anchor="w",
                                  text_color=("gray40", "gray60"))
        self.l_eta.grid(row=2, column=0, columnspan=2, sticky="ew", padx=18, pady=(6, 12))

        content = ctk.CTkFrame(main, fg_color="transparent")
        content.grid(row=3, column=0, sticky="nsew")
        content.grid_columnconfigure(0, weight=1)
        content.grid_rowconfigure(0, weight=1)
        self._build_tabs(content)
        self._build_preview(content)

    def _build_tabs(self, parent) -> None:
        tabs = ctk.CTkTabview(parent, corner_radius=14, anchor="nw")
        tabs.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        t_res, t_log = tabs.add("Results"), tabs.add("Activity log")

        t_res.grid_columnconfigure(0, weight=1)
        t_res.grid_rowconfigure(1, weight=1)
        bar = ctk.CTkFrame(t_res, fg_color="transparent")
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.search = ctk.CTkEntry(bar, placeholder_text="Search file, order or invoice…",
                                   width=260, height=32)
        self.search.pack(side="left")
        self.search.bind("<KeyRelease>", lambda _: self._refill_tree())
        self.v_filter = tk.StringVar(value="All")
        ctk.CTkSegmentedButton(bar, values=list(FILTERS), variable=self.v_filter, height=32, width=330, dynamic_resizing=False,
                               command=lambda _: self._refill_tree()).pack(side="left", padx=10)

        cols = ("time", "file", "new", "order", "invoice", "type", "status", "secs")
        heads = ("Time", "Original file", "New name", "Order", "Invoice", "Type", "Status", "Sec")
        widths = (68, 220, 180, 78, 78, 46, 100, 44)
        self.tree = ttk.Treeview(t_res, columns=cols, show="headings", style="Inv.Treeview",
                                 selectmode="browse")
        for c, h, w in zip(cols, heads, widths):
            self.tree.heading(c, text=h, anchor="w")
            self.tree.column(c, width=w, minwidth=40, anchor="w", stretch=c in ("file", "new"))
        self.tree.grid(row=1, column=0, sticky="nsew")
        sb = ctk.CTkScrollbar(t_res, command=self.tree.yview)
        sb.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind("<<TreeviewSelect>>", self._on_select)
        self.tree.bind("<Double-1>", lambda _: self._open_selected())

        actions = ctk.CTkFrame(t_res, fg_color="transparent")
        actions.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        btn = dict(height=32, corner_radius=8, fg_color=("gray80", "gray25"),
                   hover_color=("gray72", "gray32"), text_color=("gray10", "gray90"))
        ctk.CTkButton(actions, text="Open scans folder", command=self._open_folder, **btn).pack(side="left")
        ctk.CTkButton(actions, text="Open log", command=self._open_log, **btn).pack(side="left", padx=8)
        ctk.CTkButton(actions, text="Clear list", command=self._clear, **btn).pack(side="left")
        self.undo_btn = ctk.CTkButton(actions, text="↶  Undo renames", height=32,
                                      corner_radius=8, fg_color=PURPLE, hover_color="#7f62b8",
                                      command=self._undo_session)
        self.undo_btn.pack(side="right")

        t_log.grid_columnconfigure(0, weight=1)
        t_log.grid_rowconfigure(0, weight=1)
        self.logbox = ctk.CTkTextbox(t_log, font=ctk.CTkFont("Consolas", 12), wrap="none")
        self.logbox.grid(row=0, column=0, sticky="nsew")
        tb = self.logbox._textbox
        tb.tag_config("time", foreground=GREY)
        tb.tag_config("ok", foreground=GREEN)
        tb.tag_config("warn", foreground=AMBER)
        tb.tag_config("err", foreground=RED)
        tb.tag_config("info", foreground=BLUE)
        self.logbox.configure(state="disabled")

    def _build_preview(self, parent) -> None:
        pv = ctk.CTkFrame(parent, width=400, corner_radius=14)
        pv.grid(row=0, column=1, sticky="nsew")
        pv.grid_propagate(False)
        pv.grid_columnconfigure(0, weight=1)
        pv.grid_rowconfigure(2, weight=1)
        head = ctk.CTkFrame(pv, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 6))
        ctk.CTkLabel(head, text="Scan preview", font=self.f_h2).pack(side="left")
        self.v_view = tk.StringVar(value="Header")
        ctk.CTkSegmentedButton(head, values=["Header", "Full page"], variable=self.v_view,
                               height=28, command=lambda _: self._show_preview()).pack(side="right")
        self.name_lbl = ctk.CTkLabel(pv, text="Select a row to see the scan", font=self.f_small,
                                     text_color=("gray40", "gray60"), wraplength=360, anchor="w",
                                     justify="left")
        self.name_lbl.grid(row=1, column=0, sticky="ew", padx=16)
        self.img_lbl = ctk.CTkLabel(pv, text="🖼", font=ctk.CTkFont("Segoe UI", 54),
                                    text_color=("gray70", "gray30"),
                                    fg_color=("gray86", "gray16"), corner_radius=10)
        self.img_lbl.grid(row=2, column=0, sticky="nsew", padx=16, pady=8)
        self.img_lbl.bind("<Configure>", lambda _: self._schedule_preview())

        det = ctk.CTkFrame(pv, fg_color="transparent")
        det.grid(row=3, column=0, sticky="ew", padx=16)
        det.grid_columnconfigure((1, 3), weight=1)
        self.d = {}
        for i, (k, lab) in enumerate([("order", "Order"), ("invoice", "Invoice"),
                                      ("order_type", "Type"), ("status", "Status")]):
            r, c = divmod(i, 2)
            ctk.CTkLabel(det, text=lab, font=self.f_small, text_color=("gray40", "gray60")).grid(
                row=r, column=c * 2, sticky="w", padx=(0, 8), pady=2)
            self.d[k] = ctk.CTkLabel(det, text="–", font=ctk.CTkFont("Segoe UI", 14, "bold"), anchor="w")
            self.d[k].grid(row=r, column=c * 2 + 1, sticky="w", pady=2)
        self.d_detail = ctk.CTkLabel(pv, text="", font=self.f_small, wraplength=360, justify="left",
                                     anchor="w", text_color=AMBER)
        self.d_detail.grid(row=4, column=0, sticky="ew", padx=16, pady=(4, 0))
        btns = ctk.CTkFrame(pv, fg_color="transparent")
        btns.grid(row=5, column=0, sticky="ew", padx=16, pady=(8, 14))
        btns.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkButton(btns, text="Open image", height=32, command=self._open_selected).grid(
            row=0, column=0, sticky="ew", padx=(0, 4))
        ctk.CTkButton(btns, text="Show in folder", height=32, command=self._reveal_selected).grid(
            row=0, column=1, sticky="ew", padx=(4, 0))

    # ---------------- styling
    def _style_tree(self) -> None:
        dark = ctk.get_appearance_mode() == "Dark"
        scale = ctk.ScalingTracker.get_widget_scaling(self)
        bg, alt = ("#1d2026", "#22262d") if dark else ("#ffffff", "#f4f6f9")
        fg, head = ("#e4e6ea", "#2a2f37") if dark else ("#1c1f24", "#e8ecf1")
        st = ttk.Style(self)
        st.theme_use("clam")
        st.configure("Inv.Treeview", background=bg, fieldbackground=bg, foreground=fg,
                     rowheight=int(30 * scale), borderwidth=0, font=("Segoe UI", 10))
        st.configure("Inv.Treeview.Heading", background=head, foreground=fg, relief="flat",
                     font=("Segoe UI Semibold", 10), padding=(6, 6))
        st.map("Inv.Treeview", background=[("selected", "#1f6aa5")],
               foreground=[("selected", "#ffffff")])
        st.map("Inv.Treeview.Heading", background=[("active", head)])
        st.layout("Inv.Treeview", [("Inv.Treeview.treearea", {"sticky": "nswe"})])  # no border
        self.tree.tag_configure("odd", background=alt)
        self.tree.tag_configure("even", background=bg)
        for status, (_, colour) in STATUS.items():
            if status not in ("renamed", "unchanged"):
                self.tree.tag_configure(status, foreground=colour)

    def _set_theme(self, mode: str) -> None:
        ctk.set_appearance_mode(mode)
        self.after(50, self._style_tree)

    # ---------------- helpers
    def _browse(self, var: tk.StringVar) -> None:
        d = filedialog.askdirectory(initialdir=var.get() or None, parent=self)
        if d:
            var.set(str(Path(d)))

    def _toggle_advanced(self) -> None:
        self.adv_open = not self.adv_open
        if self.adv_open:
            self.adv.pack(fill="x", padx=12, pady=(4, 8), before=self.adv_after)
        else:
            self.adv.pack_forget()
        self.adv_btn.configure(text=("▾" if self.adv_open else "▸") + "  Advanced rules")

    def _mode_changed(self) -> None:
        watch = self.v_mode.get() == "Watch"
        for w in self.interval_row.winfo_children():
            if isinstance(w, ctk.CTkEntry):
                w.configure(state="normal" if watch and not self.running else "disabled")

    @property
    def running(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    def _collect(self) -> dict | None:
        try:
            cfg = dict(folder=self.v_folder.get().strip(), log_dir=self.v_log.get().strip(),
                       review_dir=self.v_review.get().strip(), mode=self.v_mode.get(),
                       interval=max(5, int(float(self.v_interval.get()))),
                       dry_run=bool(self.v_dry.get()), prefix=self.v_prefix.get().strip(),
                       types=self.v_types.get().strip().upper(),
                       ms_above=int(self.v_ms.get()), settle=float(self.v_settle.get()),
                       appearance=self.v_theme.get())
        except ValueError:
            messagebox.showerror(APP_NAME, "Interval, MS above and settle must be numbers.", parent=self)
            return None
        if not Path(cfg["folder"]).is_dir():
            messagebox.showerror(APP_NAME, f"Scans folder not found:\n{cfg['folder']}", parent=self)
            return None
        if not cfg["log_dir"]:
            cfg["log_dir"] = DEFAULTS["log_dir"]
        return cfg

    def _set_inputs(self, enabled: bool) -> None:
        for w in self.inputs:
            try:
                w.configure(state="normal" if enabled else "disabled")
            except (tk.TclError, ValueError):
                pass
        self._mode_changed()

    def _set_pill(self, text: str, colour: str | None = None) -> None:
        self.pill.configure(text=f"●  {text}", text_color=colour or ("gray20", "gray85"))

    def _log(self, text: str, tag: str = "") -> None:
        self.logbox.configure(state="normal")
        self.logbox._textbox.insert("end", datetime.now().strftime("%H:%M:%S  "), "time")
        self.logbox._textbox.insert("end", text + "\n", tag)
        lines = int(self.logbox._textbox.index("end-1c").split(".")[0])
        if lines > 5000:
            self.logbox._textbox.delete("1.0", f"{lines - 5000}.0")
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    # ---------------- start / stop
    def _start_stop(self) -> None:
        if self.running:
            self.worker.stop_event.set()
            self.start_btn.configure(state="disabled", text="Stopping…")
            self._set_pill("Stopping after this file…", AMBER)
            return
        cfg = self._collect()
        if not cfg:
            return
        self.settings.update(cfg)
        save_settings(self.settings)
        ns = SimpleNamespace(**cfg)
        ns.types = [t.strip() for t in cfg["types"].split(",") if t.strip()]
        ns.ms_above = None if cfg["ms_above"] < 0 else cfg["ms_above"]
        self.worker = Worker(ns, self.events, self.reader_cache)
        self.run_started = time.time()
        self._set_inputs(False)
        self.start_btn.configure(text="■   Stop", fg_color=RED, hover_color="#b84a4a")
        self._set_pill("Starting…", BLUE)
        self._log(f"Started ({cfg['mode'].lower()}{', preview only' if cfg['dry_run'] else ''}) "
                  f"on {cfg['folder']}", "info")
        self.worker.start()

    def _on_stopped(self) -> None:
        self.worker = None
        self._set_inputs(True)
        self.start_btn.configure(state="normal", text="▶   Start", fg_color=GREEN, hover_color="#258a5e")
        self._set_pill("Idle")
        self.l_eta.configure(text=" ")
        if self.pass_total and self.pass_done >= self.pass_total:
            self.l_current.configure(text="All done ✔")
        else:
            self.l_current.configure(text="Stopped.")
        self._log("Stopped.", "info")
        self._count_waiting()

    # ---------------- event pump
    def _poll(self) -> None:
        try:
            for _ in range(300):
                self._handle(self.events.get_nowait())
        except queue.Empty:
            pass
        self.after(80, self._poll)

    def _handle(self, ev: tuple) -> None:
        kind = ev[0]
        if kind == "log":
            level, msg = ev[1], ev[2]
            tag = ("err" if level >= logging.ERROR else "warn" if level >= logging.WARNING
                   else "ok" if msg.startswith("RENAMED") else "")
            self._log(msg, tag)
        elif kind == "status":
            self.l_current.configure(text=ev[1])
            self._set_pill(ev[1].rstrip("…"), BLUE)
        elif kind == "pass_start":
            self.pass_total, self.pass_done, self.pass_started = ev[1], 0, time.time()
            self.bar.set(0)
            self._log(f"Found {ev[1]} scan(s) to process", "info")
        elif kind == "file_start":
            name, i, n = ev[1], ev[2], ev[3]
            self.pass_done = i - 1
            self.l_current.configure(text=f"Reading  {name}")
            self._set_pill(f"Working  {i}/{n}", GREEN)
            self.c_wait.set(n - i + 1)
            self._update_progress()
        elif kind == "row":
            self._add_row(ev[1])
            self.pass_done += 1
            self.c_wait.set(max(0, self.pass_total - self.pass_done))
            self._update_progress()
        elif kind == "pass_done":
            n = self.pass_total
            self.bar.set(1)
            self.l_pct.configure(text="100%")
            self.l_current.configure(text=f"Pass finished: {n} scan(s) processed ✔")
        elif kind == "waiting":
            self._set_pill(f"Watching  ·  next check in {ev[1]}s", GREEN)
            if self.pass_total == 0:
                self.l_current.configure(text="Watching the folder for new scans…")
            self.l_eta.configure(text="Waiting for new scans. New files are picked up automatically.")
        elif kind == "fatal":
            self._log(ev[1], "err")
            messagebox.showerror(APP_NAME, f"The run stopped because of an error:\n\n{ev[1]}", parent=self)
        elif kind == "stopped":
            self._on_stopped()

    def _update_progress(self) -> None:
        n, d = self.pass_total, self.pass_done
        if not n:
            return
        self.bar.set(d / n)
        self.l_pct.configure(text=f"{d / n:.0%}")
        elapsed = time.time() - self.pass_started
        if d:
            per = elapsed / d
            left = per * (n - d)
            mins, secs = divmod(int(left), 60)
            eta = f"{mins} min {secs:02d} s left" if mins else f"{secs} s left"
            self.l_eta.configure(text=f"{d} of {n} done   ·   {per:.1f} s per file   ·   about {eta}")
        else:
            self.l_eta.configure(text=f"0 of {n} done   ·   estimating time…")

    # ---------------- results
    def _row_path(self, row: dict) -> Path:
        folder = Path(row["_folder"])
        if row.get("status") == "renamed":
            return folder / row["new_name"]
        if row.get("status") == "needs_review" and row.get("_review"):
            moved = Path(row["_review"]) / row["file"]
            if moved.exists():
                return moved
        return folder / row["file"]

    def _add_row(self, row: dict) -> None:
        row["_time"] = datetime.now().strftime("%H:%M:%S")
        row["_id"] = str(len(self.rows))
        self.rows.append(row)
        if self._matches(row):
            self._insert(row, at_top=True)
        self._refresh_stats()

    def _matches(self, row: dict) -> bool:
        allowed = FILTERS[self.v_filter.get()]
        if allowed is not None and row.get("status") not in allowed:
            return False
        q = self.search.get().strip().lower()
        if q:
            hay = " ".join(str(row.get(k, "")) for k in ("file", "new_name", "order", "invoice")).lower()
            return q in hay
        return True

    def _insert(self, row: dict, at_top: bool = False) -> None:
        label = STATUS.get(row.get("status"), (row.get("status", ""), GREY))[0]
        stripe = "odd" if int(row["_id"]) % 2 else "even"
        values = (row["_time"], row.get("file", ""), row.get("new_name", "") or "—",
                  row.get("order") or "", row.get("invoice") or "", row.get("order_type") or "",
                  label, row.get("seconds", ""))
        self.tree.insert("", 0 if at_top else "end", iid=row["_id"], values=values,
                         tags=(stripe, row.get("status", "")))

    def _refill_tree(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for row in reversed(self.rows):
            if self._matches(row):
                self._insert(row)

    def _refresh_stats(self) -> None:
        cnt = Counter(r.get("status") for r in self.rows)
        dry = bool(self.v_dry.get())
        self.c_done.set(cnt["renamed"] + cnt["would_rename"],
                        "✔  Would rename" if dry and not cnt["renamed"] else "✔  Renamed")
        self.c_review.set(cnt["needs_review"])
        self.c_err.set(cnt["error"])
        secs = [r["seconds"] for r in self.rows if isinstance(r.get("seconds"), (int, float))]
        self.c_speed.set(f"{sum(secs) / len(secs):.1f}" if secs else "–")
        self.undo_btn.configure(state="normal" if cnt["renamed"] else "disabled")

    def _count_waiting(self) -> None:
        if self.running:
            return
        folder = Path(self.v_folder.get().strip())
        try:
            args = SimpleNamespace(prefix=self.v_prefix.get().strip(), include_renamed=False, settle=0)
            n = len(core.pending_files(folder, args, set())) if folder.is_dir() else 0
        except OSError:
            n = 0
        self.c_wait.set(n)
        if not self.rows and folder.is_dir():
            self.l_current.configure(text=f"{n} scan(s) waiting in the folder. Press Start." if n
                                     else "No new scans in the folder.")

    def _clear(self) -> None:
        if self.rows and not messagebox.askyesno(
                APP_NAME, "Clear the results list? Files and the log are not affected.", parent=self):
            return
        self.rows.clear()
        self._refill_tree()
        self._refresh_stats()
        self.selected = None
        self._show_details(None)

    # ---------------- selection & preview
    def _on_select(self, _=None) -> None:
        sel = self.tree.selection()
        self.selected = self.rows[int(sel[0])] if sel else None
        self._show_details(self.selected)

    def _show_details(self, row: dict | None) -> None:
        for k, lbl in self.d.items():
            if row is None:
                lbl.configure(text="–", text_color=("gray10", "gray90"))
            elif k == "status":
                label, colour = STATUS.get(row.get("status"), (row.get("status", ""), GREY))
                lbl.configure(text=label, text_color=colour)
            else:
                lbl.configure(text=row.get(k) or "–", text_color=("gray10", "gray90"))
        self.d_detail.configure(text=(row or {}).get("detail", "") or "")
        if row is None:
            self.name_lbl.configure(text="Select a row to see the scan")
        else:
            new = row.get("new_name")
            self.name_lbl.configure(text=f"{row['file']}" + (f"\n→  {new}" if new else ""))
        self._show_preview()

    def _schedule_preview(self) -> None:
        if getattr(self, "_pv_job", None):
            self.after_cancel(self._pv_job)
        self._pv_job = self.after(150, self._show_preview)

    def _show_preview(self) -> None:
        self._pv_job = None
        row = self.selected
        if row is None:
            self._clear_image("🖼", ctk.CTkFont("Segoe UI", 54))
            return
        path = self._row_path(row)
        try:
            with open_page(path) as im:
                im = ImageOps.exif_transpose(im).convert("RGB")
                if self.v_view.get() == "Header":
                    w, h = im.size
                    x0, y0, x1, y1 = PREVIEW_HEADER
                    im = im.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))
                scale = ctk.ScalingTracker.get_widget_scaling(self)
                box_w = max(100, int(self.img_lbl.winfo_width() / scale) - 16)
                box_h = max(100, int(self.img_lbl.winfo_height() / scale) - 16)
                r = min(box_w / im.width, box_h / im.height)
                size = (max(1, int(im.width * r)), max(1, int(im.height * r)))
                im = im.resize((size[0] * 2, size[1] * 2), Image.LANCZOS)  # sharp on high-DPI
                self.preview_img = ctk.CTkImage(light_image=im, dark_image=im, size=size)
            self.img_lbl.configure(image=self.preview_img, text="")
        except (OSError, ValueError):
            self._clear_image("File not found\n(moved or renamed since)", self.f_body)

    def _clear_image(self, text: str, font) -> None:
        self.preview_img = None
        self.img_lbl.configure(image=None, text=text, font=font)
        self.img_lbl._label.configure(image="")  # CTkLabel keeps the old picture otherwise

    def _open_selected(self) -> None:
        if self.selected:
            p = self._row_path(self.selected)
            if p.exists():
                os.startfile(p)

    def _reveal_selected(self) -> None:
        if self.selected:
            p = self._row_path(self.selected)
            if p.exists():
                subprocess.Popen(["explorer", "/select,", str(p)])

    def _open_folder(self) -> None:
        p = Path(self.v_folder.get().strip())
        if p.is_dir():
            os.startfile(p)

    def _open_log(self) -> None:
        p = Path(self.v_log.get().strip() or DEFAULTS["log_dir"]) / "rename_log.csv"
        if p.exists():
            os.startfile(p)
        else:
            messagebox.showinfo(APP_NAME, f"No log yet at\n{p}", parent=self)

    # ---------------- undo
    def _undo_session(self) -> None:
        if self.running:
            messagebox.showinfo(APP_NAME, "Stop the run first, then undo.", parent=self)
            return
        todo = [r for r in self.rows if r.get("status") == "renamed"]
        if not todo:
            return
        if not messagebox.askyesno(
                APP_NAME, f"Rename {len(todo)} file(s) back to their original scanner names?",
                parent=self):
            return
        done, failed = 0, []
        log_dir = Path(self.v_log.get().strip() or DEFAULTS["log_dir"])
        for r in reversed(todo):
            folder = Path(r["_folder"])
            src, dst = folder / r["new_name"], folder / r["file"]
            if not src.exists() or dst.exists():
                failed.append(r["file"])
                continue
            try:
                src.rename(dst)
            except OSError:
                failed.append(r["file"])
                continue
            core.write_log(log_dir, [dict(file=r["new_name"], new_name=r["file"], order=r.get("order"),
                                          invoice=r.get("invoice"), order_type=r.get("order_type"),
                                          status="undone", detail="undo from app")])
            r["status"] = "undone"
            done += 1
        self._log(f"Undo: {done} file(s) restored" + (f", {len(failed)} skipped" if failed else ""),
                  "warn" if failed else "info")
        self._refill_tree()
        self._refresh_stats()
        self._count_waiting()
        msg = f"{done} file(s) restored to their original names."
        if failed:
            msg += f"\n\n{len(failed)} skipped (file missing or name taken):\n" + "\n".join(failed[:10])
        messagebox.showinfo(APP_NAME, msg, parent=self)

    # ---------------- close
    def _on_close(self) -> None:
        if self.running:
            if not messagebox.askyesno(APP_NAME, "A run is in progress. Stop it and exit?", parent=self):
                return
            self.worker.stop_event.set()
        cfg = self._collect_quiet()
        self.settings.update(cfg)
        save_settings(self.settings)
        self.destroy()

    def _collect_quiet(self) -> dict:
        out = {}
        for key, var in [("folder", self.v_folder), ("log_dir", self.v_log), ("review_dir", self.v_review),
                         ("mode", self.v_mode), ("prefix", self.v_prefix), ("types", self.v_types),
                         ("appearance", self.v_theme)]:
            out[key] = var.get().strip()
        out["dry_run"] = bool(self.v_dry.get())
        for key, var, cast in [("interval", self.v_interval, int), ("ms_above", self.v_ms, int),
                               ("settle", self.v_settle, float)]:
            try:
                out[key] = cast(float(var.get()))
            except ValueError:
                pass
        return out


def main() -> None:
    if len(sys.argv) > 1:
        # Any arguments: run as the command-line renamer (same options as invoice_renamer.py),
        # e.g. from Task Scheduler:  InvoiceRenamer.exe "D:\Scans" --watch 30
        sys.exit(core.main(sys.argv[1:]))
    try:  # crisp text on high-DPI screens
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    App().mainloop()


if __name__ == "__main__":
    main()
