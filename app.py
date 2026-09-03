#!/usr/bin/env python3
"""Holt /api/depeschen und druckt neue PDFs. Status-Oberfläche im Forest-Theme."""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk
from typing import Any, Dict, List, Optional
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
    return {
        "host_url": host,
        "bearer_token": token,
        "poll_interval_seconds": interval,
        "printer": str(data.get("printer") or "").strip(),
        "theme": theme,
    }


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
    target = str(path.resolve())
    if sys.platform == "win32":
        os.startfile(target)  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.run(["open", target], check=False)
    else:
        subprocess.run(["xdg-open", target], check=False)


class ApiClient:
    def __init__(self, host_url: str, token: str) -> None:
        self.host_url = host_url
        self._lock = threading.Lock()
        self.session = requests.Session()
        self.session.headers["Authorization"] = f"Bearer {token}"
        self.session.headers["Accept"] = "application/json"
        # Keine endlosen Hänger bei defekten Verbindungen
        adapter = requests.adapters.HTTPAdapter(max_retries=0)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def fetch_depeschen(self, since: int) -> List[Dict[str, Any]]:
        url = f"{self.host_url}/api/depeschen"
        try:
            with self._lock:
                response = self.session.get(
                    url,
                    params={"since": since},
                    timeout=(CONNECT_TIMEOUT, READ_TIMEOUT_API),
                )
            response.raise_for_status()
        except req_exc.RequestException as exc:
            raise RuntimeError(format_network_error(exc)) from exc
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise RuntimeError("Ungültige JSON-Antwort vom Server") from exc
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
                # Jeder Fehler (Netzwerk, Timeout, …) — Poller läuft weiter
                self.events.put(("poll_error", format_network_error(exc)))
            if self.stop_event.wait(self.interval):
                break


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
        self._set_status("waiting", "Warte auf ersten Abruf…")

    def _apply_forest_theme(self, theme: str) -> None:
        tcl = THEME_DIR / f"{theme}.tcl"
        if not tcl.is_file():
            raise FileNotFoundError(f"Forest-Theme nicht gefunden: {tcl}")
        self.tk.call("source", str(tcl))
        style = ttk.Style(self)
        style.theme_use(theme)
        self.configure(background=style.lookup(".", "background") or "#313131")
        style.configure("Status.TLabel", font=("Segoe UI", 11, "bold"))
        style.configure("Muted.TLabel", foreground="#b0b0b0")
        style.configure("Title.TLabel", font=("Segoe UI", 16, "bold"))

    def _build_ui(self) -> None:
        outer = ttk.Frame(self, padding=20)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(2, weight=1)

        header = ttk.Frame(outer)
        header.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        ttk.Label(header, text=APP_NAME, style="Title.TLabel").pack(side="left")
        self.status_dot = ttk.Label(header, text="●  startet", style="Status.TLabel")
        self.status_dot.pack(side="right")

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

        self._status_cell(status_card, 0, 0, "Host", self.var_host)
        self._status_cell(status_card, 0, 1, "Abrufintervall", self.var_interval)
        self._status_cell(status_card, 0, 2, "Aktiv seit", self.var_since)
        self._status_cell(status_card, 0, 3, "Letzter Abruf", self.var_last_poll)
        self._status_cell(status_card, 1, 0, "Abrufe", self.var_polls)
        self._status_cell(status_card, 1, 1, "Gedruckt (Sitzung)", self.var_printed)

        printer_box = ttk.Frame(status_card)
        printer_box.grid(row=1, column=2, columnspan=2, sticky="ew", padx=8, pady=8)
        ttk.Label(printer_box, text="Drucker", style="Muted.TLabel").pack(anchor="w")
        self.printer_combo = ttk.Combobox(
            printer_box,
            textvariable=self.var_printer,
            state="readonly",
            width=48,
        )
        self.printer_combo.pack(fill="x", pady=(4, 0))
        self.printer_combo.bind("<<ComboboxSelected>>", self._on_printer_changed)

        ttk.Label(status_card, textvariable=self.var_detail, style="Muted.TLabel").grid(
            row=2, column=0, columnspan=4, sticky="w", padx=8, pady=(4, 0)
        )

        list_card = ttk.LabelFrame(
            outer, text="Zuletzt gedruckte Depeschen", padding=(12, 10)
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
        self.tree.column("result", width=140, anchor="w")
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
            text="Auswahl erneut drucken",
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
        try:
            printers = printing.list_printers()
        except Exception as exc:
            printers = []
            self.var_detail.set(f"Drucker konnten nicht geladen werden: {exc}")
        self.printer_combo["values"] = printers
        state = load_state()
        preferred = (
            str(state.get("printer") or "").strip()
            or self.config_data.get("printer")
            or printing.default_printer()
        )
        if preferred and preferred in printers:
            self.var_printer.set(preferred)
        elif printers:
            self.var_printer.set(printers[0])
        if self.var_printer.get():
            save_state({**state, "printer": self.var_printer.get()})

    def _on_printer_changed(self, _event: object = None) -> None:
        printer = self.var_printer.get().strip()
        if printer:
            save_state({**load_state(), "printer": printer})
            self.var_detail.set(f"Drucker gesetzt auf {printer}")

    def _set_status(self, kind: str, detail: str) -> None:
        labels = {
            "ok": "●  online",
            "error": "●  Fehler",
            "waiting": "●  wartend",
            "printing": "●  druckt",
        }
        self.status_dot.configure(text=labels.get(kind, "●  Status"))
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
                    self._set_status("error", f"Abruf fehlgeschlagen: {payload}")
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
                "ok", f"Abruf ok — {len(items)} bekannt, nichts Neues"
            )
            return
        printer = self.var_printer.get().strip()
        self._set_status(
            "printing", f"Drucke {len(new_items)} neue Depesche(n)…"
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
                "ok",
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
        result = "gedruckt"
        error = ""
        try:
            if not printer:
                raise RuntimeError("Bitte zuerst einen Drucker wählen")
            if not dest.is_file():
                self.client.download_pdf(pdf_link, dest)
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
            self.var_detail.set(f"Geöffnet: {row['filename']}")
        except Exception as exc:
            self._set_status("error", f"Öffnen fehlgeschlagen: {exc}")

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
        self._set_status(
            "waiting",
            f"Reset — seit {format_ts(now)}, Cache geleert ({removed} Datei(en))",
        )

    def reprint_selected(self) -> None:
        selected = self.tree.selection()
        if not selected:
            self.var_detail.set("Depesche zum erneuten Drucken auswählen")
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
                lambda: self._finish_reprint(row["id"], "erneut gedruckt", ""),
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
        if self.tree.exists(iid):
            values = list(self.tree.item(iid, "values"))
            if len(values) >= 4:
                values[3] = display
                self.tree.item(iid, values=values)
        if error:
            self._set_status("error", f"Erneuter Druck fehlgeschlagen: {error}")
        else:
            self._set_status("ok", "Erneut gedruckt")
            self.print_count += 1
            self.var_printed.set(str(self.print_count))

    def on_close(self) -> None:
        self.stop_event.set()
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
