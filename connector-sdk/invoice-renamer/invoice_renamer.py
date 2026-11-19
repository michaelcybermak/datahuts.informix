"""
Invoice photo renamer.

Scans a folder of scanned invoice images (AAW layout), reads the
OrderNbr / Invoice numbers and the order type suffix (e.g. "MS") with
local OCR, and renames each file to:

    <order number>-<invoice number>-<order type>.<ext>
    e.g. 1651959-1652117-MS.jpg

Usage:
    python invoice_renamer.py "D:\\Work\\AAW\\New Samples" --dry-run
    python invoice_renamer.py "D:\\Work\\AAW\\New Samples"
    python invoice_renamer.py "D:\\Work\\AAW\\Inbox" --watch 30

Files whose names already look like 1234567-1234567-XX are skipped
unless --include-renamed is given (useful for checking accuracy).
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import shutil
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageFilter, ImageOps

try:  # progress bar is optional; the script still runs without tqdm
    from tqdm import tqdm
    from tqdm.contrib.logging import logging_redirect_tqdm
except ImportError:
    tqdm = None

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp", ".webp", ".pdf"}  # PDFs: text layer, else OCR
DEFAULT_KNOWN_TYPES = ["MS", "MB", "MC", "ML", "MN"]  # extend with --types
# A type not in the list is still accepted when read the same way this often:
UNKNOWN_TYPE_MIN_READS = 3
UNKNOWN_TYPE_MIN_SHARE = 0.7
DEFAULT_MS_ABOVE = 1650000  # order numbers above this are always type MS
RENAMED_RE = re.compile(r"^\d{4,10}-\d{4,10}-[A-Z0-9]{1,4}(_\d+)?$")

# Header block holding OrderNbr / Invoice (fractions of page width/height).
HEADER_BOX = (0.55, 0.15, 1.00, 0.42)  # tall enough for pages scanned lower down

# "OrderNbr - 1651959 MS"   /   "Invoice - 1652117 MS" (not "InvoiceDate")
ORDER_RE = re.compile(r"Order\s*N\w*\W*?(\d{4,10})(?:\s+([A-Z0-9$]{1,3})\b)?", re.I)
# Case-sensitive on purpose: the field label is "Invoice", the page title is "INVOICE".
INVOICE_RE = re.compile(r"\b[Il1]nvo[il1f!|]ce\b(?!\s*Date)\W*?(\d{4,10})(?:\s+([A-Z0-9$]{1,3})\b)?")
# Label-only and value-only patterns, for pairing fragments by position.
ORDER_LABEL_RE = re.compile(r"Order\s*N\w*", re.I)
INVOICE_LABEL_RE = re.compile(r"\b[Il1]nvo[il1f!|]ce(?!\s*-?\s*Date)\b", re.I)
VALUE_RE = re.compile(r"(?<!\d)(\d{4,10})(?!\d)(?:\s*([A-Z0-9$]{1,3})\b)?", re.I)

# Characters OCR commonly reads in place of the intended one on faded scans.
CONFUSABLE = {
    "S": "58BDOU09$",
    "M": "NH",
    "O": "0DQU",
    "B": "8",
    "D": "0O",
    "C": "G(0O",
    "I": "1L|",
}

log = logging.getLogger("invoice_renamer")


def _adaptive_threshold(gray: np.ndarray) -> np.ndarray:
    import cv2

    if gray.shape[1] < 2000:  # low-res scans: upscale so thin strokes survive
        gray = cv2.resize(gray, None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    return cv2.adaptiveThreshold(cv2.medianBlur(gray, 3), 255,
                                 cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 31, 15)


@dataclass
class Extraction:
    order: str | None = None
    invoice: str | None = None
    order_type: str | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.order and self.invoice and self.order_type)

    def filename(self, ext: str) -> str:
        return f"{self.order}-{self.invoice}-{self.order_type}{ext.lower()}"


class InvoiceReader:
    def __init__(self, known_types: list[str], ms_above: int | None = DEFAULT_MS_ABOVE):
        self.ms_above = ms_above
        # Imported here so --help works even before the OCR engine is installed.
        from rapidocr import RapidOCR

        logging.getLogger("RapidOCR").setLevel(logging.ERROR)
        self.engine = RapidOCR()
        logging.getLogger("RapidOCR").setLevel(logging.ERROR)  # it resets its level on init
        self.known_types = [t.upper() for t in known_types]

    # ---------- OCR ----------
    def _ocr(self, img: Image.Image) -> list[tuple[str, np.ndarray]]:
        """Return [(text, 4x2 corner box)] for every text fragment found."""
        result = self.engine(np.array(img.convert("RGB")))
        if not result.txts:
            return []
        return [(t, np.asarray(b, dtype=float)) for t, b in zip(result.txts, result.boxes)]

    @staticmethod
    def _by_layout(items, label_re: re.Pattern, skip_title: bool = False,
                   anchor: np.ndarray | None = None) -> tuple[str, str | None, np.ndarray] | None:
        """
        Find the value printed to the right of a label on the same line.

        OCR reading order is unreliable on tilted/folded scans (a value can come
        out *before* its label), so pair by position instead: follow the label's
        own slant to the right and take the nearest fragment holding a number.
        """
        labels = [(text, box, m) for text, box in items
                  if (m := label_re.search(text))
                  # the big "I N V O I C E" title is all capitals; the field label isn't
                  and not (skip_title and m.group(0).isupper())]
        if anchor is not None:
            # The Invoice label sits under OrderNbr, left-aligned with it.
            ax, ay = anchor[:, 0].min(), anchor[:, 1].mean()
            labels = [lb for lb in labels if lb[1][:, 1].mean() > ay]
            labels.sort(key=lambda lb: abs(lb[1][:, 0].min() - ax))

        for text, box, m in labels:
            # Value in the same fragment, e.g. "OrderNbr - 1651098 MS"
            if v := VALUE_RE.search(text[m.end():]):
                return v.group(1), v.group(2), box

            tl, tr, br, bl = box
            height = max(abs(bl[1] - tl[1]), 1.0)
            slope = (tr[1] - tl[1]) / max(tr[0] - tl[0], 1.0)
            cx, cy = box[:, 0].mean(), box[:, 1].mean()
            right = box[:, 0].max()

            def on_row(b):
                bx, by = b[:, 0].mean(), b[:, 1].mean()
                return bx > right and abs(by - (cy + slope * (bx - cx))) < 0.6 * height

            row = sorted(((b[:, 0].min(), t) for t, b in items if on_row(b)), key=lambda r: r[0])
            for idx, (_, t) in enumerate(row):
                if v := VALUE_RE.search(t):
                    typ = v.group(2)
                    if not typ and idx + 1 < len(row):  # type in the next fragment
                        nxt = row[idx + 1][1].strip()
                        typ = nxt if re.fullmatch(r"[A-Za-z0-9$]{1,3}", nxt) else None
                    return v.group(1), typ, box
        return None

    def _extract(self, items) -> tuple[tuple | None, tuple | None]:
        """(order_number, type), (invoice_number, type) from one OCR pass."""
        order = self._by_layout(items, ORDER_LABEL_RE)
        invoice = self._by_layout(items, INVOICE_LABEL_RE, skip_title=True,
                                  anchor=order[2] if order else None)
        order = order[:2] if order else None
        invoice = invoice[:2] if invoice else None
        if order is None or invoice is None:  # fall back to reading order
            text = " ".join(t for t, _ in items)
            if order is None and (m := ORDER_RE.search(text)):
                order = m.groups()
            if invoice is None and (m := INVOICE_RE.search(text)):
                invoice = m.groups()
        return order, invoice

    @staticmethod
    def _variants(page: Image.Image):
        """Yield progressively different renderings of the header block."""
        w, h = page.size
        x0, y0, x1, y1 = HEADER_BOX
        crop = page.crop((int(w * x0), int(h * y0), int(w * x1), int(h * y1)))
        gray = crop.convert("L")
        contrast = ImageOps.autocontrast(gray, cutoff=2)

        def fit(im, max_side):
            im = im.copy()
            im.thumbnail((max_side, max_side))
            return im

        yield "contrast", fit(contrast, 2400)
        yield "plain", fit(crop, 1400)
        yield "adaptive", Image.fromarray(_adaptive_threshold(np.array(gray)))
        yield "contrast-small", fit(contrast, 1400)
        yield "sharpen", fit(contrast.filter(ImageFilter.SHARPEN), 2400)
        # Last resort: whole page, in case the scan is shifted.
        yield "full-page", fit(ImageOps.autocontrast(page.convert("L"), cutoff=2), 2600)

    def _type_by_rule(self, order: str) -> str | None:
        """Order numbers above the threshold are always MS."""
        if self.ms_above is not None and int(order) > self.ms_above:
            return "MS"
        return None

    # ---------- parsing ----------
    @staticmethod
    def _clean_type(raw: str | None) -> str | None:
        if not raw:
            return None
        raw = re.sub(r"[^A-Z0-9$]", "", raw.upper())
        return raw or None

    def _match_known(self, read: str) -> tuple[str, bool] | None:
        """Map an OCR read to a known type: (type, exact?) or None."""
        if read in self.known_types:
            return read, True
        hits = {k for k in self.known_types
                if len(k) == len(read)
                and all(r == c or r in CONFUSABLE.get(c, "") for r, c in zip(read, k))}
        return (hits.pop(), False) if len(hits) == 1 else None

    def _decide_type(self, votes: Counter, exact_only: bool = False) -> tuple[str, bool] | None:
        """Return (type, had_exact_read) or None. Exact reads weigh 2, look-alikes 1."""
        score, exact = Counter(), set()
        for read, n in votes.items():
            if m := self._match_known(read):
                t, is_exact = m
                if is_exact:
                    exact.add(t)
                score[t] += n * (2 if is_exact else 1)
        if exact_only:
            score = Counter({t: s for t, s in score.items() if t in exact})
        if not score:
            return None
        best = score.most_common(1)[0][0]
        return best, best in exact

    @staticmethod
    def _consistent_unknown(votes: Counter) -> str | None:
        """An unlisted type, accepted only if OCR read it the same way consistently."""
        total = sum(votes.values())
        if not total:
            return None
        best, n = votes.most_common(1)[0]
        if (re.fullmatch(r"[A-Z]{2,3}", best) and n >= UNKNOWN_TYPE_MIN_READS
                and n / total >= UNKNOWN_TYPE_MIN_SHARE):
            return best
        return None

    # ---------- PDFs ----------
    @staticmethod
    def _pdf_text_items(page) -> list[tuple[str, np.ndarray]]:
        """Text runs of a digital PDF page as [(text, 4x2 box)], top-left origin like OCR."""
        tp = page.get_textpage()
        try:
            height = page.get_height()
            items = []
            for i in range(tp.count_rects()):
                left, bottom, right, top = tp.get_rect(i)
                text = tp.get_text_bounded(left, bottom, right, top).strip()
                if text:
                    y0, y1 = height - top, height - bottom
                    items.append((text, np.array([[left, y0], [right, y0], [right, y1], [left, y1]],
                                                 dtype=float)))
            return items
        finally:
            tp.close()

    def read(self, path: Path) -> Extraction:
        if path.suffix.lower() != ".pdf":
            return self._read_image(ImageOps.exif_transpose(Image.open(path)), path.name)

        import pypdfium2 as pdfium  # only needed for PDFs

        pdf = pdfium.PdfDocument(str(path))
        try:
            if len(pdf) == 0:
                raise ValueError("PDF has no pages")
            page = pdf[0]  # the header block is on the first page
            # Digital PDF: read the text layer directly (exact, no OCR needed).
            items = self._pdf_text_items(page)
            if items:
                order, invoice = self._extract(items)
                log.debug("%s [pdf-text]: order=%s invoice=%s | %s", path.name, order, invoice,
                          " ".join(t for t, _ in items))
                orders, invoices, types = Counter(), Counter(), Counter()
                for found, bucket in ((order, orders), (invoice, invoices)):
                    if found:
                        bucket[found[0]] += 1
                        if t := self._clean_type(found[1]):
                            types[t] += UNKNOWN_TYPE_MIN_READS  # text is exact, not a guess
                ex = self._decide(orders, invoices, types)
                if ex.ok:
                    return ex
            # Scanned PDF (or text layer without the fields): render page 1 and OCR it.
            img = page.render(scale=300 / 72).to_pil()
        finally:
            pdf.close()
        return self._read_image(img, path.name)

    def _read_image(self, page: Image.Image, name_for_log: str) -> Extraction:
        orders, invoices, types = Counter(), Counter(), Counter()

        for name, img in self._variants(page):
            items = self._ocr(img)
            order, invoice = self._extract(items)
            log.debug("%s [%s]: order=%s invoice=%s | %s", name_for_log, name, order, invoice,
                      " ".join(t for t, _ in items))
            for found, bucket in ((order, orders), (invoice, invoices)):
                if found:
                    bucket[found[0]] += 1
                    if t := self._clean_type(found[1]):
                        types[t] += 1

            # Stop early once both numbers and a certain type are in hand.
            if orders and invoices:
                o, i = orders.most_common(1)[0][0], invoices.most_common(1)[0][0]
                if o != i and (self._type_by_rule(o) or self._decide_type(types, exact_only=True)):
                    break
        return self._decide(orders, invoices, types)

    def _decide(self, orders: Counter, invoices: Counter, types: Counter) -> Extraction:
        ex = Extraction()
        if orders:
            ex.order = orders.most_common(1)[0][0]
            if len(orders) > 1:
                ex.problems.append(f"order reads disagree {dict(orders)}")
        else:
            ex.problems.append("order number not found")
        if invoices:
            ex.invoice = invoices.most_common(1)[0][0]
            if len(invoices) > 1:
                ex.problems.append(f"invoice reads disagree {dict(invoices)}")
        else:
            ex.problems.append("invoice number not found")
        decided = self._decide_type(types)
        if ex.order and (rule := self._type_by_rule(ex.order)):
            ex.order_type = rule  # business rule beats OCR of the faint suffix
        elif decided:
            ex.order_type, was_exact = decided
            if not was_exact:
                ex.problems.append(f"type inferred from look-alike reads {dict(types)}")
        elif unknown := self._consistent_unknown(types):
            ex.order_type = unknown
            ex.problems.append(f"new type {unknown} (not in --types), reads {dict(types)}")
        else:
            ex.problems.append(
                f"order type unclear (reads: {dict(types) or 'none'}; known: {self.known_types})")
        if ex.order and ex.order == ex.invoice:
            ex.problems.append("order and invoice numbers are identical")
            ex.order_type = None  # force review
        return ex


def unique_target(target: Path, source: Path) -> Path:
    if not target.exists() or target.resolve() == source.resolve():
        return target
    n = 2
    while True:
        cand = target.with_name(f"{target.stem}_{n}{target.suffix}")
        if not cand.exists():
            return cand
        n += 1


def pending_files(folder: Path, args, seen: set[Path]) -> list[Path]:
    """Images in `folder` that are due for processing this pass."""
    def wanted(path: Path) -> bool:
        if path in seen:
            return False
        if args.prefix and not path.name.upper().startswith(args.prefix.upper()):
            if not (args.include_renamed and RENAMED_RE.match(path.stem)):
                return False  # only scanner output (e.g. SKM_...) is processed
        if RENAMED_RE.match(path.stem) and not args.include_renamed:
            return False
        try:
            # scanner may still be writing it; pick it up next pass
            return time.time() - path.stat().st_mtime >= args.settle
        except FileNotFoundError:
            return False

    files = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_EXTS)
    return [p for p in files if wanted(p)]


def process_folder(folder: Path, reader: InvoiceReader, args, seen: set[Path]) -> list[dict]:
    rows = []

    def record(row: dict) -> None:
        rows.append(row)
        if not args.no_log and not args.dry_run:
            write_log(Path(args.log_dir), [row])  # per file, so a stopped run keeps its log

    todo = pending_files(folder, args, seen)
    if not todo:
        return rows

    bar = None
    if tqdm is not None and not args.no_progress:
        # disable=None hides the bar when output is not a terminal (e.g. piped to a file)
        bar = tqdm(total=len(todo), unit="file", desc="Renaming", dynamic_ncols=True,
                   disable=None)
    try:
        for path in todo:
            _process_one(path, reader, args, seen, record)
            if bar is not None:
                bar.set_postfix(Counter(r["status"] for r in rows), refresh=False)
                bar.update(1)
    finally:
        if bar is not None:
            bar.close()
    return rows


def _process_one(path: Path, reader: "InvoiceReader", args, seen: set[Path], record) -> None:
    started = time.time()
    try:
        ex = reader.read(path)
    except Exception as e:  # unreadable/corrupt image, etc.
        # Not added to `seen`, so --watch retries it next pass.
        log.error("%s: failed to read (%s)", path.name, e)
        record(dict(file=path.name, status="error", detail=str(e)))
        return

    secs = time.time() - started
    row = dict(file=path.name, order=ex.order, invoice=ex.invoice,
               order_type=ex.order_type, seconds=round(secs, 1))

    if not ex.ok:
        row.update(status="needs_review", detail="; ".join(ex.problems))
        log.warning("REVIEW  %s  -> %s", path.name, row["detail"])
        if args.review_dir and not args.dry_run:
            try:
                review = Path(args.review_dir)
                review.mkdir(parents=True, exist_ok=True)
                # shutil.move also works across drives and to network shares (copy + delete)
                shutil.move(str(path), str(unique_target(review / path.name, path)))
            except OSError as e:
                log.error("%s: could not move to review folder (%s); will retry", path.name, e)
                row.update(status="error", detail=f"{row['detail']}; move to review failed: {e}")
                record(row)
                return  # not added to `seen`, so --watch retries it
        seen.add(path)
        record(row)
        return

    target = unique_target(path.with_name(ex.filename(path.suffix)), path)
    row["new_name"] = target.name
    if ex.problems:
        row["detail"] = "; ".join(ex.problems)

    if target.name == path.name:
        row["status"] = "unchanged"
        log.info("OK      %s  (already correct)", path.name)
    elif args.dry_run:
        row["status"] = "would_rename"
        log.info("DRY     %s  -> %s", path.name, target.name)
    else:
        try:
            path.rename(target)
        except OSError as e:  # e.g. file open in a viewer / still syncing
            log.error("%s: could not rename (%s); will retry", path.name, e)
            row.update(status="error", detail=f"rename failed: {e}")
            record(row)
            return  # not added to `seen`, so --watch retries it
        row["status"] = "renamed"
        log.info("RENAMED %s  -> %s", path.name, target.name)
        seen.add(target)
    seen.add(path)
    record(row)


def write_log(folder: Path, rows: list[dict]) -> Path | None:
    if not rows:
        return None
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / "rename_log.csv"
    new = not out.exists()
    fields = ["timestamp", "file", "new_name", "order", "invoice", "order_type",
              "status", "detail", "seconds"]
    with out.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        if new:
            w.writeheader()
        ts = datetime.now().isoformat(timespec="seconds")
        for r in rows:
            w.writerow({"timestamp": ts, **r})
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Rename scanned invoices to ORDER-INVOICE-TYPE.")
    ap.add_argument("folder", help="Folder containing the scanned invoice images")
    ap.add_argument("--dry-run", action="store_true", help="Show what would be renamed, change nothing")
    ap.add_argument("--watch", type=float, metavar="SECONDS",
                    help="Keep running and check the folder for new files every SECONDS")
    ap.add_argument("--types", default=",".join(DEFAULT_KNOWN_TYPES),
                    help="Comma-separated known order types (default: %(default)s). "
                         "Types not in this list go to review instead of being guessed.")
    ap.add_argument("--ms-above", type=int, default=DEFAULT_MS_ABOVE,
                    help="Order numbers above this are always MS, no OCR of the type "
                         "(default: %(default)s; use -1 to always read the type from the scan)")
    ap.add_argument("--review-dir", help="Move files that could not be read confidently here")
    ap.add_argument("--include-renamed", action="store_true",
                    help="Also process files already named like ORDER-INVOICE-TYPE")
    ap.add_argument("--prefix", default="SKM",
                    help="Only process files whose name starts with this (default: %(default)s; "
                         "use \"\" for all files)")
    ap.add_argument("--settle", type=float, default=5, metavar="SECONDS",
                    help="Ignore files modified less than this long ago (default: 5)")
    ap.add_argument("--log-dir", default=r"C:\RPA",
                    help="Folder for rename_log.csv (default: %(default)s)")
    ap.add_argument("--no-log", action="store_true", help="Do not write rename_log.csv")
    ap.add_argument("--no-progress", action="store_true", help="Do not show the progress bar")
    ap.add_argument("-v", "--verbose", action="store_true", help="Print raw OCR text")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s", datefmt="%H:%M:%S")

    folder = Path(args.folder)
    if not folder.is_dir():
        log.error("Folder not found: %s", folder)
        return 2

    reader = InvoiceReader([t.strip() for t in args.types.split(",") if t.strip()],
                           ms_above=None if args.ms_above < 0 else args.ms_above)
    seen: set[Path] = set()

    while True:
        if tqdm is not None and not args.no_progress:
            # print log lines above the bar instead of breaking it
            with logging_redirect_tqdm():
                rows = process_folder(folder, reader, args, seen)
        else:
            rows = process_folder(folder, reader, args, seen)
        if rows:
            counts = Counter(r["status"] for r in rows)
            log.info("Done: %s", ", ".join(f"{k}={v}" for k, v in counts.items()))
        if not args.watch:
            break
        try:
            time.sleep(args.watch)
        except KeyboardInterrupt:
            break
    return 0


if __name__ == "__main__":
    sys.exit(main())
