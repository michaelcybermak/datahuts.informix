# Invoice renamer

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
