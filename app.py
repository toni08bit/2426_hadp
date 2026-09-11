#!/usr/bin/env python3
"""Holt /api/depeschen und druckt neue PDFs. Status-Oberfläche im Forest-Theme."""

from __future__ import annotations

import json
import math
import queue
import re
import shutil
import subprocess
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urljoin

import requests
import yaml
from requests import exceptions as req_exc

import printing

APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "config.yml"
STATE_PATH = APP_DIR / "state.json"
CACHE_DIR = APP_DIR / "cache"
THEME_DIR = APP_DIR / "vendor" / "Forest-ttk-theme"
RECENT_LIMIT = 200
APP_NAME = "2426_HADP"
CONNECT_TIMEOUT = 10
READ_TIMEOUT_API = 30
READ_TIMEOUT_PDF = 60
PRINT_FILTERS = ("none", "all", "only_responsible")

STATUS_DARK_GRAY = "#3a3a3a"
STATUS_YELLOW = "#e8c200"
STATUS_RED = "#E24B4A"
STATUS_HATCH = "#252525"

UNIT_STATUS_COLORS: Dict[Union[int, str], str] = {
    0: STATUS_RED,
    1: STATUS_DARK_GRAY,
    2: STATUS_DARK_GRAY,
    3: STATUS_YELLOW,
    4: STATUS_RED,
    5: STATUS_RED,
    6: STATUS_DARK_GRAY,
    "A": STATUS_YELLOW,
}
FLASH_STATUSES = {0, 5, "A"}
HATCH_STATUSES = {6}
UNKNOWN_STATUS_COLOR = STATUS_DARK_GRAY
UNIT_ENTRY_RE = re.compile(r"^(?P<label>.+?)\s*\((?P<unit>[^)]+)\)\s*$")


def load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.is_file():
        example = APP_DIR / "config.example.yml"
        raise FileNotFoundError(
            f"{CONFIG_PATH.name} fehlt. Kopieren Sie {example.name} nach config.yml "
            "und tragen Sie die Werte ein."
        )
    with CONFIG_PATH.open(encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    host = str(data.get("host_url") or "").rstrip("/")
    token = str(data.get("bearer_token") or "").strip()
    interval = int(data.get("poll_interval_seconds") or 5)
    if not host:
        raise ValueError("config.yml: host_url ist erforderlich")
    if not token:
        raise ValueError("config.yml: bearer_token ist erforderlich")
    if interval < 1:
        raise ValueError("config.yml: poll_interval_seconds muss >= 1 sein")
    theme = str(data.get("theme") or "forest-dark")
    if theme not in ("forest-dark", "forest-light"):
        theme = "forest-dark"
    raw_units = data.get("responsible_units") or []
    if not isinstance(raw_units, list):
        raise ValueError("config.yml: responsible_units muss eine Liste sein")
    responsible = [
        parse_responsible_unit(str(u))
        for u in raw_units
        if str(u).strip()
    ]
    print_filter = str(data.get("print_filter") or "all").strip().lower()
    if print_filter not in PRINT_FILTERS:
        raise ValueError(
            "config.yml: print_filter muss none, all oder only_responsible sein"
        )
    return {
        "host_url": host,
        "bearer_token": token,
        "poll_interval_seconds": interval,
        "printer": str(data.get("printer") or "").strip(),
        "theme": theme,
        "responsible_units": responsible,
        "print_filter": print_filter,
    }


def parse_responsible_unit(raw: str) -> Dict[str, str]:
    """Parse 'Anzeigename (EINHEITSNAME)'; bare names stay label and unit."""
    text = raw.strip()
    match = UNIT_ENTRY_RE.match(text)
    if match:
        label = match.group("label").strip()
        unit = match.group("unit").strip()
        if label and unit:
            return {"label": label, "unit": unit}
    return {"label": text, "unit": text}


def load_state() -> Dict[str, Any]:
    if not STATE_PATH.is_file():
        return {}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: Dict[str, Any]) -> None:
    STATE_PATH.write_text(json.dumps(state, indent=2), encoding="utf-8")


def format_ts(unix: Optional[float]) -> str:
    if unix is None:
        return "—"
    return datetime.fromtimestamp(unix).strftime("%d.%m.%Y %H:%M:%S")


def depesche_id(item: Dict[str, Any]) -> str:
    link = str(item.get("pdf_link") or "")
    unit = str(item.get("unit") or "")
    ts = item.get("timestamp", "")
    return f"{ts}|{unit}|{link}"


def normalize_unit_status(raw: Any) -> Union[int, str, None]:
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        stripped = raw.strip()
        if stripped.upper() == "A":
            return "A"
        if stripped.isdigit() or (stripped.startswith("-") and stripped[1:].isdigit()):
            return int(stripped)
        return stripped
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, int):
        return raw
    if isinstance(raw, float) and raw.is_integer():
        return int(raw)
    return raw


