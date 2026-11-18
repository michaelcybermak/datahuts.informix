"""Build InvoiceRenamer.exe (Windows) with PyInstaller.

    pip install -r requirements.txt pyinstaller customtkinter
    python build_exe.py

Output: dist\\InvoiceRenamer.exe - a single file you can copy to any Windows PC;
Python does not need to be installed there.
"""

import os
import subprocess
import sys
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent


def make_icon(path: Path) -> None:
    """Draw a simple invoice-with-arrow icon in several sizes."""
    S = 256
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((8, 8, S - 8, S - 8), radius=56, fill=(31, 106, 165, 255))
    d.rounded_rectangle((62, 40, 194, 216), radius=14, fill=(255, 255, 255, 255))
    for y, w in ((78, 92), (106, 70), (134, 92), (162, 56)):
        d.rounded_rectangle((82, y, 82 + w, y + 12), radius=6, fill=(160, 178, 196, 255))
    d.ellipse((140, 140, 236, 236), fill=(47, 165, 114, 255))
    d.polygon([(166, 188), (196, 188), (196, 172), (220, 194), (196, 216), (196, 200), (166, 200)],
              fill=(255, 255, 255, 255))
    im.save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def main() -> int:
    ico = HERE / "app.ico"
    make_icon(ico)
    cmd = [
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
        "--onefile", "--windowed", "--name", "InvoiceRenamer",
        "--icon", str(ico), "--add-data", f"{ico};.",
        "--collect-data", "rapidocr",          # OCR models + config files
        "--collect-submodules", "rapidocr",
        "--collect-binaries", "onnxruntime",
        "--collect-data", "customtkinter",     # themes and fonts
        "--collect-all", "pypdfium2", "--collect-all", "pypdfium2_raw",  # PDF engine (pdfium.dll)
        # rapidocr can use these engines but we only need onnxruntime
        "--exclude-module", "torch", "--exclude-module", "paddle",
        "--exclude-module", "openvino", "--exclude-module", "tensorflow",
        "--exclude-module", "matplotlib", "--exclude-module", "pandas",
        str(HERE / "invoice_renamer_gui.py"),
    ]
    # PyInstaller finds runtime DLLs through PATH. Put System32 first so it bundles the current
    # Microsoft C++ runtime (msvcp140.dll), not an old copy from another program such as Java,
    # which makes onnxruntime fail with "DLL initialization routine failed".
    env = dict(os.environ)
    system32 = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32")
    env["PATH"] = system32 + os.pathsep + env.get("PATH", "")
    return subprocess.call(cmd, cwd=HERE, env=env)


if __name__ == "__main__":
    sys.exit(main())
