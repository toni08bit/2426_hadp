"""Print PDFs via CUPS (`lp`) without extra apps or print dialogs."""

from __future__ import annotations

import os
import re
import subprocess
from typing import List, Optional

_LPSTAT_ENV = {**os.environ, "LC_ALL": "C", "LANG": "C", "LANGUAGE": "C"}
_PRINTER_LINE = re.compile(
    r"^printer\s+(?P<name>.+?)\s+(?:is|disabled|now)\b"
)
_DEVICE_LINE = re.compile(r"^device for\s+(?P<name>.+?):")


def _lpstat(*args: str, timeout: float = 8.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["lpstat", *args],
        capture_output=True,
        text=True,
        check=False,
        env=_LPSTAT_ENV,
        timeout=timeout,
    )


def _cups_error(stderr: str) -> Optional[str]:
    text = (stderr or "").strip()
    if not text:
        return None
    lowered = text.lower()
    if "scheduler is not running" in lowered:
        return "CUPS-Dienst läuft nicht"
    if "no destinations added" in lowered:
        return None
    if text.startswith("lpstat:"):
        return text.split(":", 1)[1].strip() or text
    return text


def list_printers() -> List[str]:
    try:
        enumerated = _lpstat("-e")
        queued = _lpstat("-p")
        devices = _lpstat("-v")
    except FileNotFoundError as exc:
        raise RuntimeError("lpstat nicht gefunden — CUPS ist nicht installiert") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("lpstat hat nicht rechtzeitig geantwortet") from exc

    for result in (enumerated, queued, devices):
        message = _cups_error(result.stderr)
        if message:
            raise RuntimeError(message)

    names: List[str] = []
    seen = set()

    def add(raw: str) -> None:
        name = raw.strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)

    for line in enumerated.stdout.splitlines():
        add(line)
    for line in queued.stdout.splitlines():
        match = _PRINTER_LINE.match(line.strip())
        if match:
            add(match.group("name"))
    for line in devices.stdout.splitlines():
        match = _DEVICE_LINE.match(line.strip())
        if match:
            add(match.group("name"))
    return names


def default_printer() -> str:
    try:
        result = _lpstat("-d")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return ""
    text = result.stdout.strip()
    if ":" in text:
        name = text.split(":", 1)[1].strip()
        if name and "none" not in name.lower():
            return name
    return ""


def print_pdf(path: str, printer: str) -> None:
    if not printer:
        raise RuntimeError("Kein Drucker ausgewählt")
    if not os.path.isfile(path):
        raise FileNotFoundError(path)
    subprocess.run(["lp", "-d", printer, "-o", "fit-to-page", path], check=True)
