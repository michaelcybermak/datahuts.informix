"""Invoice Extractor (v2) - desktop app.

Templates page: open a sample invoice, drag boxes over the header values you want and over
each table column, name them, test, save. One template per supplier layout.
Run page: pick the scans folder and a CSV folder; every invoice becomes one CSV
(one row per table line, header values repeated).

    python extractor_gui.py        (or run InvoiceExtractor.exe)
"""

from __future__ import annotations

import json
import logging
import os
import queue
import shutil
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

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import customtkinter as ctk
from PIL import Image, ImageTk

import extractor as ex
from invoice_renamer_gui import AMBER, BLUE, GREEN, GREY, PURPLE, RED, StatCard, resource

APP_NAME = "Invoice Extractor"
VERSION = "2.0"
DEVELOPER = "CyberMAK 2026"
APP_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "InvoiceExtractor"
SETTINGS_FILE = APP_DIR / "settings.json"
DEFAULTS = dict(folder=r"D:\Work\AAW\New Samples", out_dir=r"C:\RPA\InvoiceCSV", log_dir=r"C:\RPA",
                templates=str(APP_DIR / "templates"), prefix="", mode="Watch", interval=30,
                settle=5, appearance="Dark")
STATUS = {
    "extracted": ("Extracted", GREEN),
    "preview": ("Preview", BLUE),
    "needs_review": ("Needs review", AMBER),
    "no_template": ("No template", PURPLE),
    "error": ("Error", RED),
}
FIELD_COLOUR, COLUMN_COLOUR, SELECT_COLOUR = "#3B8ED0", "#2FA572", "#F5A623"


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


def seed_templates(folder: Path) -> None:
    """First start: copy the bundled starter templates (e.g. AAW) into the templates folder."""
    try:
        if folder.exists() and any(folder.glob("*.json")):
            return
        folder.mkdir(parents=True, exist_ok=True)
        for p in resource("templates").glob("*.json"):
            shutil.copy2(p, folder / p.name)
    except OSError:
        pass


def style_tree(widget: tk.Misc, style_name: str) -> None:
    dark = ctk.get_appearance_mode() == "Dark"
    scale = ctk.ScalingTracker.get_widget_scaling(widget)
    bg, fg, head = ("#1d2026", "#e4e6ea", "#2a2f37") if dark else ("#ffffff", "#1c1f24", "#e8ecf1")
    st = ttk.Style(widget)
    st.theme_use("clam")
    st.configure(style_name, background=bg, fieldbackground=bg, foreground=fg,
                 rowheight=int(28 * scale), borderwidth=0, font=("Segoe UI", 10))
    st.configure(f"{style_name}.Heading", background=head, foreground=fg, relief="flat",
                 font=("Segoe UI Semibold", 10), padding=(6, 5))
    st.map(style_name, background=[("selected", "#1f6aa5")], foreground=[("selected", "#ffffff")])
    st.map(f"{style_name}.Heading", background=[("active", head)])
    st.layout(style_name, [(f"{style_name}.treearea", {"sticky": "nswe"})])


def make_tree(parent, columns: list[tuple[str, str, int]], style: str) -> tuple[ttk.Treeview, tk.Misc]:
    """Treeview with a scrollbar in a frame; columns = [(id, heading, width)]."""
    frame = ctk.CTkFrame(parent, fg_color="transparent")
    frame.grid_columnconfigure(0, weight=1)
    frame.grid_rowconfigure(0, weight=1)
    tree = ttk.Treeview(frame, columns=[c[0] for c in columns], show="headings", style=style,
                        selectmode="browse")
    for cid, head, width in columns:
        tree.heading(cid, text=head, anchor="w")
        tree.column(cid, width=width, minwidth=40, anchor="w", stretch=True)
    tree.grid(row=0, column=0, sticky="nsew")
    sb = ctk.CTkScrollbar(frame, command=tree.yview)
    sb.grid(row=0, column=1, sticky="ns")
    tree.configure(yscrollcommand=sb.set)
    xsb = ctk.CTkScrollbar(frame, orientation="horizontal", command=tree.xview)
    xsb.grid(row=1, column=0, sticky="ew")
    tree.configure(xscrollcommand=xsb.set)
    return tree, frame


class QueueLogHandler(logging.Handler):
    def __init__(self, events: queue.Queue):
        super().__init__()
        self.events = events

    def emit(self, record):
        self.events.put(("log", record.levelno, record.getMessage()))


# =========================================================================== worker
class Worker(threading.Thread):
    def __init__(self, cfg: SimpleNamespace, events: queue.Queue, cache: dict):
        super().__init__(daemon=True)
        self.cfg, self.events, self.cache = cfg, events, cache
        self.stop_event = threading.Event()

    def emit(self, *e):
        self.events.put(e)

    def run(self):
        try:
            self._run()
        except Exception as e:
            ex.log.exception("Run failed")
            self.emit("fatal", f"{type(e).__name__}: {e}")
        finally:
            self.emit("stopped")

    def _run(self):
        c = self.cfg
        templates = ex.load_templates(Path(c.templates))
        if not templates:
            self.emit("fatal", f"No templates found in\n{c.templates}\n\nCreate one on the Templates page.")
            return
        if "ocr" not in self.cache:
            self.emit("status", "Loading OCR engine…")
            self.cache["ocr"] = ex.OcrEngine()
        ocr = self.cache["ocr"]
        folder, out_dir = Path(c.folder), Path(c.out_dir)
        skip: set[Path] = set()
        while not self.stop_event.is_set():
            self.emit("status", "Scanning folder…")
            todo = [p for p in ex.pending_files(folder, out_dir, c.prefix, c.settle) if p not in skip]
            if todo:
                self.emit("pass_start", len(todo))
                for i, path in enumerate(todo, 1):
                    if self.stop_event.is_set():
                        break
                    self.emit("file_start", path.name, i, len(todo))
                    try:
                        row = ex.process_file(path, templates, ocr, out_dir)
                    except Exception as e:  # unreadable file, locked CSV, ...
                        row = dict(file=path.name, status="error", detail=str(e), rows=0)
                    res = row.pop("result", None)
                    if row["status"] in ("error", "no_template"):
                        skip.add(path)  # no CSV written; try again only after a restart
                    ex.write_log(Path(c.log_dir), row)
                    self.emit("row", dict(row, _path=str(path), _csv=str(out_dir / row["csv"])
                                          if row.get("csv") else ""), res)
                self.emit("pass_done")
            if c.mode != "Watch":
                break
            end = time.time() + c.interval
            while not self.stop_event.is_set() and (left := end - time.time()) > 0:
                self.emit("waiting", int(left + 0.999))
                self.stop_event.wait(min(1.0, left))


