"""Multicast UDP relay control for physical tableau lights."""

from __future__ import annotations

import json
import socket
import threading
import time
from typing import Any, Dict, List, Optional, Union

RELAY_MCAST_GROUP = "239.255.255.250"
RELAY_MCAST_PORT = 49152
RELAY_APPLICATION = "2426hadp_tabl_relay"
RELAY_SEND_INTERVAL_S = 0.1
RELAY_KEEPALIVE_S = 10.0

# Status A: shared global beat — 0.7 s on, 0.7 s off
STATUS_A_ON_S = 0.7
STATUS_A_PERIOD_S = 1.4

# Status 0 / 5: 3 s on → 0.5 off → 0.5 on → 0.5 off
STATUS_05_PATTERN = (
    (3.0, True),
    (0.5, False),
    (0.5, True),
    (0.5, False),
)
STATUS_05_PERIOD_S = sum(duration for duration, _ in STATUS_05_PATTERN)

ALWAYS_ON = {1, 3, 4}
ALWAYS_OFF = {2, 6}


def _normalize_status(raw: Any) -> Union[int, str, None]:
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


def relay_on_for_status(status: Any, elapsed: float) -> bool:
    """Return whether the relay should be on at elapsed seconds for this status."""
    normalized = _normalize_status(status)
    if normalized is None:
        return False
    if normalized in ALWAYS_ON:
        return True
    if normalized in ALWAYS_OFF:
        return False
    if normalized == "A":
        return (elapsed % STATUS_A_PERIOD_S) < STATUS_A_ON_S
    if normalized in (0, 5):
        t = elapsed % STATUS_05_PERIOD_S
        for duration, on in STATUS_05_PATTERN:
            if t < duration:
                return on
            t -= duration
        return False
    return False


class RelayMulticastSender:
    """While running, periodically multicasts on/off for every tableau unit label."""

    def __init__(self, labels: List[str]) -> None:
        self._labels = list(labels)
        self._statuses: Dict[str, Any] = {label: None for label in self._labels}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sock: Optional[socket.socket] = None
        self._t0 = 0.0
        self._last_units: Optional[Dict[str, bool]] = None
        self._last_sent_at: Optional[float] = None

    def set_statuses(self, by_label: Dict[str, Any]) -> None:
        with self._lock:
            for label in self._labels:
                self._statuses[label] = by_label.get(label)

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._t0 = time.monotonic()
        self._last_units = None
        self._last_sent_at = None
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        self._sock = sock
        self._thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="relay-multicast",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None:
            thread.join(timeout=2.0)
            self._thread = None
        self._send_all(False)
        sock = self._sock
        self._sock = None
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def _run(self) -> None:
        while not self._stop.wait(RELAY_SEND_INTERVAL_S):
            self._send_current()

    def _send_current(self) -> None:
        elapsed = time.monotonic() - self._t0
        with self._lock:
            units = {
                label: relay_on_for_status(self._statuses.get(label), elapsed)
                for label in self._labels
            }
        self._emit(units)

    def _send_all(self, on: bool) -> None:
        self._emit({label: on for label in self._labels})

    def _emit(self, units: Dict[str, bool]) -> None:
        now = time.monotonic()
        unchanged = self._last_units is not None and self._last_units == units
        if unchanged:
            if (
                self._last_sent_at is None
                or now - self._last_sent_at < RELAY_KEEPALIVE_S
            ):
                return
        payload = json.dumps(
            {"application": RELAY_APPLICATION, "units": units},
            separators=(",", ":"),
        ).encode("utf-8")
        sock = self._sock
        if sock is not None:
            try:
                sock.sendto(payload, (RELAY_MCAST_GROUP, RELAY_MCAST_PORT))
            except OSError:
                return
            self._last_units = dict(units)
            self._last_sent_at = now
            return
        try:
            with socket.socket(
                socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP
            ) as oneshot:
                oneshot.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
                oneshot.sendto(payload, (RELAY_MCAST_GROUP, RELAY_MCAST_PORT))
            self._last_units = dict(units)
            self._last_sent_at = now
        except OSError:
            pass