def unit_status_color(status: Any) -> str:
    normalized = normalize_unit_status(status)
    if normalized is None:
        return UNKNOWN_STATUS_COLOR
    return UNIT_STATUS_COLORS.get(normalized, "#E24B4A")


def status_should_flash(status: Any) -> bool:
    return normalize_unit_status(status) in FLASH_STATUSES


def status_should_hatch(status: Any) -> bool:
    return normalize_unit_status(status) in HATCH_STATUSES


def mix_hex(hex_color: str, other: str, amount: float) -> str:
    """Blend hex_color toward other by amount (0..1)."""
    amount = max(0.0, min(1.0, amount))

    def parts(value: str) -> tuple[int, int, int]:
        raw = value.lstrip("#")
        if len(raw) != 6:
            return (0, 0, 0)
        return int(raw[0:2], 16), int(raw[2:4], 16), int(raw[4:6], 16)

    r1, g1, b1 = parts(hex_color)
    r2, g2, b2 = parts(other)
    r = int(r1 + (r2 - r1) * amount)
    g = int(g1 + (g2 - g1) * amount)
    b = int(b1 + (b2 - b1) * amount)
    return f"#{r:02x}{g:02x}{b:02x}"


def rounded_rect(
    canvas: tk.Canvas,
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    radius: float,
    **kwargs: Any,
) -> int:
    radius = max(0.0, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points = [
        x1 + radius,
        y1,
        x2 - radius,
        y1,
        x2,
        y1,
        x2,
        y1 + radius,
        x2,
        y2 - radius,
        x2,
        y2,
        x2 - radius,
        y2,
        x1 + radius,
        y2,
        x1,
        y2,
        x1,
        y2 - radius,
        x1,
        y1 + radius,
        x1,
        y1,
    ]
    return canvas.create_polygon(points, smooth=True, splinesteps=36, **kwargs)


def format_network_error(exc: BaseException) -> str:
    """Menschlich lesbare deutsche Meldung für Netzwerk-/HTTP-Fehler."""
    if isinstance(exc, req_exc.Timeout):
        return "Zeitüberschreitung — Server antwortet nicht rechtzeitig"
    if isinstance(exc, req_exc.SSLError):
        return f"SSL/TLS-Fehler: {exc}"
    if isinstance(exc, req_exc.ProxyError):
        return f"Proxy-Fehler: {exc}"
    if isinstance(exc, req_exc.HTTPError):
        status = exc.response.status_code if exc.response is not None else "?"
        reason = ""
        if exc.response is not None:
            reason = (exc.response.reason or "").strip()
        detail = f" {reason}" if reason else ""
        return f"HTTP-Fehler {status}{detail}"
    if isinstance(exc, req_exc.ConnectionError):
        cause = exc.__cause__ or exc.__context__
        text = str(cause or exc).lower()
        if isinstance(cause, ConnectionRefusedError) or "connection refused" in text:
            return "Verbindung abgelehnt — Server nicht erreichbar"
        if isinstance(cause, TimeoutError) or "timed out" in text:
            return "Zeitüberschreitung beim Verbindungsaufbau"
        if "name or service not known" in text or "getaddrinfo failed" in text:
            return "DNS-Fehler — Hostname nicht auflösbar"
        if "nodename nor servname" in text or "name resolution" in text:
            return "DNS-Fehler — Hostname nicht auflösbar"
        if "network is unreachable" in text:
            return "Netzwerk nicht erreichbar"
        if "temporary failure" in text:
            return "Temporärer DNS-/Netzwerkfehler"
        return f"Verbindungsfehler: {cause or exc}"
    if isinstance(exc, req_exc.ChunkedEncodingError):
        return "Übertragung abgebrochen — unvollständige Antwort"
    if isinstance(exc, req_exc.ContentDecodingError):
        return "Antwort konnte nicht dekodiert werden"
    if isinstance(exc, req_exc.TooManyRedirects):
        return "Zu viele Weiterleitungen"
    if isinstance(exc, req_exc.InvalidURL):
        return f"Ungültige URL: {exc}"
    if isinstance(exc, req_exc.RequestException):
        return f"Netzwerkfehler: {exc}"
    if isinstance(exc, json.JSONDecodeError):
        return "Ungültige JSON-Antwort vom Server"
    return str(exc)


def clear_cache_dir() -> int:
    """Löscht alle Dateien im Cache. Gibt die Anzahl gelöschter Einträge zurück."""
    if not CACHE_DIR.is_dir():
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        return 0
    removed = 0
    for path in CACHE_DIR.iterdir():
        try:
            if path.is_file() or path.is_symlink():
                path.unlink(missing_ok=True)
                removed += 1
            elif path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed


def open_with_default_app(path: Path) -> None:
    if not path.is_file():
        raise FileNotFoundError(f"Datei nicht gefunden: {path.name}")
    subprocess.run(["xdg-open", str(path.resolve())], check=False)


class ApiClient:
    def __init__(self, host_url: str, token: str) -> None:
        self.host_url = host_url
        self._lock = threading.Lock()
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.session.headers["Accept"] = "application/json"
        adapter = requests.adapters.HTTPAdapter(max_retries=0)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def _get_json(self, url: str, params: Optional[Dict[str, Any]] = None) -> Any:
        try:
            with self._lock:
                response = self.session.get(
                    url,
                    params=params,
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT_API),
                )
            response.raise_for_status()
        except req_exc.RequestException as exc:
            raise RuntimeError(format_network_error(exc)) from exc
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("Ungültige JSON-Antwort vom Server") from exc

    def fetch_depeschen(self, since: int) -> List[Dict[str, Any]]:
        payload = self._get_json(
            f"{self.host_url}/api/depeschen",
            params={"since": since},
        )
        if not isinstance(payload, list):
            raise ValueError("API hat keine JSON-Liste zurückgegeben")
        return payload

    def fetch_units(self) -> List[Dict[str, Any]]:
        payload = self._get_json(f"{self.host_url}/api/units")
        if not isinstance(payload, list):
            raise ValueError("API hat keine JSON-Liste zurückgegeben")
        return payload

    def download_pdf(self, pdf_link: str, dest: Path) -> None:
        url = urljoin(self.host_url + "/", pdf_link)
        try:
            with self._lock:
                response = self.session.get(
                    url,
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT_PDF),
                )
            response.raise_for_status()
        except req_exc.RequestException as exc:
            raise RuntimeError(format_network_error(exc)) from exc
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(response.content)


