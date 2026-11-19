# Invoice Extractor (v2)

Pulls the header values and table lines you choose out of each invoice (scans and PDFs,
local OCR, offline) and writes **one CSV per invoice** to a folder you pick. Files are not
renamed or moved. v1 (the renamer below) is kept on branch `invoice-renamer-v1`.

## Desktop app (InvoiceExtractor.exe)

`dist\InvoiceExtractor.exe` is a single file; copy it to any Windows PC and double-click it.

**Templates page**: one template per supplier layout.

1. *Open sample invoice* - pick a scan or PDF from that supplier. The text found is outlined.
2. *Draw: Field* - drag a box around a header value (or click a word to box it). Name it,
   e.g. `invoice_number`, and choose a type: text, number, amount or date. Tick *Required*
   if the invoice must not pass without it; *Must match* takes an optional regex such as `\d{5,10}`.
3. *Draw: Table column* - drag a box over each column of the table body (below the headings).
   All columns share the same top and bottom; rows are found automatically.
4. *Identify words* - text printed on that supplier's invoices (company name). Each file gets
   the template with the most matches. *Table ends at* - words that end the table, e.g. Total.
5. *Test on sample*, then *Save template*.

Boxes can be moved (drag) and resized (drag the corner); Delete removes one; Ctrl+wheel zooms.
A box drawn as the wrong kind can be switched between Field and Table column in *Selected*.
Templates are saved as JSON in `%APPDATA%\InvoiceExtractor\templates`; AAW and LuLu LPO
templates are installed on first start.

**Multi-page invoices** (PDFs):

- Table lines are read from every page, in order, into the same CSV, with a `page` column.
  On each page the table is found by its column headings (saved with the template), so it may
  start higher on page 2 than on page 1. Reading stops at a *Table ends at* word.
- *Skip lines with*: words of lines to leave out, such as the page footer printed between
  pages (`Page`, for `Page 1 of 2 ...`). Unlike *Table ends at*, the table carries on after them.
- *Table runs to the end of each page* (on by default): a column box drawn over one row still
  reads the whole table, and invoices longer than the sample are complete.
- Fields are read from the first page, or from the last page (*Read from: Last page*), e.g.
  totals. Use ◀ ▶ to see other pages of the sample; a field drawn on the last page reads from it.
- *Follows label*: the box moves with a printed label such as `Total Invoice Cost:`, for
  values whose position changes with the number of lines.