# =========================================================================== template editor
class TemplateEditor(ctk.CTkFrame):
    """Draw fields and table columns on a sample invoice."""

    def __init__(self, master, app: "App"):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.template: ex.Template | None = None
        self.page: ex.Page | None = None
        self.sel: ex.Region | None = None
        self.dirty = False
        self.zoom = 1.0
        self.photo = None
        self.drag = None  # (mode, start_x, start_y, original_box)
        self.loading = False
        self.pages: dict[int, ex.Page] = {}  # sample pages read so far
        self.page_index = 0
        self.page_total = 0
        self.page_path = ""

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_list()
        self._build_canvas()
        self._build_props()
        self.refresh_list()

    # ---------------- layout
    def _build_list(self):
        f = ctk.CTkFrame(self, width=230, corner_radius=14)
        f.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        f.grid_propagate(False)
        f.grid_columnconfigure(0, weight=1)
        f.grid_rowconfigure(1, weight=1)
        ctk.CTkLabel(f, text="Templates", font=self.app.f_h2).grid(row=0, column=0, sticky="w",
                                                                    padx=14, pady=(14, 6))
        self.tlist = tk.Listbox(f, activestyle="none", borderwidth=0, highlightthickness=0,
                                font=("Segoe UI", 11), exportselection=False)
        self.tlist.grid(row=1, column=0, sticky="nsew", padx=10)
        self.tlist.bind("<<ListboxSelect>>", lambda _: self._pick_template())
        b = ctk.CTkFrame(f, fg_color="transparent")
        b.grid(row=2, column=0, sticky="ew", padx=10, pady=10)
        b.grid_columnconfigure((0, 1), weight=1)
        ctk.CTkButton(b, text="＋ New", height=32, command=self.new_template).grid(
            row=0, column=0, sticky="ew", padx=(0, 4))
        ctk.CTkButton(b, text="Duplicate", height=32, command=self.duplicate).grid(
            row=0, column=1, sticky="ew", padx=(4, 0))
        ctk.CTkButton(b, text="Delete", height=32, fg_color=RED, hover_color="#b84a4a",
                      command=self.delete_template).grid(row=1, column=0, columnspan=2,
                                                         sticky="ew", pady=(8, 0))
        self._style_listbox()

    def _style_listbox(self):
        dark = ctk.get_appearance_mode() == "Dark"
        self.tlist.configure(bg="#2b2b2b" if dark else "#ebebeb", fg="#e4e6ea" if dark else "#1c1f24",
                             selectbackground="#1f6aa5", selectforeground="#ffffff")

    def _build_canvas(self):
        f = ctk.CTkFrame(self, corner_radius=14)
        f.grid(row=0, column=1, sticky="nsew")
        f.grid_columnconfigure(0, weight=1)
        f.grid_rowconfigure(1, weight=1)
        bar = ctk.CTkFrame(f, fg_color="transparent")
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", padx=12, pady=(12, 8))
        ctk.CTkButton(bar, text="Open sample invoice…", height=32, command=self.open_sample).pack(side="left")
        ctk.CTkLabel(bar, text="  Draw:", font=self.app.f_body).pack(side="left", padx=(12, 4))
        self.v_draw = tk.StringVar(value="Field")
        ctk.CTkSegmentedButton(bar, values=["Field", "Table column"], variable=self.v_draw,
                               height=32).pack(side="left")
        nav = ctk.CTkFrame(bar, fg_color="transparent")
        nav.pack(side="left", padx=(12, 0))
        ctk.CTkButton(nav, text="◀", width=30, height=32, command=lambda: self.goto_page(-1)).pack(side="left")
        self.l_pageno = ctk.CTkLabel(nav, text="Page –", width=78, font=self.app.f_body)
        self.l_pageno.pack(side="left")
        ctk.CTkButton(nav, text="▶", width=30, height=32, command=lambda: self.goto_page(1)).pack(side="left")
        self.v_words = tk.BooleanVar(value=True)
        ctk.CTkSwitch(bar, text="Show text", variable=self.v_words, font=self.app.f_body,
                      command=self.redraw).pack(side="left", padx=14)
        for txt, factor in (("+", 1.25), ("Fit", 0), ("−", 1 / 1.25)):
            ctk.CTkButton(bar, text=txt, width=36, height=32,
                          command=lambda k=factor: self.set_zoom(k)).pack(side="right", padx=(4, 0))
        self.canvas = tk.Canvas(f, highlightthickness=0, cursor="crosshair")
        self.canvas.grid(row=1, column=0, sticky="nsew", padx=(12, 0))
        ys = ctk.CTkScrollbar(f, command=self.canvas.yview)
        ys.grid(row=1, column=1, sticky="ns", padx=(0, 8))
        xs = ctk.CTkScrollbar(f, orientation="horizontal", command=self.canvas.xview)
        xs.grid(row=2, column=0, sticky="ew", padx=12)
        self.canvas.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
        self.hint = ctk.CTkLabel(f, text="", font=self.app.f_small, anchor="w",
                                 text_color=("gray35", "gray65"))
        self.hint.grid(row=3, column=0, columnspan=2, sticky="ew", padx=14, pady=(4, 10))
        self.canvas.bind("<ButtonPress-1>", self._press)
        self.canvas.bind("<B1-Motion>", self._motion)
        self.canvas.bind("<ButtonRelease-1>", self._release)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Configure>", lambda _: self.page and self.zoom_fit_pending())
        self.canvas.bind("<Delete>", lambda _: self.delete_region())
        self._style_canvas()
        self._set_hint()

    def _style_canvas(self):
        self.canvas.configure(bg="#1a1c20" if ctk.get_appearance_mode() == "Dark" else "#d9dde3")

    def _build_props(self):
        f = ctk.CTkScrollableFrame(self, width=330, corner_radius=14)
        f.grid(row=0, column=2, sticky="nsew", padx=(12, 0))
        f.grid_columnconfigure(0, weight=1)
        self.props = f
        P = dict(fill="x", padx=10)

        def label(text, top=10):
            ctk.CTkLabel(f, text=text, font=self.app.f_body, anchor="w").pack(**P, pady=(top, 2))

        def hint(text):
            ctk.CTkLabel(f, text=text, font=ctk.CTkFont("Segoe UI", 11), anchor="w", justify="left",
                         wraplength=300, text_color=("gray45", "gray55")).pack(**P)

        ctk.CTkLabel(f, text="Template", font=self.app.f_h2, anchor="w").pack(**P, pady=(6, 0))
        label("Name")
        self.e_name = ctk.CTkEntry(f, height=32)
        self.e_name.pack(**P)
        label("Identify words")
        self.e_ident = ctk.CTkEntry(f, height=32)
        self.e_ident.pack(**P)
        hint("Comma separated text printed on this supplier's invoices, e.g. the company "
             "name. The template with most matches is used.")
        label("Table ends at")
        self.e_stop = ctk.CTkEntry(f, height=32)
        self.e_stop.pack(**P)
        hint("Comma separated words that mark the end of the table, e.g. Total.")
        self.v_merge = tk.BooleanVar(value=True)
        ctk.CTkSwitch(f, text="Join wrapped lines to the row above", variable=self.v_merge,
                      font=self.app.f_body).pack(**P, pady=(10, 0))
        self.v_to_end = tk.BooleanVar(value=True)
        ctk.CTkSwitch(f, text="Table runs to the end of each page", variable=self.v_to_end,
                      font=self.app.f_body, command=self._mark_dirty).pack(**P, pady=(8, 0))
        hint("On: lines are read down the whole page until a 'Table ends at' word, so longer "
             "invoices than the sample are complete. Off: only as far down as the columns are drawn.")
        label("Check each line: quantity × price = total", top=14)
        chk = ctk.CTkFrame(f, fg_color="transparent")
        chk.pack(**P)
        self.v_chk = {}
        self.chk_menus = {}
        for i, (key, title) in enumerate((("qty", "Quantity"), ("price", "Price"), ("total", "Total"))):
            chk.grid_columnconfigure(i, weight=1)
            ctk.CTkLabel(chk, text=title, font=ctk.CTkFont("Segoe UI", 11),
                         text_color=("gray40", "gray60")).grid(row=0, column=i, sticky="w", padx=2)
            self.v_chk[key] = tk.StringVar(value="—")
            m = ctk.CTkOptionMenu(chk, values=["—"], variable=self.v_chk[key], height=28, width=96,
                                  dynamic_resizing=False, command=lambda _: self._mark_dirty())
            m.grid(row=1, column=i, sticky="ew", padx=2)
            self.chk_menus[key] = m
        hint("Optional. Lines that don't add up go to review; a clearly misread quantity "
             "(e.g. 61 for 6|) is corrected from total ÷ price and noted in the log.")
        for e in (self.e_name, self.e_ident, self.e_stop):
            e.bind("<KeyRelease>", lambda _: self._mark_dirty())

        ctk.CTkLabel(f, text="Fields and columns", font=self.app.f_h2, anchor="w").pack(**P, pady=(18, 4))
        self.rtree, rframe = make_tree(f, [("name", "Name", 120), ("kind", "Kind", 60),
                                           ("value", "Value on sample", 140)], "Reg.Treeview")
        rframe.configure(height=220)
        rframe.pack(**P)
        rframe.grid_propagate(False)
        self.rtree.configure(height=7)
        self.rtree.bind("<<TreeviewSelect>>", self._pick_region)

        self.reg_box = ctk.CTkFrame(f, corner_radius=10, fg_color=("gray88", "gray17"))
        self.reg_box.pack(**P, pady=(10, 0))
        R = dict(fill="x", padx=10)
        ctk.CTkLabel(self.reg_box, text="Selected", font=self.app.f_body, anchor="w").pack(**R, pady=(8, 2))
        self.e_rname = ctk.CTkEntry(self.reg_box, height=30, placeholder_text="name, e.g. invoice_number")
        self.e_rname.pack(**R)
        self.e_rname.bind("<KeyRelease>", lambda _: self._region_changed())
        self.v_rkind = tk.StringVar(value="Field")
        ctk.CTkSegmentedButton(self.reg_box, values=["Field", "Table column"], variable=self.v_rkind,
                               height=30, command=lambda _: self._kind_changed()).pack(**R, pady=(8, 0))
        row = ctk.CTkFrame(self.reg_box, fg_color="transparent")
        row.pack(**R, pady=(8, 0))
        self.v_rtype = tk.StringVar(value="text")
        ctk.CTkOptionMenu(row, values=ex.FIELD_TYPES, variable=self.v_rtype, width=110, height=30,
                          command=lambda _: self._region_changed()).pack(side="left")
        self.v_rreq = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(row, text="Required", variable=self.v_rreq, font=self.app.f_body,
                        command=self._region_changed).pack(side="left", padx=12)
        self.field_opts = ctk.CTkFrame(self.reg_box, fg_color="transparent")
        self.field_opts.pack(**R)
        ctk.CTkLabel(self.field_opts, text="Read from", font=self.app.f_small, anchor="w").pack(
            fill="x", pady=(8, 2))
        self.v_rpage = tk.StringVar(value="First page")
        self.seg_rpage = ctk.CTkSegmentedButton(self.field_opts, values=["First page", "Last page"],
                                                variable=self.v_rpage, height=28,
                                                command=lambda _: self._region_changed())
        self.seg_rpage.pack(fill="x")
        ctk.CTkLabel(self.field_opts, text="Follows label (optional)", font=self.app.f_small,
                     anchor="w").pack(fill="x", pady=(8, 2))
        self.e_rlabel = ctk.CTkEntry(self.field_opts, height=30, placeholder_text="e.g. Total Invoice Cost:")
        self.e_rlabel.pack(fill="x")
        self.e_rlabel.bind("<KeyRelease>", lambda _: self._region_changed())
        ctk.CTkLabel(self.field_opts, text="The box moves with this printed label, for values whose "
                                           "position changes (totals after a table).",
                     font=ctk.CTkFont("Segoe UI", 11), anchor="w", justify="left", wraplength=280,
                     text_color=("gray45", "gray55")).pack(fill="x")
        ctk.CTkLabel(self.reg_box, text="Must match (optional regex)", font=self.app.f_small,
                     anchor="w").pack(**R, pady=(8, 2))
        self.e_rpat = ctk.CTkEntry(self.reg_box, height=30, placeholder_text=r"e.g. \d{5,10}")
        self.e_rpat.pack(**R)
        self.e_rpat.bind("<KeyRelease>", lambda _: self._region_changed())
        self.stretch_btn = ctk.CTkButton(self.reg_box, text="⇣  Stretch columns to table end", height=30,
                                         command=self.stretch_columns)
        self.stretch_btn.pack(**R, pady=(10, 0))
        ctk.CTkButton(self.reg_box, text="Delete this box", height=30, fg_color=RED,
                      hover_color="#b84a4a", command=self.delete_region).pack(**R, pady=10)

        ctk.CTkButton(f, text="▶  Test on sample", height=38, fg_color=PURPLE, hover_color="#7f62b8",
                      command=self.test).pack(**P, pady=(16, 6))
        self.save_btn = ctk.CTkButton(f, text="💾  Save template", height=42, fg_color=GREEN,
                                      hover_color="#258a5e", font=ctk.CTkFont("Segoe UI", 14, "bold"),
                                      command=self.save)
        self.save_btn.pack(**P, pady=(0, 12))
        self._show_region(None)

    def restyle(self):
        style_tree(self, "Reg.Treeview")
        self._style_canvas()
        self._style_listbox()
        self.redraw()

    # ---------------- template list
    @property
    def folder(self) -> Path:
        return Path(self.app.v_templates.get().strip() or DEFAULTS["templates"])

    def refresh_list(self, select: str | None = None):
        self.templates = ex.load_templates(self.folder)
        self.tlist.delete(0, "end")
        for t in self.templates:
            self.tlist.insert("end", f"  {t.name}")
        names = [t.name for t in self.templates]
        if select in names:
            i = names.index(select)
            self.tlist.selection_set(i)
            self.tlist.see(i)

    def _confirm_discard(self) -> bool:
        return not self.dirty or messagebox.askyesno(
            APP_NAME, "Discard unsaved changes to this template?", parent=self)

    def _pick_template(self):
        sel = self.tlist.curselection()
        if not sel:
            return
        t = self.templates[sel[0]]
        if self.template is not None and t.name == self.template.name:
            return
        if not self._confirm_discard():
            self.refresh_list(self.template.name if self.template else None)
            return
        self.load_template(ex.Template.from_json(t.to_json()))

    def load_template(self, t: ex.Template):
        self.template, self.sel, self.dirty = t, None, False
        self.e_name.delete(0, "end")
        self.e_name.insert(0, t.name)
        self.e_ident.delete(0, "end")
        self.e_ident.insert(0, ", ".join(t.identify))
        self.e_stop.delete(0, "end")
        self.e_stop.insert(0, ", ".join(t.stop_words))
        self.v_merge.set(t.merge_wrapped)
        self.v_to_end.set(t.to_page_end)
        for key, var in self.v_chk.items():
            var.set((t.line_check or {}).get(key) or "—")
        self.refresh_regions()
        self._show_region(None)
        if t.sample and Path(t.sample).exists():
            if self.page is None or self.page_path != t.sample:
                self._clear_page("Loading this template's sample invoice…")
                self.open_sample(t.sample)
            else:
                self.redraw()
        else:
            self._clear_page("No sample invoice is saved with this template.\n\n"
                             "Click 'Open sample invoice…' and pick one of this supplier's documents\n"
                             "to see and edit the boxes.")
        self._set_hint()

    def new_template(self, sample: str | None = None) -> bool:
        """Start a new template; with `sample`, that file is opened as its sample at once."""
        if not self._confirm_discard():
            return False
        n, names = 1, {t.name for t in self.templates}
        while f"New template {n}" in names:
            n += 1
        self.load_template(ex.Template(f"New template {n}", stop_words=["Total"]))
        self.dirty = True
        self.tlist.selection_clear(0, "end")
        if sample:
            self._clear_page(f"Reading {Path(sample).name}…")
            self.open_sample(sample)
        elif self.page is None:
            self.open_sample()
        return True

    def duplicate(self):
        if not self.template:
            return
        d = ex.Template.from_json(self.template.to_json())
        d.name = f"{d.name} copy"
        self.load_template(d)
        self.dirty = True
        self.tlist.selection_clear(0, "end")

    def delete_template(self):
        if not self.template:
            return
        p = self.folder / f"{ex.safe_name(self.template.name)}.json"
        if not messagebox.askyesno(APP_NAME, f"Delete template '{self.template.name}'?", parent=self):
            return
        try:
            p.unlink(missing_ok=True)
        except OSError as e:
            messagebox.showerror(APP_NAME, str(e), parent=self)
            return
        self.template, self.dirty = None, False
        self.refresh_list()
        self.refresh_regions()
        self.redraw()
        self._set_hint()

    # ---------------- sample page
    def _clear_page(self, message: str):
        """Forget the sample on screen (it belongs to another template) and show a message."""
        self._load_seq = getattr(self, "_load_seq", 0) + 1  # a load still running is now stale
        self.loading = False
        self.page, self.photo, self.pages = None, None, {}
        self.page_index = self.page_total = 0
        self.page_path = ""
        self._canvas_msg = message
        self._show_pageno()
        self.redraw()

    def open_sample(self, path: str | None = None):
        if path is None:
            path = filedialog.askopenfilename(
                parent=self, title="Open a sample invoice",
                initialdir=self.app.v_folder.get() or None,
                filetypes=[("Invoices", "*.jpg *.jpeg *.png *.tif *.tiff *.bmp *.webp *.pdf"),
                           ("All files", "*.*")])
            if not path:
                return
        self._load_seq = seq = getattr(self, "_load_seq", 0) + 1  # the newest request wins
        cached = self.app.cache.setdefault("samples", {}).get(path)
        if cached:  # read before: show it at once, with the other pages already read
            self._sample_loaded(path, cached[0], None, seq)
            self.pages.update(cached[1])
            return
        self.loading = True
        self.hint.configure(text="Reading the sample invoice (OCR)… this takes a few seconds.")

        def work():
            try:
                if "ocr" not in self.app.cache:
                    self.app.cache["ocr"] = ex.OcrEngine()
                page = ex.load_page(Path(path), self.app.cache["ocr"])
                page.total = ex.page_count(Path(path))
                self.after(0, lambda: self._sample_loaded(path, page, None, seq))
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                self.after(0, lambda: self._sample_loaded(path, None, err, seq))

        threading.Thread(target=work, daemon=True).start()

    def _sample_loaded(self, path, page, err, seq=None):
        if page is not None:  # keep it, even if no longer wanted, so switching back is instant
            self.app.cache.setdefault("samples", {}).setdefault(path, (page, {}))
        if seq is not None and seq != getattr(self, "_load_seq", seq):
            return  # another template or sample was chosen meanwhile
        self.loading = False
        self._canvas_msg = None
        if err:
            messagebox.showerror(APP_NAME, f"Could not read the sample:\n{err}", parent=self)
            self._set_hint()
            return
        self.page_path = path
        self.pages = {0: page}
        self.page_index, self.page_total = 0, page.total
        self.page = page
        self._show_pageno()
        if self.template is None:
            self.load_template(ex.Template("New template 1", stop_words=["Total"]))
            self.dirty = True
        if self.template.sample != path:
            self.template.sample = path
            self.dirty = True
        if not self.template.identify:
            self.e_ident.delete(0, "end")
            self.e_ident.insert(0, self._guess_identify())
        self.set_zoom(0)
        self.refresh_regions()
        self._set_hint()

    def _get_page(self, i: int) -> ex.Page:
        """Sample page i (0-based), read on first use."""
        if i not in self.pages:
            self.hint.configure(text=f"Reading page {i + 1}…")
            self.configure(cursor="watch")
            self.update_idletasks()
            try:
                first = self.pages[0]
                self.pages[i] = self.app.cache.setdefault("samples", {}).get(
                    self.page_path, (None, {}))[1].get(i) or ex.load_page(Path(self.page_path), self.app.cache.get("ocr"), index=i,
                                             rotation=first.rotation if first.source == "ocr" else None,
                                             ref_size=first.image.size)
            finally:
                self.configure(cursor="")
                self._set_hint()
        return self.pages[i]

    def _remember_pages(self):
        entry = self.app.cache.setdefault("samples", {}).get(self.page_path)
        if entry:
            entry[1].update({k: v for k, v in self.pages.items() if k})

    def show_page(self, i: int):
        if self.page is None or not 0 <= i < self.page_total:
            return
        try:
            self.page = self._get_page(i)
        except Exception as e:
            messagebox.showerror(APP_NAME, f"Could not read page {i + 1}:\n{e}", parent=self)
            return
        self.page_index = i
        self._remember_pages()
        self._show_pageno()
        self.set_zoom(0 if self._auto_fit_on() else 1)
        self.refresh_regions(keep=self.sel)

    def goto_page(self, step: int):
        self.show_page(self.page_index + step)

    def _auto_fit_on(self) -> bool:
        return getattr(self, "_auto_fit", True)

    def _show_pageno(self):
        self.l_pageno.configure(text=f"Page {self.page_index + 1} / {self.page_total}" if self.page_total
                                else "Page –")

    @property
    def last_index(self) -> int:
        return max(0, self.page_total - 1)

    def _region_page(self, r: ex.Region) -> int:
        return self.last_index if (r.kind == "field" and r.page == "last") else 0

    def _visible(self, r: ex.Region) -> bool:
        """Columns show on every page; fields on the page they are read from."""
        if r.kind == "column" or self.page_total <= 1:
            return True
        return self.page_index == self._region_page(r)

    def _guess_identify(self) -> str:
        """Suggest the longest text line near the top of the page (usually the company name)."""
        top = [w for w in self.page.words if w.y0 < 0.15 and sum(c.isalpha() for c in w.text) >= 6]
        top.sort(key=lambda w: -len(w.text))
        return ", ".join(w.text for w in top[:1])

    # ---------------- zoom & drawing
    def zoom_fit_pending(self):
        if getattr(self, "_fit_job", None) is None and getattr(self, "_auto_fit", True):
            self._fit_job = self.after(120, lambda: (setattr(self, "_fit_job", None), self.set_zoom(0)))

    def set_zoom(self, factor: float):
        if self.page is None:
            return
        iw, ih = self.page.image.size
        if factor == 0:
            avail = max(200, self.canvas.winfo_width() - 8)
            self.zoom = avail / iw
            self._auto_fit = True
        else:
            self.zoom = min(3.0, max(0.1, self.zoom * factor))
            self._auto_fit = False
        w, h = int(iw * self.zoom), int(ih * self.zoom)
        self.photo = ImageTk.PhotoImage(self.page.image.resize((max(1, w), max(1, h)), Image.LANCZOS))
        self.canvas.configure(scrollregion=(0, 0, w, h))
        self.redraw()

    def _wheel(self, e):
        if e.state & 0x0004:  # Ctrl + wheel = zoom
            self.set_zoom(1.25 if e.delta > 0 else 1 / 1.25)
        else:
            self.canvas.yview_scroll(int(-e.delta / 120) * 3, "units")

    def _ref_size(self):
        """Positions are page-1 fractions on every page (later pages may be cut shorter)."""
        return (self.pages.get(0) or self.page).image.size

    def _to_canvas(self, box):
        iw, ih = self._ref_size()
        z = self.zoom
        return box[0] * iw * z, box[1] * ih * z, box[2] * iw * z, box[3] * ih * z

    def _to_frac(self, x, y):
        iw, ih = self._ref_size()
        return (min(1, max(0, x / (iw * self.zoom))), min(1, max(0, y / (ih * self.zoom))))

    def redraw(self):
        c = self.canvas
        c.delete("all")
        if self.page is None or self.photo is None:
            if getattr(self, "_canvas_msg", None):
                c.create_text(30, 30, anchor="nw", fill="#8a8f98", font=("Segoe UI", 13),
                              text=self._canvas_msg)
                return
            c.create_text(30, 30, anchor="nw", fill="#8a8f98", font=("Segoe UI", 13),
                          text="Open a sample invoice to start drawing.\n\n"
                               "1. Choose Field, drag a box around a header value (e.g. the invoice number).\n"
                               "2. Choose Table column, drag a box over each column of the table body.\n"
                               "3. Name each box on the right, Test, then Save.")
            return
        c.create_image(0, 0, anchor="nw", image=self.photo)
        if self.v_words.get():
            for w in self.page.words:
                x0, y0, x1, y1 = self._to_canvas((w.x0, w.y0, w.x1, w.y1))
                c.create_rectangle(x0, y0, x1, y1, outline="#c9a227", dash=(2, 3), width=1)
        if self.template:
            for r in self.template.regions:
                if self._visible(r):
                    self._draw_region(r)

    def _draw_region(self, r: ex.Region):
        x0, y0, x1, y1 = self._to_canvas(r.box)
        colour = SELECT_COLOUR if r is self.sel else (FIELD_COLOUR if r.kind == "field" else COLUMN_COLOUR)
        width = 3 if r is self.sel else 2
        self.canvas.create_rectangle(x0, y0, x1, y1, outline=colour, width=width,
                                     fill=colour, stipple="gray12")
        label = r.name or "(unnamed)"
        if r.kind == "field" and (val := self._value(r)):
            label += f"  =  {val[:30]}"
        tw = self.canvas.create_text(x0 + 3, y0 - 2, anchor="sw", text=label, fill="#ffffff",
                                     font=("Segoe UI Semibold", 9))
        bx = self.canvas.bbox(tw)
        if bx:
            bg = self.canvas.create_rectangle(bx[0] - 3, bx[1] - 1, bx[2] + 3, bx[3] + 1,
                                              fill=colour, outline=colour)
            self.canvas.tag_lower(bg, tw)
        if r is self.sel:  # resize handle
            self.canvas.create_rectangle(x1 - 6, y1 - 6, x1 + 6, y1 + 6, fill=SELECT_COLOUR,
                                         outline="#ffffff")

    def _value(self, r: ex.Region) -> str:
        if self.page is None:
            return ""
        if r.kind == "field":
            page = self.pages.get(self._region_page(r))
            if page is None:
                return "(on the last page)"
            return ex.clean_value(ex._join_lines(ex._text_in(page.words, r.box)), r.type, r.pattern)
        res = ex.apply_template(self._current(anchor=False), self.page.words)
        vals = [row.get(r.name, "") for row in res.rows if row.get(r.name)]  # this page only
        return f"{len(vals)} rows, e.g. {vals[0]}" if vals else "no rows"

    # ---------------- mouse
    def _hit(self, x, y) -> ex.Region | None:
        if not self.template:
            return None
        fx, fy = self._to_frac(x, y)
        hits = [r for r in self.template.regions if self._visible(r)
                and r.box[0] <= fx <= r.box[2] and r.box[1] <= fy <= r.box[3]]
        return min(hits, key=lambda r: (r.box[2] - r.box[0]) * (r.box[3] - r.box[1])) if hits else None

    def _press(self, e):
        self.canvas.focus_set()
        if self.page is None:
            self.open_sample()
            return
        if self.template is None:
            self.load_template(ex.Template("New template 1", stop_words=["Total"]))
            self.dirty = True
        x, y = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        if self.sel:
            sx0, sy0, sx1, sy1 = self._to_canvas(self.sel.box)
            if abs(x - sx1) < 9 and abs(y - sy1) < 9:
                self.drag = ("resize", x, y, list(self.sel.box))
                return
        hit = self._hit(x, y)
        if hit:
            self._select(hit)
            self.drag = ("move", x, y, list(hit.box))
            return
        self.drag = ("draw", x, y, None)
        self._rubber = self.canvas.create_rectangle(x, y, x, y, outline=SELECT_COLOUR, width=2, dash=(4, 2))

    def _motion(self, e):
        if not self.drag:
            return
        mode, sx, sy, orig = self.drag
        x, y = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        if mode == "draw":
            self.canvas.coords(self._rubber, sx, sy, x, y)
            return
        iw, ih = self.page.image.size
        dx, dy = (x - sx) / (iw * self.zoom), (y - sy) / (ih * self.zoom)
        b = self.sel.box
        if mode == "move":
            w, h = orig[2] - orig[0], orig[3] - orig[1]
            b[0] = min(1 - w, max(0, orig[0] + dx))
            b[1] = min(1 - h, max(0, orig[1] + dy))
            b[2], b[3] = b[0] + w, b[1] + h
        else:
            b[2] = min(1, max(orig[0] + 0.005, orig[2] + dx))
            b[3] = min(1, max(orig[1] + 0.003, orig[3] + dy))
        if self.sel.kind == "column":
            self._sync_columns(self.sel)
        self.redraw()

    def _release(self, e):
        if not self.drag:
            return
        mode, sx, sy, orig = self.drag
        self.drag = None
        x, y = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        if mode != "draw":
            if orig != self.sel.box:
                self._mark_dirty()
                self.refresh_regions(keep=self.sel)
            return
        self.canvas.delete(self._rubber)
        if abs(x - sx) < 6 and abs(y - sy) < 6:  # a click: box around the word under the mouse
            fx, fy = self._to_frac(x, y)
            word = next((w for w in self.page.words if w.x0 <= fx <= w.x1 and w.y0 <= fy <= w.y1), None)
            if not word:
                self._select(None)
                return
            pad_x, pad_y = 0.004, 0.003
            box = [word.x0 - pad_x, word.y0 - pad_y, word.x1 + pad_x, word.y1 + pad_y]
        else:
            (fx0, fy0), (fx1, fy1) = self._to_frac(sx, sy), self._to_frac(x, y)
            box = [min(fx0, fx1), min(fy0, fy1), max(fx0, fx1), max(fy0, fy1)]
        self._add_region(box)

    def _add_region(self, box):
        kind = "column" if self.v_draw.get() == "Table column" else "field"
        if kind == "field" and self.page_total > 1 and 0 < self.page_index < self.last_index:
            messagebox.showinfo(APP_NAME, "Fields are read from the first or the last page. "
                                          "Go to one of those to draw this field.", parent=self)
            return
        names = {r.name for r in self.template.regions}
        base = "column" if kind == "column" else "field"
        n = 1
        while f"{base}_{n}" in names:
            n += 1
        r = ex.Region(f"{base}_{n}", kind, [round(v, 4) for v in box])
        if kind == "column":
            self._fit_new_column(r)
        elif self.page_total > 1 and self.page_index == self.last_index:
            r.page = "last"
            r.label = self._label_left_of(r.box)  # totals move with the table length
        self.template.regions.append(r)
        self._mark_dirty()
        self.refresh_regions(keep=r)
        self._select(r)
        self.e_rname.focus_set()
        self.e_rname.select_range(0, "end")

    def _label_left_of(self, box) -> str:
        """The text just left of a box on the same line, e.g. 'Total Invoice Cost:'."""
        cy = (box[1] + box[3]) / 2
        left = [w for w in self.page.words if abs(w.cy - cy) < 0.6 * max(w.h, 0.005)
                and w.x1 <= box[0] + 0.005 and box[0] - w.x1 < 0.3 and any(c.isalpha() for c in w.text)]
        return max(left, key=lambda w: w.x1).text if left else ""

    def _sync_columns(self, src: ex.Region):
        for c in self.template.regions:
            if c.kind == "column" and c is not src:
                c.box[1], c.box[3] = src.box[1], src.box[3]

    # ---------------- regions panel
    def refresh_regions(self, keep: ex.Region | None = None):
        self.rtree.delete(*self.rtree.get_children())
        cols = ["—"] + [c.name for c in self.template.columns] if self.template else ["—"]
        for key, m in self.chk_menus.items():
            m.configure(values=cols)
            if self.v_chk[key].get() not in cols:
                self.v_chk[key].set("—")
        if not self.template:
            return
        for i, r in enumerate(self.template.regions):
            self.rtree.insert("", "end", iid=str(i),
                              values=(r.name, "Field" if r.kind == "field" else "Column", self._value(r)))
        if keep is not None and keep in self.template.regions:
            iid = str(self.template.regions.index(keep))
            self.rtree.selection_set(iid)
            self.rtree.see(iid)
        self.redraw()

    def _pick_region(self, _=None):
        sel = self.rtree.selection()
        if sel and self.template:
            r = self.template.regions[int(sel[0])]
            if r is not self.sel:
                self._select(r, from_tree=True)

    def _select(self, r: ex.Region | None, from_tree: bool = False):
        self.sel = r
        self._show_region(r)
        if not from_tree:
            if r is not None and self.template and r in self.template.regions:
                iid = str(self.template.regions.index(r))
                self.rtree.selection_set(iid)
                self.rtree.see(iid)
            else:
                self.rtree.selection_remove(*self.rtree.selection())
        self.redraw()

    def _show_region(self, r: ex.Region | None):
        self._loading_region = True
        self.e_rname.delete(0, "end")
        self.e_rpat.delete(0, "end")
        self.e_rlabel.delete(0, "end")
        state = "normal" if r else "disabled"
        for w in [*self.reg_box.winfo_children(), self.seg_rpage, self.e_rlabel]:
            try:
                w.configure(state=state)
            except (tk.TclError, ValueError):
                pass
        if r:
            self.v_rkind.set("Field" if r.kind == "field" else "Table column")
            self.stretch_btn.configure(state="normal" if r.kind == "column" else "disabled")
            self.e_rname.insert(0, r.name)
            self.e_rpat.insert(0, r.pattern)
            self.v_rtype.set(r.type)
            self.v_rreq.set(r.required)
            self.v_rpage.set("Last page" if r.page == "last" else "First page")
            self.e_rlabel.insert(0, r.label)
            fstate = "normal" if r.kind == "field" else "disabled"
            self.seg_rpage.configure(state=fstate)
            self.e_rlabel.configure(state=fstate)
        self._loading_region = False

    def _region_changed(self):
        if self._loading_region or not self.sel:
            return
        name = self.e_rname.get().strip().replace(" ", "_")
        self.sel.name = name
        self.sel.type = self.v_rtype.get()
        self.sel.required = bool(self.v_rreq.get())
        self.sel.pattern = self.e_rpat.get().strip()
        if self.sel.kind == "field":
            new_page = "last" if self.v_rpage.get() == "Last page" else "first"
            if new_page != self.sel.page:
                self.sel.page = new_page
                if not self._visible(self.sel):  # show the page it now reads from
                    self.after(10, lambda: self.show_page(self._region_page(self.sel)))
            self.sel.label = self.e_rlabel.get().strip()
        self._mark_dirty()
        if self.template and self.sel in self.template.regions:
            iid = str(self.template.regions.index(self.sel))
            self.rtree.item(iid, values=(self.sel.name, "Field" if self.sel.kind == "field" else "Column",
                                         self._value(self.sel)))
        self.redraw()

    def _kind_changed(self):
        if self._loading_region or not self.sel:
            return
        kind = "column" if self.v_rkind.get() == "Table column" else "field"
        if kind == self.sel.kind:
            return
        self.sel.kind = kind
        if kind == "column":
            self._fit_new_column(self.sel)
        self.stretch_btn.configure(state="normal" if kind == "column" else "disabled")
        self._mark_dirty()
        self.refresh_regions(keep=self.sel)

    def _fit_new_column(self, r: ex.Region):
        """A new column takes the table's top and bottom; the first one reaches down to the
        'Table ends at' words so a box drawn over one row still covers the whole table."""
        others = [c for c in self.template.regions if c.kind == "column" and c is not r]
        if others:
            r.box[1], r.box[3] = others[0].box[1], others[0].box[3]
        elif (end := self._table_end_y(r.box[1])) is not None and end > r.box[3]:
            r.box[3] = round(end, 4)

    def _table_end_y(self, top: float) -> float | None:
        """Top of the first line below `top` containing a 'Table ends at' word, on the sample."""
        if self.page is None:
            return None
        stops = [ex._norm(x) for x in self.e_stop.get().split(",") if x.strip()]
        if not stops:
            return None
        words = self.page.words
        best = None
        for w in words:
            if w.y0 <= top + 0.005:
                continue
            line = ex._norm(" ".join(o.text for o in sorted(words, key=lambda o: o.x0)
                                     if abs(o.cy - w.cy) < 0.6 * max(w.h, 0.005)))
            if any(st in line for st in stops) and (best is None or w.y0 < best):
                best = w.y0
        return best - 0.004 if best is not None else None

    def stretch_columns(self):
        cols = [c for c in self.template.regions if c.kind == "column"] if self.template else []
        if not cols:
            return
        top = min(c.box[1] for c in cols)
        end = self._table_end_y(top)
        if end is None:
            messagebox.showinfo(APP_NAME, "None of the 'Table ends at' words were found below the table "
                                          "on this sample. Drag a column's corner down instead.", parent=self)
            return
        for c in cols:
            c.box[3] = round(end, 4)
        self._mark_dirty()
        self.refresh_regions(keep=self.sel)

    def delete_region(self):
        if self.sel and self.template and self.sel in self.template.regions:
            self.template.regions.remove(self.sel)
            self.sel = None
            self._show_region(None)
            self._mark_dirty()
            self.refresh_regions()

    # ---------------- test & save
    def _current(self, anchor: bool = True) -> ex.Template:
        t = self.template
        t.name = self.e_name.get().strip() or t.name
        t.identify = [s.strip() for s in self.e_ident.get().split(",") if s.strip()]
        t.stop_words = [s.strip() for s in self.e_stop.get().split(",") if s.strip()]
        t.merge_wrapped = bool(self.v_merge.get())
        t.to_page_end = bool(self.v_to_end.get())
        chosen = {k: v.get() for k, v in self.v_chk.items()}
        t.line_check = chosen if all(v != "—" for v in chosen.values()) else None
        if anchor and self.pages:
            first = self.pages[0].words
            t.anchor = ex.make_anchor(t, first)
            t.table_head = ex.make_table_head(t, first)
            for f in t.fields:
                if f.label:
                    ex.set_label_pos(f, self._get_page(self._region_page(f)).words)
                else:
                    f.label_pos = None
        return t

    def _all_sample_words(self) -> list[list]:
        return [self._get_page(i).words for i in range(max(1, self.page_total))]

    def _problems(self, t: ex.Template) -> list[str]:
        out = []
        names = [r.name for r in t.regions]
        if not t.regions:
            out.append("Draw at least one field or column.")
        if any(not n for n in names):
            out.append("Every box needs a name.")
        dup = {n for n in names if names.count(n) > 1 and n}
        if dup:
            out.append(f"Names must be unique: {', '.join(sorted(dup))}")
        if not t.identify:
            out.append("Add at least one identify word so the template can be recognised.")
        return out

    def test(self):
        if not self.template or self.page is None:
            messagebox.showinfo(APP_NAME, "Open a sample invoice and draw some boxes first.", parent=self)
            return
        t = self._current()
        pages = self._all_sample_words()
        res = ex.apply_template(t, pages[0], pages[1:])
        found = ex.identify(pages[0], [t])
        missing_labels = [f.name for f in t.fields if f.label and not f.label_pos]
        note = None
        if not found:
            note = ("Warning: none of the identify words were found on this sample, so real files "
                    "would not be matched to it.")
        elif missing_labels:
            note = f"Label not found on the sample for: {', '.join(missing_labels)}"
        ResultWindow(self, f"Test: {t.name}  ({len(pages)} page{'s' if len(pages) > 1 else ''})",
                     t, res, note=note)

    def save(self):
        if not self.template:
            return
        t = self._current()
        if probs := self._problems(t):
            messagebox.showwarning(APP_NAME, "\n".join(probs), parent=self)
            return
        existing = {x.name.lower(): x for x in self.templates}
        target = self.folder / f"{ex.safe_name(t.name)}.json"
        old_sel = self.tlist.curselection()
        old_name = self.templates[old_sel[0]].name if old_sel else None
        if target.exists() and old_name != t.name and t.name.lower() in existing:
            if not messagebox.askyesno(APP_NAME, f"A template named '{t.name}' exists. Replace it?",
                                       parent=self):
                return
        try:
            ex.save_template(self.folder, t)
            if old_name and old_name != t.name:  # renamed: remove the old file
                (self.folder / f"{ex.safe_name(old_name)}.json").unlink(missing_ok=True)
        except OSError as e:
            messagebox.showerror(APP_NAME, f"Could not save:\n{e}", parent=self)
            return
        self.dirty = False
        self.refresh_list(select=t.name)
        self._set_hint(f"Saved '{t.name}' to {target}")

    def _mark_dirty(self):
        self.dirty = True
        self.save_btn.configure(text="💾  Save template  •")

    def _set_hint(self, text: str | None = None):
        if not self.dirty and hasattr(self, "save_btn"):
            self.save_btn.configure(text="💾  Save template")
        if text:
            self.hint.configure(text=text)
        elif self.page is None:
            self.hint.configure(text="Open a sample invoice from the supplier this template is for.")
        else:
            self.hint.configure(text="Drag to draw a box · click a word to box it · drag a box to move it · "
                                     "drag its corner to resize · Delete removes · Ctrl+wheel zooms")


