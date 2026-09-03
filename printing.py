"""Print PDFs on the system printer without extra apps or print dialogs.

On Windows, pages are rasterized in-process and sent to the spooler via GDI.
CUPS (`lp`) is used on other platforms.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import List


def list_printers() -> List[str]:
    if sys.platform == "win32":
        return _list_printers_windows()
    return _list_printers_cups()


def default_printer() -> str:
    if sys.platform == "win32":
        import win32print

        try:
            return win32print.GetDefaultPrinter() or ""
        except Exception:
            return ""
    try:
        result = subprocess.run(
            ["lpstat", "-d"],
            capture_output=True,
            text=True,
            check=False,
        )
        text = result.stdout.strip()
        if ":" in text:
            return text.split(":", 1)[1].strip()
    except FileNotFoundError:
        pass
    return ""


def print_pdf(path: str, printer: str) -> None:
    if not printer:
        raise RuntimeError("Kein Drucker ausgewählt")
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    if sys.platform == "win32":
        _print_pdf_windows(path, printer)
    else:
        _print_pdf_cups(path, printer)


def _list_printers_windows() -> List[str]:
    import win32print

    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    names = [info["pPrinterName"] for info in win32print.EnumPrinters(flags, None, 2)]
    return sorted(set(names), key=str.lower)


def _list_printers_cups() -> List[str]:
    try:
        result = subprocess.run(
            ["lpstat", "-p"],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        return []
    names: List[str] = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[0] == "printer":
            names.append(parts[1])
    return names


def _print_pdf_windows(path: str, printer: str) -> None:
    import fitz
    import win32con
    import win32ui
    from PIL import Image, ImageWin

    path = os.path.abspath(path)
    doc = fitz.open(path)
    if doc.page_count < 1:
        doc.close()
        raise ValueError("PDF hat keine Seiten")

    hdc = win32ui.CreateDC()
    hdc.CreatePrinterDC(printer)
    printable_w = hdc.GetDeviceCaps(win32con.HORZRES)
    printable_h = hdc.GetDeviceCaps(win32con.VERTRES)
    if printable_w < 1 or printable_h < 1:
        hdc.DeleteDC()
        doc.close()
        raise RuntimeError(f"Drucker '{printer}' meldet keinen druckbaren Bereich")

    started = False
    try:
        hdc.StartDoc(os.path.basename(path))
        started = True
        for page in doc:
            pix = page.get_pixmap(matrix=fitz.Matrix(200 / 72, 200 / 72), alpha=False)
            img = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            img = _fit_to_page(img, printable_w, printable_h)
            x = (printable_w - img.width) // 2
            y = (printable_h - img.height) // 2
            hdc.StartPage()
            ImageWin.Dib(img).draw(
                hdc.GetHandleOutput(),
                (x, y, x + img.width, y + img.height),
            )
            hdc.EndPage()
    finally:
        if started:
            hdc.EndDoc()
        hdc.DeleteDC()
        doc.close()


def _fit_to_page(img, printable_w: int, printable_h: int):
    from PIL import Image

    page_landscape = img.width > img.height
    paper_landscape = printable_w > printable_h
    if page_landscape != paper_landscape:
        img = img.rotate(90, expand=True)
    scale = min(printable_w / img.width, printable_h / img.height)
    width = max(1, int(img.width * scale))
    height = max(1, int(img.height * scale))
    if (width, height) != img.size:
        img = img.resize((width, height), Image.Resampling.LANCZOS)
    return img


def _print_pdf_cups(path: str, printer: str) -> None:
    subprocess.run(["lp", "-d", printer, "-o", "fit-to-page", path], check=True)