class Poller(threading.Thread):
    def __init__(
        self,
        client: ApiClient,
        since: int,
        interval: int,
        events: queue.Queue,
        stop_event: threading.Event,
    ) -> None:
        super().__init__(daemon=True, name="depeschen-poller")
        self.client = client
        self._since_lock = threading.Lock()
        self._since = since
        self.interval = interval
        self.events = events
        self.stop_event = stop_event

    @property
    def since(self) -> int:
        with self._since_lock:
            return self._since

    def set_since(self, value: int) -> None:
        with self._since_lock:
            self._since = value

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                items = self.client.fetch_depeschen(self.since)
                self.events.put(("poll_ok", items))
            except Exception as exc:
                self.events.put(("poll_error", format_network_error(exc)))
            if self.stop_event.wait(self.interval):
                break


class TableauWindow(tk.Toplevel):
    """Large status board for responsible units, ordered as in config."""

    def __init__(
        self,
        master: tk.Tk,
        units: List[Dict[str, str]],
        dark: bool,
        on_close,
    ) -> None:
        super().__init__(master)
        self.title(f"{APP_NAME} — Tableau")
        self.units = list(units)
        self._on_close = on_close
        self.protocol("WM_DELETE_WINDOW", self._handle_close)
        self.minsize(720, 320)

        bg = "#1e1e1e" if dark else "#f5f5f0"
        muted = "#9a9a9a" if dark else "#666666"
        self.configure(background=bg)
        self._bg = bg
        self._muted = muted
        self._flash_phase = 0.0
        self._flash_job: Optional[str] = None
        self._flash_period_ms = 700
        self._flash_tick_ms = 40
        # Share of the cycle spent fading up (short) vs down (longer)
        self._flash_rise_share = 0.32

        header = ttk.Frame(self, padding=(24, 16))
        header.pack(fill="x")
        ttk.Label(header, text="Tableau", style="Title.TLabel").pack(side="left")

        header_right = ttk.Frame(header)
        header_right.pack(side="right")
        self.status_dot = ttk.Label(
            header_right, text="●  Verbinden…", style="StatusConnecting.TLabel"
        )
        self.status_dot.pack(anchor="e")
        self.var_detail = tk.StringVar(value="Warte auf Status…")
        ttk.Label(
            header_right, textvariable=self.var_detail, style="Muted.TLabel"
        ).pack(anchor="e", pady=(4, 0))

        self.canvas = tk.Canvas(self, background=bg, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        self.canvas.bind("<Configure>", lambda _e: self._redraw())

        self._statuses: Dict[str, Any] = {u["unit"]: None for u in self.units}
        self._redraw()
        self._tick_flash()

    def _handle_close(self) -> None:
        self.dispose()
        self._on_close()

    def dispose(self) -> None:
        if self._flash_job is not None:
            try:
                self.after_cancel(self._flash_job)
            except Exception:
                pass
            self._flash_job = None

    def _set_status(self, kind: str, detail: str) -> None:
        styles = {
            "connecting": ("●  Verbinden…", "StatusConnecting.TLabel"),
            "online": ("●  Online", "StatusOnline.TLabel"),
            "offline": ("●  Offline", "StatusOffline.TLabel"),
        }
        text, style_name = styles.get(kind, ("●  Status", "Status.TLabel"))
        self.status_dot.configure(text=text, style=style_name)
        self.var_detail.set(detail)

    def _tick_flash(self) -> None:
        self._flash_phase = (
            self._flash_phase + self._flash_tick_ms / self._flash_period_ms
        ) % 1.0
        if any(status_should_flash(self._statuses.get(u["unit"])) for u in self.units):
            self._redraw()
        self._flash_job = self.after(self._flash_tick_ms, self._tick_flash)

    def update_units(self, units: List[Dict[str, Any]], error: str = "") -> None:
        by_name = {
            str(u.get("name") or "").strip(): u
            for u in units
            if isinstance(u, dict) and str(u.get("name") or "").strip()
        }
        for entry in self.units:
            unit = by_name.get(entry["unit"])
            self._statuses[entry["unit"]] = None if unit is None else unit.get("status")
        if error:
            self._set_status("offline", f"Abruf fehlgeschlagen: {error}")
        else:
            self._set_status("online", f"Aktualisiert {format_ts(time.time())}")
        self._redraw()

    def _flash_brightness(self) -> float:
        """Incandescent-style pulse: short fade-on, longer fade-off."""
        rise = self._flash_rise_share
        if self._flash_phase < rise:
            t = self._flash_phase / rise
            # Ease into full brightness quickly
            wave = t * t * (3.0 - 2.0 * t)
        else:
            t = (self._flash_phase - rise) / (1.0 - rise)
            # Softer, longer decay
            wave = (1.0 - t) ** 1.7
        return 0.16 + 0.84 * wave

    def _tile_color(self, status: Any) -> str:
        color = unit_status_color(status)
        if not status_should_flash(status):
            return color
        dim = 1.0 - self._flash_brightness()
        return mix_hex(color, self._bg, 0.12 + 0.78 * dim)

    def _redraw(self) -> None:
        self.canvas.delete("all")
        width = max(self.canvas.winfo_width(), 1)
        height = max(self.canvas.winfo_height(), 1)
        entries = self.units
        if not entries:
            self.canvas.create_text(
                width / 2,
                height / 2,
                text="Keine zuständigen Einheiten in der Config",
                fill=self._muted,
                font=("DejaVu Sans", 18),
            )
            return

        count = len(entries)
        cols = max(1, min(count, int(math.ceil(math.sqrt(count)))))
        rows = int(math.ceil(count / cols))
        cell_w = width / cols
        cell_h = height / rows
        pad_x = max(10.0, cell_w * 0.06)
        pad_y = max(10.0, cell_h * 0.08)

        for index, entry in enumerate(entries):
            row, col = divmod(index, cols)
            x1 = col * cell_w + pad_x
            y1 = row * cell_h + pad_y
            x2 = (col + 1) * cell_w - pad_x
            y2 = (row + 1) * cell_h - pad_y
            status = self._statuses.get(entry["unit"])
            color = self._tile_color(status)
            radius = min(28.0, (x2 - x1) * 0.12, (y2 - y1) * 0.12)
            rounded_rect(
                self.canvas,
                x1,
                y1,
                x2,
                y2,
                radius,
                fill=color,
                outline="",
            )
            if status_should_hatch(status):
                self._draw_hatch(x1, y1, x2, y2, radius)
            label = entry["label"]
            font_size = max(14, min(36, int(min(x2 - x1, y2 - y1) * 0.14)))
            font = ("DejaVu Sans", font_size, "bold")
            cx = (x1 + x2) / 2
            cy = (y1 + y2) / 2
            text_width = max(40, int(x2 - x1 - 24))
            for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1), (1, 1), (-1, 1)):
                self.canvas.create_text(
                    cx + dx,
                    cy + dy,
                    text=label,
                    fill="#000000",
                    font=font,
                    width=text_width,
                    justify="center",
                )
            self.canvas.create_text(
                cx,
                cy,
                text=label,
                fill="#ffffff",
                font=font,
                width=text_width,
                justify="center",
            )

    def _draw_hatch(
        self, x1: float, y1: float, x2: float, y2: float, radius: float
    ) -> None:
        inset = max(4.0, radius * 0.25)
        left, top, right, bottom = x1 + inset, y1 + inset, x2 - inset, y2 - inset
        spacing = max(12.0, min(right - left, bottom - top) * 0.12)
        line_w = max(3, int(spacing * 0.28))
        k = left + top
        k_max = right + bottom
        while k <= k_max:
            xa = max(left, k - bottom)
            xb = min(right, k - top)
            if xa < xb:
                self.canvas.create_line(
                    xa,
                    k - xa,
                    xb,
                    k - xb,
                    fill=STATUS_HATCH,
                    width=line_w,
                    capstyle=tk.ROUND,
                )
            k += spacing