# =========================================================================== results window
class ResultWindow(ctk.CTkToplevel):
    def __init__(self, master, title: str, t: ex.Template, res: ex.Result, note: str | None = None):
        super().__init__(master)
        self.title(title)
        self.geometry("1100x620")
        self.after(150, self.lift)
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(2, weight=1)
        HeaderGrid(self, t, res).grid(row=0, column=0, sticky="ew", padx=16, pady=(16, 8))
        msg = note or ("; ".join(res.problems) if res.problems else "")
        if msg:
            ctk.CTkLabel(self, text=msg, text_color=AMBER, anchor="w", wraplength=1000,
                         justify="left").grid(row=1, column=0, sticky="ew", padx=16)
        LinesTable(self, t, res).grid(row=2, column=0, sticky="nsew", padx=16, pady=(8, 16))


class HeaderGrid(ctk.CTkFrame):
    def __init__(self, master, t: ex.Template | None, res: ex.Result | None):
        super().__init__(master, corner_radius=10, fg_color=("gray88", "gray17"))
        self.show(t, res)

    def show(self, t, res):
        for w in self.winfo_children():
            w.destroy()
        if not t or not res or not t.fields:
            ctk.CTkLabel(self, text="No header fields", text_color=("gray45", "gray55")).grid(
                row=0, column=0, padx=12, pady=10)
            return
        per_row = 4
        for i, f in enumerate(t.fields):
            r, c = divmod(i, per_row)
            self.grid_columnconfigure(c, weight=1)
            cell = ctk.CTkFrame(self, fg_color="transparent")
            cell.grid(row=r, column=c, sticky="w", padx=12, pady=6)
            ctk.CTkLabel(cell, text=f.name, font=ctk.CTkFont("Segoe UI", 11),
                         text_color=("gray40", "gray60"), anchor="w").pack(anchor="w")
            val = res.header.get(f.name, "")
            ctk.CTkLabel(cell, text=val or "— not found —", anchor="w",
                         font=ctk.CTkFont("Segoe UI", 14, "bold"),
                         text_color=(("gray10", "gray92") if val else (AMBER, AMBER))).pack(anchor="w")


