"""Print PDFs via CUPS (`lp`) without extra apps or print dialogs."""

from __future__ import annotations

import os
import subprocess
from typing import List


def list_printers() -> List[str]:
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


def default_printer() -> str:
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
    subprocess.run(["lp", "-d", printer, "-o", "fit-to-page", path], check=True)