- A row the printer split over the page break (values at the bottom of one page, its number
  at the top of the next) is joined back into one row.
  Wrapped lines at the top of a page, before its first numbered row (e.g. the codes line
  of the previous page's last item), are added to that last item.
- Lines with nothing in the first column, or (when the table has number/amount/date columns)
  nothing in any of those, are not new rows: they are wrapped text of the row above, or
  stamps, notes and page footers.

**Line check** (optional, per template): pick the quantity, price and total columns and every
line is checked for quantity x price = total (allowing for prices printed rounded). A
quantity OCR clearly misread - e.g. `61` for `6|` on dot-matrix printouts, or blank - is
replaced by total / price when that is a whole number made of the digits read; the CSV
`check` column and the log detail say so. Lines that still don't add up send the file to
review.

**Scans**: pages scanned sideways are turned upright automatically (also in the editor).
Later pages of a scan may be cut shorter than page 1; they are measured in page-1 units so
the columns still line up. Once the table has ended, the remaining pages are not read.

Starter templates (installed on first start):

| template | documents | checked on |
|---|---|---|
| AAW | AAW invoices, scanned | 6 scans: 5 read fully, the faded one sent to review |
| LuLu LPO | LuLu purchase orders, digital PDF, 1-3 pages | all 64 POs, 1,227 lines: every value matches the PDFs' own text |
| Carrefour LPO | Carrefour purchase orders, sideways dot-matrix scans | sample POs: lines add up to the printed totals; misread lines go to review |

**Run page**: scans folder, CSV folder, Run once or Watch. Each file gets `<scan name>.csv`
with one row per table line and the header values repeated on every row (UTF-8 with BOM, so
Excel shows Arabic correctly). A file is done once its CSV exists, so re-scanning a file
re-extracts it. Status per file goes to `C:\RPA\extract_log.csv`:

| status | meaning |
|---|---|
| extracted | all required values found, CSV written |
| needs_review | CSV written, but a required value is missing (see detail) |
| no_template | no template's identify words were found; no CSV |
| error | the file could not be read; no CSV |

A *No template* file can be turned into a template straight from the Run page: select it and
click *Create template from this file* (or double-click / right-click it). The Templates page
opens with a new template and that file already loaded as its sample. Once it is saved, the
next run picks the file up again, since it still has no CSV.

Command line (also what the exe does when started with arguments):

    python extractor.py "D:\Scans" --out "C:\RPA\InvoiceCSV" --watch 30
    InvoiceExtractorCmd.exe "D:\Scans" --out "C:\RPA\InvoiceCSV" --watch 30

`InvoiceExtractorCmd.exe` (`python build_exe.py cmd`) is the console version: it prints each
file and a summary, and says so when every file already has its CSV. `InvoiceExtractor.exe`
also accepts these arguments but, being a window app, prints nothing in cmd.

Limits: an invoice scanned as separate image files (one per page) is treated as separate
invoices; scan multi-page invoices to one PDF.

Build: `python build_exe.py` (InvoiceExtractor.exe) or `python build_exe.py renamer`.

---

# Invoice renamer (v1)

Reads scanned AAW invoice images with local OCR (free, offline) and renames each to

    <OrderNbr>-<Invoice>-<OrderType>.jpg      e.g. 1651959-1652117-MS.jpg, 94300-94303-MB.jpg

## Desktop app (InvoiceRenamer.exe)

`dist\InvoiceRenamer.exe` is a single file. Copy it to any Windows PC and double-click it;
Python does not need to be installed there. The app has:

- folder pickers for the scans, the log and (optionally) a folder for unclear scans
- Run once or Watch mode, a preview-only switch, and the advanced rules (prefix, types, MS rule)
- a dashboard with counts, a progress bar and time left
- a results table with search and filters, and a preview of the scan for the selected row
- an activity log, and Undo to put this session's renames back

Settings are remembered in `%APPDATA%\InvoiceRenamer\settings.json`.

Started with arguments, the exe works like the command-line script (handy for Task Scheduler):

    InvoiceRenamer.exe "D:\Work\AAW\New Samples" --watch 30

To rebuild the exe after changing the code:

    pip install -r requirements.txt customtkinter pyinstaller
    python build_exe.py

Developed by CyberMAK 2026.

## Install (command-line script)

    pip install -r requirements.txt

## Run

    # preview only
    python invoice_renamer.py "D:\Work\AAW\New Samples" --dry-run

    # rename everything in the folder once
    python invoice_renamer.py "D:\Work\AAW\New Samples"

    # keep watching the folder, checking every 30 s (Ctrl+C to stop)
    python invoice_renamer.py "D:\Work\AAW\New Samples" --watch 30

A progress bar (tqdm) shows files done, time left and counts per status, with each file's result
printed above it. It hides itself when output goes to a file or pipe; `--no-progress` turns it off.

## File types

- Images: jpg, jpeg, png, tif, tiff, bmp, webp, read with OCR.
- PDFs: a digital PDF (one with real text) is read straight from its text, which is instant and
  exact. A scanned PDF, or one whose text lacks the fields, has page 1 rendered and read with OCR
  like an image. The renamed file keeps its own extension (`196707-196749-ML.pdf`).

## Rules

- Only files whose names begin with `SKM` (the scanner's output) are processed
  (`--prefix`, `--prefix ""` for all files).
- Order numbers above 1,650,000 are always type **MS** (`--ms-above`, `-1` disables).
- Other orders: the type is read from the scan and is matched against `--types`
  (default `MS,MB,MC,ML,MN`). A type not in that list is still accepted if OCR reads it the same
  way at least 3 times and in 70% of reads; the log notes it as a "new type". Anything unclear is
  left alone and logged as `needs_review` (or moved with `--review-dir`, which may be on another
  drive or a network share).
- Multi-page invoices get `_2`, `_3`… suffixes. Files already named `N-N-XX` are skipped.
- Every file processed is appended to `C:\RPA\rename_log.csv` (change with `--log-dir`).