class LinesTable(ctk.CTkFrame):
    def __init__(self, master, t: ex.Template | None, res: ex.Result | None):
        super().__init__(master, fg_color="transparent")
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.inner = None
        self.show(t, res)

    def show(self, t, res):
        if self.inner:
            self.inner.destroy()
        cols = [c.name for c in t.columns] if t else []
        spec = ([("#", "#", 40), ("page", "Page", 46)] + [(c, c, 110) for c in cols]) if cols \
            else [("info", "Table", 300)]
        tree, frame = make_tree(self, spec, "Lines.Treeview")
        style_tree(self, "Lines.Treeview")
        frame.grid(row=0, column=0, sticky="nsew")
        self.inner = frame
        if not cols:
            tree.insert("", "end", values=("No table columns in this template",))
        elif res and res.rows:
            for i, r in enumerate(res.rows, 1):
                tree.insert("", "end", values=(i, r.get("page", 1), *[r.get(c, "") for c in cols]))
        else:
            tree.insert("", "end", values=("", "", "No table lines found"))


# =========================================================================== run page
class RunPage(ctk.CTkFrame):
    def __init__(self, master, app: "App"):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.rows: list[dict] = []
        self.results: list = []
        self.pass_total = self.pass_done = 0
        self.pass_started = 0.0
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(3, weight=1)
        self._build_settings()
        self._build_cards()
        self._build_progress()
        self._build_body()

    def _build_settings(self):
        s = self.app.settings
        f = ctk.CTkFrame(self, corner_radius=14)
        f.grid(row=0, column=0, sticky="ew")
        for c in (1, 4):
            f.grid_columnconfigure(c, weight=1)
        self.inputs = []

        def folder(row, col, label, var):
            ctk.CTkLabel(f, text=label, font=self.app.f_body).grid(row=row, column=col, sticky="w",
                                                                   padx=(16, 8), pady=6)
            e = ctk.CTkEntry(f, textvariable=var, height=32)
            e.grid(row=row, column=col + 1, sticky="ew", pady=6)
            b = ctk.CTkButton(f, text="Browse", width=70, height=32, command=lambda: self.app.browse(var))
            b.grid(row=row, column=col + 2, padx=(6, 12), pady=6)
            self.inputs += [e, b]

        folder(0, 0, "Scans folder", self.app.v_folder)
        folder(0, 3, "CSV folder", self.app.v_out)
        folder(1, 0, "Templates folder", self.app.v_templates)
        folder(1, 3, "Log folder", self.app.v_log)
        opts = ctk.CTkFrame(f, fg_color="transparent")
        opts.grid(row=2, column=0, columnspan=6, sticky="ew", padx=16, pady=(4, 12))
        ctk.CTkLabel(opts, text="Mode", font=self.app.f_body).pack(side="left")
        self.v_mode = tk.StringVar(value=s["mode"])
        seg = ctk.CTkSegmentedButton(opts, values=["Run once", "Watch"], variable=self.v_mode, height=32)
        seg.pack(side="left", padx=(8, 16))
        ctk.CTkLabel(opts, text="every", font=self.app.f_body).pack(side="left")
        self.v_interval = tk.StringVar(value=str(s["interval"]))
        e1 = ctk.CTkEntry(opts, textvariable=self.v_interval, width=56, height=32, justify="center")
        e1.pack(side="left", padx=6)
        ctk.CTkLabel(opts, text="s", font=self.app.f_body).pack(side="left", padx=(0, 18))
        ctk.CTkLabel(opts, text="File prefix", font=self.app.f_body).pack(side="left")
        self.v_prefix = tk.StringVar(value=s["prefix"])
        e2 = ctk.CTkEntry(opts, textvariable=self.v_prefix, width=90, height=32, placeholder_text="all files")
        e2.pack(side="left", padx=8)
        self.inputs += [seg, e1, e2]
        self.start_btn = ctk.CTkButton(opts, text="▶   Start", width=170, height=40, corner_radius=12,
                                       font=ctk.CTkFont("Segoe UI", 15, "bold"), fg_color=GREEN,
                                       hover_color="#258a5e", command=self.app.start_stop)
        self.start_btn.pack(side="right")

    def _build_cards(self):
        cards = ctk.CTkFrame(self, fg_color="transparent")
        cards.grid(row=1, column=0, sticky="ew", pady=12)
        for i in range(5):
            cards.grid_columnconfigure(i, weight=1, uniform="c")
        self.c_wait = StatCard(cards, "Waiting", BLUE, "⏳")
        self.c_ok = StatCard(cards, "Extracted", GREEN, "✔")
        self.c_rev = StatCard(cards, "Needs review", AMBER, "⚠")
        self.c_none = StatCard(cards, "No template", PURPLE, "?")
        self.c_lines = StatCard(cards, "Table lines", GREY, "≡")
        for i, c in enumerate([self.c_wait, self.c_ok, self.c_rev, self.c_none, self.c_lines]):
            c.grid(row=0, column=i, sticky="ew", padx=(0 if i == 0 else 6, 0 if i == 4 else 6))

    def _build_progress(self):
        p = ctk.CTkFrame(self, corner_radius=14)
        p.grid(row=2, column=0, sticky="ew", pady=(0, 12))
        p.grid_columnconfigure(0, weight=1)
        self.l_current = ctk.CTkLabel(p, text="Ready.", font=self.app.f_h2, anchor="w")
        self.l_current.grid(row=0, column=0, sticky="ew", padx=18, pady=(12, 6))
        self.l_pct = ctk.CTkLabel(p, text="", font=self.app.f_h2)
        self.l_pct.grid(row=0, column=1, padx=18, pady=(12, 6))
        self.bar = ctk.CTkProgressBar(p, height=12, corner_radius=6, progress_color=GREEN)
        self.bar.grid(row=1, column=0, columnspan=2, sticky="ew", padx=18)
        self.bar.set(0)
        self.l_eta = ctk.CTkLabel(p, text=" ", font=self.app.f_small, anchor="w",
                                  text_color=("gray40", "gray60"))
        self.l_eta.grid(row=2, column=0, columnspan=2, sticky="ew", padx=18, pady=(4, 10))

    def _build_body(self):
        body = ctk.CTkFrame(self, fg_color="transparent")
        body.grid(row=3, column=0, sticky="nsew")
        body.grid_columnconfigure(0, weight=5, uniform="body")
        body.grid_columnconfigure(1, weight=6, uniform="body")
        body.grid_rowconfigure(0, weight=1)

        left = ctk.CTkTabview(body, corner_radius=14, anchor="nw")
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        t_files, t_log = left.add("Files"), left.add("Activity log")
        t_files.grid_columnconfigure(0, weight=1)
        t_files.grid_rowconfigure(0, weight=1)
        self.ftree, fframe = make_tree(t_files, [("time", "Time", 60), ("file", "File", 200),
                                                 ("tpl", "Template", 70), ("rows", "Lines", 46),
                                                 ("status", "Status", 100)], "Files.Treeview")
        fframe.grid(row=0, column=0, sticky="nsew")
        self.ftree.bind("<<TreeviewSelect>>", self._pick)
        self.ftree.bind("<Double-1>", lambda _: self._double())
        self.ftree.bind("<Button-3>", self._context)
        acts = ctk.CTkFrame(t_files, fg_color="transparent")
        acts.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        btn = dict(height=32, fg_color=("gray80", "gray25"), hover_color=("gray72", "gray32"),
                   text_color=("gray10", "gray90"))
        ctk.CTkButton(acts, text="Open CSV folder", command=self.open_out, **btn).pack(side="left")
        ctk.CTkButton(acts, text="Open log", command=self.open_log, **btn).pack(side="left", padx=8)
        ctk.CTkButton(acts, text="Clear list", command=self.clear, **btn).pack(side="left")

        t_log.grid_columnconfigure(0, weight=1)
        t_log.grid_rowconfigure(0, weight=1)
        self.logbox = ctk.CTkTextbox(t_log, font=ctk.CTkFont("Consolas", 12), wrap="none")
        self.logbox.grid(row=0, column=0, sticky="nsew")
        tb = self.logbox._textbox
        for tag, colour in (("time", GREY), ("ok", GREEN), ("warn", AMBER), ("err", RED), ("info", BLUE)):
            tb.tag_config(tag, foreground=colour)
        self.logbox.configure(state="disabled")

        right = ctk.CTkFrame(body, corner_radius=14)
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(2, weight=1)
        top = ctk.CTkFrame(right, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 6))
        self.l_sel = ctk.CTkLabel(top, text="Extracted data", font=self.app.f_h2, anchor="w")
        self.l_sel.pack(side="left")
        ctk.CTkButton(top, text="Open scan", width=96, height=30, command=self.open_scan).pack(side="right")
        ctk.CTkButton(top, text="Open CSV", width=96, height=30, command=self.open_csv).pack(side="right", padx=8)
        self.tpl_btn = ctk.CTkButton(top, text="✎  Create template from this file", height=30,
                                     fg_color=PURPLE, hover_color="#6d4bb0", command=self.make_template)
        self._tpl_btn_shown = False
        self.hgrid = HeaderGrid(right, None, None)
        self.hgrid.grid(row=1, column=0, sticky="ew", padx=16)
        self.lines = LinesTable(right, None, None)
        self.lines.grid(row=2, column=0, sticky="nsew", padx=16, pady=(10, 6))
        self.l_problem = ctk.CTkLabel(right, text="", text_color=AMBER, anchor="w", wraplength=640,
                                      justify="left", font=self.app.f_small)
        self.l_problem.grid(row=3, column=0, sticky="ew", padx=16, pady=(0, 12))

    def restyle(self):
        style_tree(self, "Files.Treeview")
        for st, (_, colour) in STATUS.items():
            self.ftree.tag_configure(st, foreground=colour if st != "extracted" else "")
        if self.ftree.selection():
            self._pick()

    # ---------------- events from the worker
    def log(self, text: str, tag: str = ""):
        self.logbox.configure(state="normal")
        self.logbox._textbox.insert("end", datetime.now().strftime("%H:%M:%S  "), "time")
        self.logbox._textbox.insert("end", text + "\n", tag)
        self.logbox.see("end")
        self.logbox.configure(state="disabled")

    def add_row(self, row: dict, res):
        row["_time"] = datetime.now().strftime("%H:%M:%S")
        self.rows.append(row)
        self.results.append(res)
        label = STATUS.get(row["status"], (row["status"], GREY))[0]
        self.ftree.insert("", 0, iid=str(len(self.rows) - 1), tags=(row["status"],),
                          values=(row["_time"], row["file"], row.get("template", "—"), row.get("rows", 0), label))
        tag = {"extracted": "ok", "needs_review": "warn", "error": "err", "no_template": "warn"}.get(row["status"], "")
        self.log(f"{label:<13} {row['file']}  ({row.get('rows', 0)} lines)"
                 + (f"  - {row['detail']}" if row.get("detail") else ""), tag)
        self.stats()
        if len(self.rows) == 1 or not self.ftree.selection():
            self.ftree.selection_set(str(len(self.rows) - 1))

    def stats(self):
        cnt = Counter(r["status"] for r in self.rows)
        self.c_ok.set(cnt["extracted"])
        self.c_rev.set(cnt["needs_review"] + cnt["error"])
        self.c_none.set(cnt["no_template"])
        self.c_lines.set(sum(int(r.get("rows") or 0) for r in self.rows))

    def progress(self):
        n, d = self.pass_total, self.pass_done
        if not n:
            return
        self.bar.set(d / n)
        self.l_pct.configure(text=f"{d / n:.0%}")
        if d:
            per = (time.time() - self.pass_started) / d
            left = int(per * (n - d))
            m, s = divmod(left, 60)
            self.l_eta.configure(text=f"{d} of {n} done   ·   {per:.1f} s per file   ·   about "
                                      + (f"{m} min {s:02d} s left" if m else f"{s} s left"))
        else:
            self.l_eta.configure(text=f"0 of {n} done   ·   estimating time…")

    def count_waiting(self):
        try:
            folder, out = Path(self.app.v_folder.get().strip()), Path(self.app.v_out.get().strip())
            n = len(ex.pending_files(folder, out, self.v_prefix.get().strip(), 0)) if folder.is_dir() else 0
        except OSError:
            n = 0
        self.c_wait.set(n)
        if not self.rows:
            self.l_current.configure(text=f"{n} file(s) without a CSV yet. Press Start." if n
                                     else "Every file in the folder already has a CSV.")

    # ---------------- selection
    def _pick(self, _=None):
        sel = self.ftree.selection()
        if not sel:
            return
        i = int(sel[0])
        row, res = self.rows[i], self.results[i]
        t = next((x for x in ex.load_templates(Path(self.app.v_templates.get())) if x.name == row.get("template")), None)
        self.l_sel.configure(text=row["file"])
        self.hgrid.show(t, res)
        self.lines.show(t, res)
        detail = row.get("detail", "") or ""
        none = row["status"] == "no_template"
        if none:
            detail = (detail + "\n" if detail else "") + \
                "No template fits this file. Click 'Create template from this file' to define one with it as the sample."
        self.l_problem.configure(text=detail)
        if none and not self._tpl_btn_shown:
            self.tpl_btn.pack(side="right")
        elif not none and self._tpl_btn_shown:
            self.tpl_btn.pack_forget()
        self._tpl_btn_shown = none

    def _double(self):
        r = self._selected()
        if r and r["status"] == "no_template":
            self.make_template()
        else:
            self.open_csv()

    def _context(self, event):
        iid = self.ftree.identify_row(event.y)
        if not iid:
            return
        self.ftree.selection_set(iid)
        r = self.rows[int(iid)]
        m = tk.Menu(self, tearoff=0)
        m.add_command(label="Create template from this file…", command=self.make_template)
        m.add_separator()
        m.add_command(label="Open scan", command=self.open_scan)
        m.add_command(label="Open CSV", command=self.open_csv,
                      state="normal" if r.get("_csv") and Path(r["_csv"]).exists() else "disabled")
        m.tk_popup(event.x_root, event.y_root)

    def make_template(self):
        r = self._selected()
        if not r or not Path(r["_path"]).exists():
            messagebox.showinfo(APP_NAME, "The file is no longer there.", parent=self)
            return
        self.app.template_from_file(r["_path"])

    def _selected(self) -> dict | None:
        sel = self.ftree.selection()
        return self.rows[int(sel[0])] if sel else None

    def open_csv(self):
        r = self._selected()
        if r and r.get("_csv") and Path(r["_csv"]).exists():
            os.startfile(r["_csv"])

    def open_scan(self):
        r = self._selected()
        if r and Path(r["_path"]).exists():
            os.startfile(r["_path"])

    def open_out(self):
        p = Path(self.app.v_out.get().strip())
        p.mkdir(parents=True, exist_ok=True)
        os.startfile(p)

    def open_log(self):
        p = Path(self.app.v_log.get().strip() or DEFAULTS["log_dir"]) / "extract_log.csv"
        if p.exists():
            os.startfile(p)
        else:
            messagebox.showinfo(APP_NAME, f"No log yet at\n{p}", parent=self)

    def clear(self):
        self.rows.clear()
        self.results.clear()
        self.ftree.delete(*self.ftree.get_children())
        self.stats()
        if self._tpl_btn_shown:
            self.tpl_btn.pack_forget()
            self._tpl_btn_shown = False


