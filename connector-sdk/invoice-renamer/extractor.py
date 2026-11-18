"""
Invoice Extractor (v2) - template-based extraction of header fields and table lines to CSV.

A *template* describes one supplier layout:
  - identify: words that appear on that supplier's invoices (used to pick the template)
  - fields:   named boxes on the page, e.g. order_number, invoice_date
  - columns:  named boxes over the table body, one per column; rows are found automatically
All positions are fractions of the page (0-1), so they work at any scan resolution.

For every input file one CSV is written: one row per table line, header values repeated
on each row. Files are never renamed or moved.

    python extractor.py "D:\\Scans" --out "D:\\CSV"              # once
    python extractor.py "D:\\Scans" --out "D:\\CSV" --watch 30   # keep watching
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

log = logging.getLogger("invoice_extractor")

SCAN_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".pdf"}
FIELD_TYPES = ["text", "number", "amount", "date"]
OCR_MAX_SIDE = 2000  # plenty for printed invoice text, and much faster than full size


# --------------------------------------------------------------------------- page text
@dataclass
class Word:
    """A piece of text on the page with its box as page fractions (x0, y0, x1, y1)."""
    text: str
    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    @property
    def h(self) -> float:
        return self.y1 - self.y0


@dataclass
class Page:
    image: Image.Image          # page 1 as an image (for preview / editor)
    words: list[Word]
    source: str                 # "pdf-text" or "ocr"


class OcrEngine:
    """Loads RapidOCR once and reuses it."""

    def __init__(self):
        from rapidocr import RapidOCR

        logging.getLogger("RapidOCR").setLevel(logging.ERROR)
        self.engine = RapidOCR()
        logging.getLogger("RapidOCR").setLevel(logging.ERROR)

    def words(self, img: Image.Image) -> list[Word]:
        gray = ImageOps.autocontrast(img.convert("L"), cutoff=1)
        if max(gray.size) > OCR_MAX_SIDE:
            gray.thumbnail((OCR_MAX_SIDE, OCR_MAX_SIDE))
        w, h = gray.size
        res = self.engine(np.array(gray.convert("RGB")))
        out = []
        for text, box in zip(res.txts or [], res.boxes if res.boxes is not None else []):
            b = np.asarray(box, dtype=float)
            out.append(Word(text.strip(), b[:, 0].min() / w, b[:, 1].min() / h,
                            b[:, 0].max() / w, b[:, 1].max() / h))
        return [x for x in out if x.text]


def _pdf_words(page) -> list[Word]:
    tp = page.get_textpage()
    try:
        pw, ph = page.get_width(), page.get_height()
        out = []
        for i in range(tp.count_rects()):
            left, bottom, right, top = tp.get_rect(i)
            text = tp.get_text_bounded(left, bottom, right, top).strip()
            if text:
                out.append(Word(text, left / pw, (ph - top) / ph, right / pw, (ph - bottom) / ph))
        return out
    finally:
        tp.close()


def load_page(path: Path, ocr: OcrEngine | None, need_words: bool = True) -> Page:
    """Page 1 of an image or PDF, with its words (text layer for digital PDFs, else OCR)."""
    if path.suffix.lower() == ".pdf":
        import pypdfium2 as pdfium

        pdf = pdfium.PdfDocument(str(path))
        try:
            if len(pdf) == 0:
                raise ValueError("PDF has no pages")
            pg = pdf[0]
            img = pg.render(scale=200 / 72).to_pil().convert("RGB")
            words = _pdf_words(pg)
        finally:
            pdf.close()
        if len(words) >= 5:  # a real text layer, not a scan with a stray watermark
            return Page(img, words, "pdf-text")
    else:
        with Image.open(path) as im:
            img = ImageOps.exif_transpose(im).convert("RGB")
    if not need_words:
        return Page(img, [], "none")
    if ocr is None:
        raise RuntimeError("OCR engine not loaded")
    return Page(img, ocr.words(img), "ocr")


# --------------------------------------------------------------------------- templates
@dataclass
class Region:
    name: str
    kind: str                   # "field" or "column"
    box: list[float]            # x0, y0, x1, y1 as page fractions
    type: str = "text"          # text | number | amount | date
    required: bool = False
    pattern: str = ""           # optional regex the value must contain, e.g. \d{5,10}


@dataclass
class Template:
    name: str
    identify: list[str] = field(default_factory=list)     # words that identify this layout
    regions: list[Region] = field(default_factory=list)
    stop_words: list[str] = field(default_factory=lambda: ["Total"])
    merge_wrapped: bool = True    # a row with an empty first column continues the row above
    anchor: dict | None = None    # {"text":..., "x":..., "y":...} used to correct shifted scans
    sample: str = ""              # sample file the template was drawn on

    @property
    def fields(self) -> list[Region]:
        return [r for r in self.regions if r.kind == "field"]

    @property
    def columns(self) -> list[Region]:
        return sorted((r for r in self.regions if r.kind == "column"), key=lambda r: r.box[0])

    def to_json(self) -> dict:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict) -> "Template":
        d = dict(d)
        d["regions"] = [Region(**r) for r in d.get("regions", [])]
        return cls(**d)


def safe_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip() or "template"


def load_templates(folder: Path) -> list[Template]:
    out = []
    for p in sorted(folder.glob("*.json")):
        try:
            out.append(Template.from_json(json.loads(p.read_text(encoding="utf-8"))))
        except (OSError, ValueError, TypeError) as e:
            log.error("Template %s could not be read (%s)", p.name, e)
    return out


def save_template(folder: Path, t: Template) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / f"{safe_name(t.name)}.json"
    p.write_text(json.dumps(t.to_json(), indent=2, ensure_ascii=False), encoding="utf-8")
    return p


def _norm(s: str) -> str:
    return re.sub(r"\s+", "", s).lower()


def find_anchor(words: list[Word], text: str) -> Word | None:
    key = _norm(text)
    hits = [w for w in words if key and key in _norm(w.text)]
    return min(hits, key=lambda w: w.y0) if hits else None


def make_anchor(t: Template, words: list[Word]) -> dict | None:
    """Remember where the first identify word sits on the sample, to align other scans."""
    for text in t.identify:
        if w := find_anchor(words, text):
            return {"text": text, "x": w.x0, "y": w.y0}
    return None


def identify(words: list[Word], templates: list[Template]) -> Template | None:
    """The template whose identify words appear most on the page (at least one)."""
    page = _norm(" ".join(w.text for w in words))
    best, best_score = None, 0
    for t in templates:
        score = sum(1 for k in t.identify if _norm(k) and _norm(k) in page)
        if t.identify and score > best_score:
            best, best_score = t, score
    return best


# --------------------------------------------------------------------------- extraction
def _text_in(words: list[Word], box, dx: float = 0.0, dy: float = 0.0) -> list[tuple[float, float, float, str]]:
    """Text inside a box: (cy, x0, height, text). Words crossing the box edge are cut by
    character position, so a box over just the value of "OrderNbr - 1653270 MS" works."""
    x0, y0, x1, y1 = box[0] + dx, box[1] + dy, box[2] + dx, box[3] + dy
    out = []
    for w in words:
        if not (y0 <= w.cy <= y1) or w.x1 <= x0 or w.x0 >= x1:
            continue
        if w.x0 >= x0 and w.x1 <= x1:
            out.append((w.cy, w.x0, w.h, w.text))
            continue
        n = len(w.text)
        step = (w.x1 - w.x0) / max(n, 1)
        keep = "".join(c for i, c in enumerate(w.text) if x0 <= w.x0 + (i + 0.5) * step <= x1)
        if keep.strip():
            out.append((w.cy, max(w.x0, x0), w.h, keep.strip()))
    return out


def _join_lines(pieces) -> str:
    if not pieces:
        return ""
    pieces = sorted(pieces)
    lines, cur, cur_y = [], [], None
    med_h = float(np.median([p[2] for p in pieces])) or 0.01
    for cy, x, h, t in pieces:
        if cur_y is not None and abs(cy - cur_y) > 0.6 * med_h:
            lines.append(cur)
            cur = []
        cur.append((x, t))
        cur_y = cy if cur_y is None else (cur_y + cy) / 2
    lines.append(cur)
    return " ".join(" ".join(t for _, t in sorted(line)) for line in lines).strip()


def clean_value(value: str, typ: str, pattern: str = "") -> str:
    v = value.strip(" -:|.,").strip()
    if typ == "number":
        m = re.search(r"-?\d[\d,]*", v)
        v = m.group(0).replace(",", "") if m else ""
    elif typ == "amount":
        m = re.search(r"-?\d[\d,]*(?:\.\d+)?", v)
        v = m.group(0) if m else ""
    elif typ == "date":
        m = re.search(r"\d{1,4}[/.\-]\d{1,2}[/.\-]\d{1,4}", v)
        v = m.group(0) if m else ""
    else:
        v = re.sub(r"\s+", " ", v)
    if pattern and v:
        try:
            m = re.search(pattern, v)
        except re.error:
            return v
        v = m.group(0) if m else ""
    return v


@dataclass
class Result:
    template: str = ""
    header: dict = field(default_factory=dict)
    rows: list[dict] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    source: str = ""

    @property
    def ok(self) -> bool:
        return not self.problems


def apply_template(t: Template, words: list[Word]) -> Result:
    res = Result(template=t.name)
    dx = dy = 0.0
    if t.anchor and (a := find_anchor(words, t.anchor["text"])):
        dx, dy = a.x0 - t.anchor["x"], a.y0 - t.anchor["y"]
        if abs(dx) > 0.15 or abs(dy) > 0.15:  # matched something else; don't trust it
            dx = dy = 0.0

    for f in t.fields:
        res.header[f.name] = clean_value(_join_lines(_text_in(words, f.box, dx, dy)), f.type, f.pattern)
        if f.required and not res.header[f.name]:
            res.problems.append(f"{f.name} not found")

    cols = t.columns
    if cols:
        res.rows = _table_rows(t, cols, words, dx, dy)
        for c in cols:
            if c.required and not any(r.get(c.name) for r in res.rows):
                res.problems.append(f"column {c.name} empty")
    return res


def _table_rows(t: Template, cols: list[Region], words, dx, dy) -> list[dict]:
    top = min(c.box[1] for c in cols) + dy
    bottom = max(c.box[3] for c in cols) + dy
    left = min(c.box[0] for c in cols) + dx
    right = max(c.box[2] for c in cols) + dx
    inside = [w for w in words if top <= w.cy <= bottom and w.x1 > left and w.x0 < right]
    if not inside:
        return []
    # group words into printed lines
    inside.sort(key=lambda w: w.cy)
    med_h = float(np.median([w.h for w in inside])) or 0.01
    lines, cur = [], [inside[0]]
    for w in inside[1:]:
        if abs(w.cy - np.mean([x.cy for x in cur])) > 0.55 * med_h:
            lines.append(cur)
            cur = []
        cur.append(w)
    lines.append(cur)

    stops = [_norm(s) for s in t.stop_words if s.strip()]
    rows: list[dict] = []
    last_bottom = None
    for line in lines:
        line_text = _norm(" ".join(w.text for w in line))
        if stops and any(s in line_text for s in stops):
            break
        y0, y1 = min(w.y0 for w in line) - dy, max(w.y1 for w in line) - dy
        row = {}
        for c in cols:
            band = [c.box[0], y0, c.box[2], y1]
            row[c.name] = clean_value(_join_lines(_text_in(line, band, dx, dy)), c.type, c.pattern)
        if not any(row.values()):
            continue
        first = cols[0].name
        if not row[first]:
            # A line with no value in the first column is either wrapped text of the row above
            # (directly under it) or a stamp / handwriting on the page: never a new row.
            close = last_bottom is not None and (y0 + dy) - last_bottom < 1.2 * med_h
            if t.merge_wrapped and rows and close:
                for c in cols:
                    if c.type == "text" and row[c.name]:
                        rows[-1][c.name] = f"{rows[-1][c.name]} {row[c.name]}".strip()
                last_bottom = y1 + dy
            continue
        rows.append(row)
        last_bottom = y1 + dy
    return rows


# --------------------------------------------------------------------------- files
def csv_path(out_dir: Path, src: Path) -> Path:
    return out_dir / f"{src.stem}.csv"


def write_csv(out_dir: Path, src: Path, t: Template, res: Result) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    target = csv_path(out_dir, src)
    head = [f.name for f in t.fields]
    cols = [c.name for c in t.columns]
    fieldnames = ["source_file", "template", *head, *cols]
    rows = res.rows or [{}]
    tmp = target.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8-sig") as f:  # BOM so Excel shows Arabic
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({"source_file": src.name, "template": t.name, **res.header, **r})
    tmp.replace(target)
    return target


def write_log(log_dir: Path, row: dict) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    out = log_dir / "extract_log.csv"
    new = not out.exists()
    fields = ["timestamp", "file", "template", "csv", "rows", "status", "detail", "seconds"]
    with out.open("a", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        w.writerow({"timestamp": datetime.now().isoformat(timespec="seconds"), **row})


def pending_files(folder: Path, out_dir: Path, prefix: str, settle: float) -> list[Path]:
    """Scans with no CSV yet (or a CSV older than the scan)."""
    todo = []
    now = time.time()
    for p in sorted(folder.iterdir()):
        if not p.is_file() or p.suffix.lower() not in SCAN_EXTS:
            continue
        if prefix and not p.name.upper().startswith(prefix.upper()):
            continue
        try:
            mtime = p.stat().st_mtime
        except FileNotFoundError:
            continue
        if now - mtime < settle:
            continue
        out = csv_path(out_dir, p)
        if out.exists() and out.stat().st_mtime >= mtime:
            continue
        todo.append(p)
    return todo


def process_file(path: Path, templates: list[Template], ocr: OcrEngine, out_dir: Path,
                 dry_run: bool = False) -> dict:
    """Extract one file. Returns a log row (plus 'result' for the app)."""
    started = time.time()
    page = load_page(path, ocr)
    t = identify(page.words, templates)
    row = dict(file=path.name, rows=0)
    if t is None:
        row.update(status="no_template", detail="no template's identify words found on the page")
        return row
    res = apply_template(t, page.words)
    res.source = page.source
    row.update(template=t.name, rows=len(res.rows), result=res,
               seconds=round(time.time() - started, 1))
    if not dry_run:
        row["csv"] = write_csv(out_dir, path, t, res).name
    if res.ok:
        row["status"] = "extracted" if not dry_run else "preview"
    else:
        row.update(status="needs_review", detail="; ".join(res.problems))
    return row


# --------------------------------------------------------------------------- CLI
def default_template_dir() -> Path:
    import os
    return Path(os.environ.get("APPDATA", str(Path.home()))) / "InvoiceExtractor" / "templates"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Extract invoice header fields and table lines to CSV.")
    ap.add_argument("folder", help="Folder with the scanned / PDF invoices")
    ap.add_argument("--out", required=True, help="Folder for the CSV files")
    ap.add_argument("--templates", default=str(default_template_dir()),
                    help="Folder with template .json files (default: %(default)s)")
    ap.add_argument("--watch", type=float, metavar="SECONDS", help="Keep watching the folder")
    ap.add_argument("--prefix", default="", help="Only files whose name starts with this")
    ap.add_argument("--settle", type=float, default=5, help="Skip files changed in the last N seconds")
    ap.add_argument("--log-dir", default=r"C:\RPA", help="Folder for extract_log.csv (default: %(default)s)")
    ap.add_argument("--dry-run", action="store_true", help="Show results, write no CSV")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    folder, out_dir = Path(args.folder), Path(args.out)
    templates = load_templates(Path(args.templates))
    if not templates:
        log.error("No templates in %s - create one in the app first.", args.templates)
        return 2
    ocr = OcrEngine()
    failed: set[Path] = set()
    while True:
        for p in pending_files(folder, out_dir, args.prefix, args.settle):
            if p in failed:
                continue
            try:
                row = process_file(p, templates, ocr, out_dir, args.dry_run)
            except Exception as e:
                row = dict(file=p.name, status="error", detail=str(e))
            res = row.pop("result", None)
            if row["status"] in ("error", "no_template") or args.dry_run:
                failed.add(p)  # no CSV was written; don't retry every pass
            log.info("%-12s %s  -> %s rows  %s", row["status"].upper(), p.name, row.get("rows", 0),
                     row.get("detail", ""))
            if args.verbose and res:
                log.info("   header: %s", res.header)
                for r in res.rows:
                    log.info("   row: %s", r)
            if not args.dry_run:
                write_log(Path(args.log_dir), row)
        if not args.watch:
            break
        try:
            time.sleep(args.watch)
        except KeyboardInterrupt:
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