class App(tk.Tk):
    def __init__(self, config: Dict[str, Any]) -> None:
        super().__init__()
        self.config_data = config
        self.started_at = int(time.time())
        self.client = ApiClient(config["host_url"], config["bearer_token"])
        self.events: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.seen_ids: set[str] = set()
        self.rows: Dict[str, Dict[str, Any]] = {}
        self.poll_count = 0
        self.print_count = 0
        self.last_poll: Optional[float] = None
        self.busy = False
        self.responsible_units: List[Dict[str, str]] = list(
            config["responsible_units"]
        )
        self.responsible_set = {u["unit"] for u in self.responsible_units}
        self.print_filter = config["print_filter"]
        self.tableau: Optional[TableauWindow] = None
        self._units_poll_job: Optional[str] = None
        self._units_fetching = False

        CACHE_DIR.mkdir(parents=True, exist_ok=True)

        self.title(APP_NAME)
        self.minsize(820, 520)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self._apply_forest_theme(config["theme"])
        self._build_ui()
        self._load_printers()

        self.poller = Poller(
            self.client,
            self.started_at,
            config["poll_interval_seconds"],
            self.events,
            self.stop_event,
        )
        self.poller.start()
        self.after(150, self._drain_events)
        self._set_status("connecting", "Warte auf ersten Abruf…")

    def should_auto_print(self, unit: str) -> bool:
        if self.print_filter == "none":
            return False
        if self.print_filter == "all":
            return True
        return unit in self.responsible_set

    def _apply_forest_theme(self, theme: str) -> None:
        tcl = THEME_DIR / f"{theme}.tcl"
        if not tcl.is_file():
            raise FileNotFoundError(f"Forest-Theme nicht gefunden: {tcl}")
        self.tk.call("source", str(tcl))
        style = ttk.Style(self)
        style.theme_use(theme)
        self.configure(background=style.lookup(".", "background") or "#313131")
        style.configure("Status.TLabel", font=("DejaVu Sans", 11, "bold"))
        style.configure("StatusOnline.TLabel", font=("DejaVu Sans", 11, "bold"), foreground="#4ee2a7")
        style.configure("StatusOffline.TLabel", font=("DejaVu Sans", 11, "bold"), foreground="#E24B4A")
        style.configure("StatusConnecting.TLabel", font=("DejaVu Sans", 11, "bold"), foreground="#e8a000")
        style.configure("Muted.TLabel", foreground="#b0b0b0")
        style.configure("Title.TLabel", font=("DejaVu Sans", 16, "bold"))
        style.configure("Switch", foreground="#b0b0b0")
        style.map(
            "Switch",
            foreground=[("selected", "#4ee2a7"), ("!selected", "#b0b0b0")],
        )

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=20)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        ttk.Label(header, text=APP_NAME, style="Title.TLabel").pack(side="left")
        header_right = ttk.Frame(header)
        header_right.pack(side="right")
        self.status_dot = ttk.Label(
            header_right, text="●  Verbinden…", style="StatusConnecting.TLabel"
        )
        self.status_dot.pack(anchor="e")
        self.var_tableau = tk.BooleanVar(value=False)
        self.tableau_switch = ttk.Checkbutton(
            header_right,
            text="Tableau",
            style="Switch",
            variable=self.var_tableau,
            command=self._on_tableau_toggled,
        )
        self.tableau_switch.pack(anchor="e", pady=(6, 0))

        status_card = ttk.LabelFrame(outer, text="Status", padding=(16, 12))
        status_card.grid(row=1, column=0, sticky="ew", pady=(0, 16))
        for col in range(4):
            status_card.columnconfigure(col, weight=1)

        self.var_host = tk.StringVar(value=self.config_data["host_url"])
        self.var_interval = tk.StringVar(
            value=f"{self.config_data['poll_interval_seconds']} s"
        )
        self.var_since = tk.StringVar(value=format_ts(self.started_at))
        self.var_last_poll = tk.StringVar(value="—")
        self.var_printed = tk.StringVar(value="0")
        self.var_polls = tk.StringVar(value="0")
        self.var_detail = tk.StringVar(value="")
        self.var_printer = tk.StringVar(value="")
        filter_label = {
            "none": "kein Auto-Druck",
            "all": "alle",
            "only_responsible": "nur zuständige",
        }.get(self.print_filter, self.print_filter)
        self.var_filter = tk.StringVar(value=filter_label)

        self._status_cell(status_card, 0, 0, "Host", self.var_host)
        self._status_cell(status_card, 0, 1, "Abrufintervall", self.var_interval)
        self._status_cell(status_card, 0, 2, "Aktiv seit", self.var_since)
        self._status_cell(status_card, 0, 3, "Letzter Abruf", self.var_last_poll)
        self._status_cell(status_card, 1, 0, "Abrufe", self.var_polls)
        self._status_cell(status_card, 1, 1, "Gedruckt (Sitzung)", self.var_printed)
        self._status_cell(status_card, 1, 2, "Druckfilter", self.var_filter)

        printer_box = ttk.Frame(status_card)
        printer_box.grid(row=1, column=3, sticky="ew", padx=8, pady=8)
        ttk.Label(printer_box, text="Drucker", style="Muted.TLabel").pack(anchor="w")
        self.printer_combo = ttk.Combobox(
            printer_box,
            textvariable=self.var_printer,
            state="readonly",
            width=36,
        )
        self.printer_combo.pack(fill="x", pady=(4, 0))
        self.printer_combo.bind("<<ComboboxSelected>>", self._on_printer_changed)

        ttk.Label(status_card, textvariable=self.var_detail, style="Muted.TLabel").grid(
            row=2, column=0, columnspan=4, sticky="w", padx=8, pady=(4, 0)
        )

        list_card = ttk.LabelFrame(
            outer, text="Zuletzt empfangene Depeschen", padding=(12, 10)
        )
        list_card.grid(row=2, column=0, sticky="nsew")
        list_card.columnconfigure(0, weight=1)
        list_card.rowconfigure(0, weight=1)

        tree_wrap = ttk.Frame(list_card)
        tree_wrap.grid(row=0, column=0, sticky="nsew")
        tree_wrap.columnconfigure(0, weight=1)
        tree_wrap.rowconfigure(0, weight=1)

        scroll = ttk.Scrollbar(tree_wrap)
        scroll.grid(row=0, column=1, sticky="ns")
        self.tree = ttk.Treeview(
            tree_wrap,
            columns=("time", "unit", "file", "result"),
            show="headings",
            selectmode="browse",
            yscrollcommand=scroll.set,
            height=12,
        )
        self.tree.grid(row=0, column=0, sticky="nsew")
        scroll.config(command=self.tree.yview)
        self.tree.heading("time", text="Zeit")
        self.tree.heading("unit", text="Einheit")
        self.tree.heading("file", text="PDF")
        self.tree.heading("result", text="Ergebnis")
        self.tree.column("time", width=170, anchor="w")
        self.tree.column("unit", width=160, anchor="w")
        self.tree.column("file", width=280, anchor="w")
        self.tree.column("result", width=160, anchor="w")
        self.tree.tag_configure("skipped", foreground="#7a7a7a")
        self.tree.bind("<Double-1>", lambda _e: self.open_selected())

        actions = ttk.Frame(list_card)
        actions.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        self.reset_btn = ttk.Button(
            actions,
            text="Reset",
            command=self.reset_session,
        )
        self.reset_btn.pack(side="left")
        self.reprint_btn = ttk.Button(
            actions,
            text="Auswahl drucken",
            style="Accent.TButton",
            command=self.reprint_selected,
        )
        self.reprint_btn.pack(side="right")

        ttk.Sizegrip(self).pack(side="bottom", anchor="se")

    def _status_cell(
        self, parent: ttk.Widget, row: int, col: int, label: str, variable: tk.StringVar
    ) -> None:
        cell = ttk.Frame(parent)
        cell.grid(row=row, column=col, sticky="ew", padx=8, pady=8)
        ttk.Label(cell, text=label, style="Muted.TLabel").pack(anchor="w")
        ttk.Label(cell, textvariable=variable).pack(anchor="w")

    def _load_printers(self) -> None:
        error = ""
        try:
            printers = printing.list_printers()
        except Exception as exc:
            printers = []
            error = str(exc)
        state = load_state()
        preferred = (
            str(state.get("printer") or "").strip()
            or str(self.config_data.get("printer") or "").strip()
            or printing.default_printer()
        )
        if preferred and preferred not in printers:
            printers = [preferred] + printers
        self.printer_combo["values"] = printers
        if preferred:
            self.var_printer.set(preferred)
        elif printers:
            self.var_printer.set(printers[0])
        if self.var_printer.get():
            save_state({**state, "printer": self.var_printer.get()})
        elif error:
            self.var_detail.set(f"Drucker konnten nicht geladen werden: {error}")
        else:
            self.var_detail.set(
                "Keine CUPS-Drucker gefunden. Drucker in CUPS anlegen "
                "oder in config.yml unter printer setzen."
            )

    def _on_printer_changed(self, _event: object = None) -> None:
        printer = self.var_printer.get().strip()
        if printer:
            save_state({**load_state(), "printer": printer})
            self.var_detail.set(f"Drucker gesetzt auf {printer}")

    def _on_tableau_toggled(self) -> None:
        if self.var_tableau.get():
            self._open_tableau()
        else:
            self._close_tableau()

    def _open_tableau(self) -> None:
        if self.tableau is not None and self.tableau.winfo_exists():
            self.tableau.lift()
            return
        dark = self.config_data["theme"] == "forest-dark"
        self.tableau = TableauWindow(
            self,
            self.responsible_units,
            dark=dark,
            on_close=self._close_tableau,
        )
        self.var_tableau.set(True)
        self._schedule_units_poll(immediate=True)

    def _close_tableau(self) -> None:
        if self._units_poll_job is not None:
            try:
                self.after_cancel(self._units_poll_job)
            except Exception:
                pass
            self._units_poll_job = None
        if self.tableau is not None:
            try:
                self.tableau.dispose()
                if self.tableau.winfo_exists():
                    self.tableau.destroy()
            except tk.TclError:
                pass
            self.tableau = None
        self.var_tableau.set(False)

    def _schedule_units_poll(self, immediate: bool = False) -> None:
        if self._units_poll_job is not None:
            try:
                self.after_cancel(self._units_poll_job)
            except Exception:
                pass
            self._units_poll_job = None
        delay = 0 if immediate else self.config_data["poll_interval_seconds"] * 1000
        self._units_poll_job = self.after(delay, self._units_poll_tick)

    def _units_poll_tick(self) -> None:
        self._units_poll_job = None
        if self.tableau is None or not self.tableau.winfo_exists():
            return
        if not self._units_fetching:
            self._units_fetching = True
            threading.Thread(
                target=self._fetch_units_worker,
                daemon=True,
                name="units-poll",
            ).start()
        self._schedule_units_poll(immediate=False)

    def _fetch_units_worker(self) -> None:
        try:
            units = self.client.fetch_units()
            self.after(0, self._on_units_ok, units)
        except Exception as exc:
            message = format_network_error(exc)
            self.after(0, self._on_units_error, message)
        finally:
            self._units_fetching = False

    def _on_units_ok(self, units: List[Dict[str, Any]]) -> None:
        if self.tableau is not None and self.tableau.winfo_exists():
            self.tableau.update_units(units)

    def _on_units_error(self, message: str) -> None:
        if self.tableau is not None and self.tableau.winfo_exists():
            self.tableau.update_units([], error=message)

    def _set_status(self, kind: str, detail: str) -> None:
        """Update connection indicator and optional detail line.

        kind is only the link state: connecting | online | offline.
        Activity (printing, reset, …) belongs in detail, not the indicator.
        """
        styles = {
            "connecting": ("●  Verbinden…", "StatusConnecting.TLabel"),
            "online": ("●  Online", "StatusOnline.TLabel"),
            "offline": ("●  Offline", "StatusOffline.TLabel"),
        }
        text, style_name = styles.get(kind, ("●  Status", "Status.TLabel"))
        self.status_dot.configure(text=text, style=style_name)
        self.var_detail.set(detail)

    def _set_detail(self, detail: str) -> None:
        self.var_detail.set(detail)

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "poll_ok":
                    self._on_poll_ok(payload)
                elif kind == "poll_error":
                    self.poll_count += 1
                    self.var_polls.set(str(self.poll_count))
                    self.last_poll = time.time()
                    self.var_last_poll.set(format_ts(self.last_poll))
                    self._set_status("offline", f"Abruf fehlgeschlagen: {payload}")
        except queue.Empty:
            pass
        if not self.stop_event.is_set():
            self.after(200, self._drain_events)

    def _on_poll_ok(self, items: List[Dict[str, Any]]) -> None:
        self.poll_count += 1
        self.last_poll = time.time()
        self.var_polls.set(str(self.poll_count))
        self.var_last_poll.set(format_ts(self.last_poll))
        new_items = []
        for item in items:
            if not isinstance(item, dict):
                continue
            iid = depesche_id(item)
            if iid in self.seen_ids:
                continue
            self.seen_ids.add(iid)
            new_items.append(item)
        if not new_items:
            self._set_status(
                "online", f"Abruf ok — {len(items)} bekannt, nichts Neues"
            )
            return
        printer = self.var_printer.get().strip()
        to_print = sum(
            1
            for item in new_items
            if self.should_auto_print(str(item.get("unit") or ""))
        )
        self._set_status(
            "online",
            f"Verarbeite {len(new_items)} neue Depesche(n)"
            f" ({to_print} Druck)…",
        )
        threading.Thread(
            target=self._print_batch,
            args=(new_items, printer),
            daemon=True,
            name="print-batch",
        ).start()

    def _print_batch(self, items: List[Dict[str, Any]], printer: str) -> None:
        for item in items:
            row = self._handle_item(item, printer)
            self.after(0, self._add_row, row)
        self.after(
            0,
            lambda: self._set_status(
                "online",
                f"Stapel fertig ({len(items)}). Letzter Abruf {format_ts(self.last_poll)}",
            ),
        )

    def _handle_item(self, item: Dict[str, Any], printer: str) -> Dict[str, Any]:
        pdf_link = str(item.get("pdf_link") or "")
        unit = str(item.get("unit") or "")
        ts = item.get("timestamp")
        filename = Path(pdf_link).name or "depesche.pdf"
        dest = CACHE_DIR / filename
        iid = depesche_id(item)
        auto_print = self.should_auto_print(unit)
        result = "gedruckt"
        error = ""
        skipped = False
        try:
            if not dest.is_file():
                self.client.download_pdf(pdf_link, dest)
            if not auto_print:
                result = "nicht gedruckt"
                skipped = True
            else:
                if not printer:
                    raise RuntimeError("Bitte zuerst einen Drucker wählen")
                printing.print_pdf(str(dest), printer)
        except Exception as exc:
            result = "fehlgeschlagen"
            error = format_network_error(exc)
        return {
            "id": iid,
            "timestamp": ts,
            "unit": unit,
            "pdf_link": pdf_link,
            "filename": filename,
            "path": str(dest),
            "result": result,
            "error": error,
            "skipped": skipped,
            "printed_at": time.time(),
        }

    def _add_row(self, row: Dict[str, Any]) -> None:
        self.rows[row["id"]] = row
        if row["result"] == "gedruckt":
            self.print_count += 1
        self.var_printed.set(str(self.print_count))
        result = row["result"]
        if row["error"]:
            result = f"fehlgeschlagen: {row['error']}"
        if self.tree.exists(row["id"]):
            self.tree.delete(row["id"])
        tags = ("skipped",) if row.get("skipped") else ()
        self.tree.insert(
            "",
            0,
            iid=row["id"],
            values=(
                format_ts(row.get("timestamp") or row.get("printed_at")),
                row["unit"],
                row["filename"],
                result,
            ),
            tags=tags,
        )
        children = self.tree.get_children()
        if len(children) > RECENT_LIMIT:
            for extra in children[RECENT_LIMIT:]:
                self.tree.delete(extra)
                self.rows.pop(extra, None)

    def open_selected(self) -> None:
        selected = self.tree.selection()
        if not selected:
            self.var_detail.set("Depesche zum Öffnen auswählen")
            return
        row = self.rows.get(selected[0])
        if not row:
            return
        try:
            open_with_default_app(Path(row["path"]))
            self._set_detail(f"Geöffnet: {row['filename']}")
        except Exception as exc:
            self._set_detail(f"Öffnen fehlgeschlagen: {exc}")

    def reset_session(self) -> None:
        now = int(time.time())
        self.poller.set_since(now)
        self.started_at = now
        self.seen_ids.clear()
        removed = clear_cache_dir()
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.rows.clear()
        self.print_count = 0
        self.var_printed.set("0")
        self.var_since.set(format_ts(now))
        self._set_detail(
            f"Reset — seit {format_ts(now)}, Cache geleert ({removed} Datei(en))"
        )

    def reprint_selected(self) -> None:
        selected = self.tree.selection()
        if not selected:
            self.var_detail.set("Depesche zum Drucken auswählen")
            return
        iid = selected[0]
        row = self.rows.get(iid)
        if not row:
            return
        printer = self.var_printer.get().strip()
        if not printer:
            self.var_detail.set("Bitte zuerst einen Drucker wählen")
            return
        self.reprint_btn.state(["disabled"])
        threading.Thread(
            target=self._reprint_worker,
            args=(row, printer),
            daemon=True,
            name="reprint",
        ).start()

    def _reprint_worker(self, row: Dict[str, Any], printer: str) -> None:
        dest = Path(row["path"])
        try:
            if not dest.is_file():
                self.client.download_pdf(row["pdf_link"], dest)
            printing.print_pdf(str(dest), printer)
            self.after(
                0,
                lambda: self._finish_reprint(row["id"], "gedruckt", ""),
            )
        except Exception as exc:
            message = format_network_error(exc)
            self.after(
                0,
                lambda m=message: self._finish_reprint(row["id"], "fehlgeschlagen", m),
            )

    def _finish_reprint(self, iid: str, result: str, error: str) -> None:
        self.reprint_btn.state(["!disabled"])
        display = result if not error else f"fehlgeschlagen: {error}"
        row = self.rows.get(iid)
        if row is not None:
            row["result"] = result
            row["error"] = error
            row["skipped"] = False
        if self.tree.exists(iid):
            values = list(self.tree.item(iid, "values"))
            if len(values) >= 4:
                values[3] = display
                self.tree.item(iid, values=values, tags=())
        if error:
            self._set_detail(f"Druck fehlgeschlagen: {error}")
        else:
            self._set_detail("Gedruckt")
            self.print_count += 1
            self.var_printed.set(str(self.print_count))

    def on_close(self) -> None:
        self.stop_event.set()
        self._close_tableau()
        self.destroy()


def main() -> None:
    try:
        config = load_config()
    except Exception as exc:
        root = tk.Tk()
        root.title(APP_NAME)
        root.geometry("560x160")
        ttk.Label(root, text=str(exc), wraplength=520, padding=20).pack()
        ttk.Button(root, text="Beenden", command=root.destroy).pack(pady=10)
        root.mainloop()
        raise SystemExit(1) from exc
    app = App(config)
    app.mainloop()


if __name__ == "__main__":
    main()