# =========================================================================== app
class App(ctk.CTk):
    def __init__(self):
        self.settings = load_settings()
        ctk.set_appearance_mode(self.settings["appearance"])
        ctk.set_default_color_theme("blue")
        super().__init__()
        self.title(f"{APP_NAME}  ·  Developed by {DEVELOPER}")
        self.geometry("1500x900")
        self.minsize(1240, 760)
        ico = resource("app.ico")
        if ico.exists():
            self.iconbitmap(str(ico))
            self.after(300, lambda: self.iconbitmap(str(ico)))
        self.f_title = ctk.CTkFont("Segoe UI", 22, "bold")
        self.f_h2 = ctk.CTkFont("Segoe UI", 15, "bold")
        self.f_body = ctk.CTkFont("Segoe UI", 13)
        self.f_small = ctk.CTkFont("Segoe UI", 12)
        self.events: queue.Queue = queue.Queue()
        self.cache: dict = {}
        self.worker: Worker | None = None

        s = self.settings
        self.v_folder = tk.StringVar(value=s["folder"])
        self.v_out = tk.StringVar(value=s["out_dir"])
        self.v_log = tk.StringVar(value=s["log_dir"])
        self.v_templates = tk.StringVar(value=s["templates"])
        seed_templates(Path(s["templates"]))

        h = QueueLogHandler(self.events)
        ex.log.addHandler(h)
        ex.log.setLevel(logging.INFO)
        ex.log.propagate = False

        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self._build_nav()
        self.container = ctk.CTkFrame(self, fg_color="transparent")
        self.container.grid(row=0, column=1, sticky="nsew", padx=20, pady=18)
        self.container.grid_columnconfigure(0, weight=1)
        self.container.grid_rowconfigure(1, weight=1)
        head = ctk.CTkFrame(self.container, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        self.l_page = ctk.CTkLabel(head, text="Run", font=self.f_title)
        self.l_page.pack(side="left")
        self.pill = ctk.CTkLabel(head, text="●  Idle", corner_radius=14, height=30,
                                 fg_color=("gray85", "gray22"), font=self.f_body)
        self.pill.pack(side="right", ipadx=12)
        self.run_page = RunPage(self.container, self)
        self.editor = TemplateEditor(self.container, self)
        self.restyle()
        self.show("Run")
        self.run_page.count_waiting()
        self.v_folder.trace_add("write", lambda *_: self.after(400, self._waiting_if_idle))
        self.v_out.trace_add("write", lambda *_: self.after(400, self._waiting_if_idle))
        self.v_templates.trace_add("write", lambda *_: self.after(400, self.editor.refresh_list))
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.after(80, self.poll)

    def _build_nav(self):
        nav = ctk.CTkFrame(self, width=230, corner_radius=0, fg_color=("gray92", "gray13"))
        nav.grid(row=0, column=0, sticky="nsew")
        nav.grid_propagate(False)
        nav.grid_rowconfigure(4, weight=1)
        ctk.CTkLabel(nav, text="🧾  Invoice\n     Extractor", font=self.f_title, justify="left",
                     anchor="w").grid(row=0, column=0, sticky="w", padx=20, pady=(22, 2))
        ctk.CTkLabel(nav, text="Templates  →  CSV", font=self.f_small,
                     text_color=("gray40", "gray60")).grid(row=1, column=0, sticky="w", padx=22, pady=(0, 20))
        self.nav_btns = {}
        for i, (name, icon) in enumerate((("Run", "▶"), ("Templates", "✎"))):
            b = ctk.CTkButton(nav, text=f"  {icon}   {name}", anchor="w", height=42, corner_radius=10,
                              font=ctk.CTkFont("Segoe UI", 14), command=lambda n=name: self.show(n))
            b.grid(row=2 + i, column=0, sticky="new", padx=14, pady=4)
            self.nav_btns[name] = b
        foot = ctk.CTkFrame(nav, fg_color="transparent")
        foot.grid(row=5, column=0, sticky="sew", padx=20, pady=18)
        ctk.CTkLabel(foot, text="Theme", font=self.f_small, text_color=("gray40", "gray60")).pack(anchor="w")
        self.v_theme = tk.StringVar(value=self.settings["appearance"])
        ctk.CTkOptionMenu(foot, values=["Dark", "Light", "System"], variable=self.v_theme, width=190,
                          height=28, command=self.set_theme).pack(anchor="w", pady=(4, 14))
        ctk.CTkLabel(foot, text=f"Developed by {DEVELOPER}", font=ctk.CTkFont("Segoe UI", 12, "bold"),
                     text_color=("gray35", "gray65")).pack(anchor="w")
        ctk.CTkLabel(foot, text=f"v{VERSION}  ·  offline OCR", font=ctk.CTkFont("Segoe UI", 11),
                     text_color=("gray50", "gray45")).pack(anchor="w")

    def show(self, name: str):
        for n, b in self.nav_btns.items():
            active = n == name
            b.configure(fg_color=("#1f6aa5" if active else "transparent"),
                        text_color=("#ffffff" if active else ("gray15", "gray85")),
                        hover_color=("#1a5a8c" if active else ("gray82", "gray22")))
        self.l_page.configure(text=name)
        self.run_page.grid_forget()
        self.editor.grid_forget()
        (self.run_page if name == "Run" else self.editor).grid(row=1, column=0, sticky="nsew")
        if name == "Templates":
            self.editor.refresh_list(self.editor.template.name if self.editor.template else None)

    def template_from_file(self, path: str):
        """Templates page, with a new template whose sample is this file."""
        prev = self.l_page.cget("text")
        self.show("Templates")
        if not self.editor.new_template(sample=path):
            self.show(prev)

    def set_theme(self, mode):
        ctk.set_appearance_mode(mode)
        self.after(60, self.restyle)

    def restyle(self):
        self.run_page.restyle()
        self.editor.restyle()

    def browse(self, var: tk.StringVar):
        d = filedialog.askdirectory(initialdir=var.get() or None, parent=self)
        if d:
            var.set(str(Path(d)))

    def pill_text(self, text, colour=None):
        self.pill.configure(text=f"●  {text}", text_color=colour or ("gray20", "gray85"))

    def _waiting_if_idle(self):
        if not self.running:
            self.run_page.count_waiting()

    @property
    def running(self) -> bool:
        return self.worker is not None and self.worker.is_alive()

    # ---------------- run control
    def collect(self) -> dict | None:
        rp = self.run_page
        try:
            cfg = dict(folder=self.v_folder.get().strip(), out_dir=self.v_out.get().strip(),
                       log_dir=self.v_log.get().strip() or DEFAULTS["log_dir"],
                       templates=self.v_templates.get().strip() or DEFAULTS["templates"],
                       prefix=rp.v_prefix.get().strip(), mode=rp.v_mode.get(),
                       interval=max(5, int(float(rp.v_interval.get()))),
                       settle=self.settings.get("settle", 5), appearance=self.v_theme.get())
        except ValueError:
            messagebox.showerror(APP_NAME, "The interval must be a number of seconds.", parent=self)
            return None
        if not Path(cfg["folder"]).is_dir():
            messagebox.showerror(APP_NAME, f"Scans folder not found:\n{cfg['folder']}", parent=self)
            return None
        if not cfg["out_dir"]:
            messagebox.showerror(APP_NAME, "Choose a folder for the CSV files.", parent=self)
            return None
        return cfg

    def start_stop(self):
        rp = self.run_page
        if self.running:
            self.worker.stop_event.set()
            rp.start_btn.configure(state="disabled", text="Stopping…")
            self.pill_text("Stopping after this file…", AMBER)
            return
        if self.editor.dirty and not messagebox.askyesno(
                APP_NAME, "The template you are editing has unsaved changes, which this run won't "
                          "use. Start anyway?", parent=self):
            return
        cfg = self.collect()
        if not cfg:
            return
        self.settings.update(cfg)
        save_settings(self.settings)
        self.worker = Worker(SimpleNamespace(**cfg), self.events, self.cache)
        for w in rp.inputs:
            w.configure(state="disabled")
        rp.start_btn.configure(text="■   Stop", fg_color=RED, hover_color="#b84a4a")
        self.pill_text("Starting…", BLUE)
        rp.log(f"Started ({cfg['mode'].lower()}) on {cfg['folder']}  →  {cfg['out_dir']}", "info")
        self.worker.start()

    def stopped(self):
        rp = self.run_page
        self.worker = None
        for w in rp.inputs:
            w.configure(state="normal")
        rp.start_btn.configure(state="normal", text="▶   Start", fg_color=GREEN, hover_color="#258a5e")
        self.pill_text("Idle")
        rp.l_eta.configure(text=" ")
        rp.l_current.configure(text="All done ✔" if rp.pass_total and rp.pass_done >= rp.pass_total
                               else "Stopped.")
        rp.log("Stopped.", "info")
        rp.count_waiting()

    def poll(self):
        rp = self.run_page
        try:
            for _ in range(300):
                e = self.events.get_nowait()
                kind = e[0]
                if kind == "log":
                    if e[1] >= logging.WARNING:
                        rp.log(e[2], "err" if e[1] >= logging.ERROR else "warn")
                elif kind == "status":
                    rp.l_current.configure(text=e[1])
                    self.pill_text(e[1].rstrip("…"), BLUE)
                elif kind == "pass_start":
                    rp.pass_total, rp.pass_done, rp.pass_started = e[1], 0, time.time()
                    rp.bar.set(0)
                    rp.log(f"Found {e[1]} file(s) to extract", "info")
                elif kind == "file_start":
                    rp.pass_done = e[2] - 1
                    rp.l_current.configure(text=f"Reading  {e[1]}")
                    self.pill_text(f"Working  {e[2]}/{e[3]}", GREEN)
                    rp.c_wait.set(e[3] - e[2] + 1)
                    rp.progress()
                elif kind == "row":
                    rp.add_row(e[1], e[2])
                    rp.pass_done += 1
                    rp.c_wait.set(max(0, rp.pass_total - rp.pass_done))
                    rp.progress()
                elif kind == "pass_done":
                    rp.bar.set(1)
                    rp.l_pct.configure(text="100%")
                    rp.l_current.configure(text=f"Pass finished: {rp.pass_total} file(s) ✔")
                elif kind == "waiting":
                    self.pill_text(f"Watching  ·  next check in {e[1]}s", GREEN)
                    rp.l_eta.configure(text="Waiting for new files. They are picked up automatically.")
                elif kind == "fatal":
                    rp.log(e[1], "err")
                    messagebox.showerror(APP_NAME, e[1], parent=self)
                elif kind == "stopped":
                    self.stopped()
        except queue.Empty:
            pass
        self.after(80, self.poll)

    def on_close(self):
        if self.editor.dirty and not messagebox.askyesno(
                APP_NAME, "Close without saving the template you are editing?", parent=self):
            return
        if self.running:
            if not messagebox.askyesno(APP_NAME, "A run is in progress. Stop it and exit?", parent=self):
                return
            self.worker.stop_event.set()
        rp = self.run_page
        self.settings.update(folder=self.v_folder.get().strip(), out_dir=self.v_out.get().strip(),
                             log_dir=self.v_log.get().strip(), templates=self.v_templates.get().strip(),
                             prefix=rp.v_prefix.get().strip(), mode=rp.v_mode.get(),
                             appearance=self.v_theme.get())
        try:
            self.settings["interval"] = int(float(rp.v_interval.get()))
        except ValueError:
            pass
        save_settings(self.settings)
        self.destroy()


def main():
    if len(sys.argv) > 1:  # command-line mode, same options as extractor.py
        sys.exit(ex.main(sys.argv[1:]))
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass
    App().mainloop()


if __name__ == "__main__":
    main()
