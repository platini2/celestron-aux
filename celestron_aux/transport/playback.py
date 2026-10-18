"""Replays a raw capture file as if it were a live connection.

Capture format: one chunk per line, either ``<seconds> <hex>`` or just ``<hex>``.
Timestamped lines are replayed with their original spacing; lines without a
timestamp are delivered immediately. ``#MARK <seconds> <text>`` lines are marks
placed during recording; other lines starting with ``#`` are comments.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Iterator, List, Optional, Tuple, Union

from .base import Transport, TransportClosed

log = logging.getLogger(__name__)

MARK_PREFIX = "#MARK"
_Entry = Tuple[Optional[float], Union[bytes, str]]  # bytes are traffic, str is a mark


def load_capture(path: Path) -> List[_Entry]:
    entries: List[_Entry] = []
    with path.open("r", encoding="ascii") as capture:
        for line_number, line in enumerate(capture, start=1):
            if line.startswith(MARK_PREFIX):
                parts = line[len(MARK_PREFIX):].split(maxsplit=1)
                try:
                    entries.append((float(parts[0]), parts[1].strip() if len(parts) > 1 else ""))
                except (ValueError, IndexError) as exc:
                    raise ValueError(f"{path}:{line_number}: expected '#MARK <seconds> <text>'") from exc
                continue
            if line.startswith("#"):
                continue
            fields = line.split()
            try:
                if len(fields) == 2:
                    entries.append((float(fields[0]), bytes.fromhex(fields[1])))
                elif len(fields) == 1:
                    entries.append((None, bytes.fromhex(fields[0])))
                elif fields:
                    raise ValueError("expected '<seconds> <hex>' or '<hex>'")
            except ValueError as exc:
                raise ValueError(f"{path}:{line_number}: {exc}") from exc
    return entries


class PlaybackTransport(Transport):
    echoes_tx = False

    def __init__(self, path: Path, speed: float = 1.0, poll_interval: float = 0.05,
                 name: Optional[str] = None) -> None:
        super().__init__(poll_interval)
        if speed <= 0:
            raise ValueError("speed must be positive")
        self.path = path
        self.name = name or str(path)
        self.speed = speed
        self._entries: Iterator[_Entry] = iter(())
        self._pending: Optional[_Entry] = None
        self._started_at = 0.0
        self._first_timestamp: Optional[float] = None
        self._recorded: Optional[float] = None
        self._marks: List[Tuple[float, str]] = []

    @property
    def description(self) -> str:
        return f"playback {self.name}"

    def open(self) -> None:
        self._entries = iter(load_capture(self.path))
        self._pending = None
        self._first_timestamp = None
        self._started_at = time.monotonic()

    def close(self) -> None:
        self._entries = iter(())

    def read(self) -> bytes:
        if self._pending is None:
            self._pending = next(self._entries, None)
            if self._pending is None:
                raise TransportClosed(f"end of capture file {self.name}")
        timestamp, data = self._pending
        delay = self._due_time(timestamp) - time.monotonic()
        if delay > 0:
            time.sleep(min(delay, self.poll_interval))
            if delay > self.poll_interval:
                return b""
        self._pending = None
        if timestamp is not None and self._first_timestamp is not None:
            self._recorded = timestamp - self._first_timestamp
        else:
            self._recorded = None
        if isinstance(data, str):  # a mark: handed over through take_marks
            if data:
                self._marks.append((self._recorded or 0.0, data))
            return b""
        return data

    def take_marks(self) -> List[Tuple[float, str]]:
        marks, self._marks = self._marks, []
        return marks

    def recorded_time(self) -> Optional[float]:
        return self._recorded

    def write(self, data: bytes) -> None:
        log.debug("playback mode: not sending %s", data.hex())

    def _due_time(self, timestamp: Optional[float]) -> float:
        if timestamp is None:
            return 0.0
        if self._first_timestamp is None:
            self._first_timestamp = timestamp
        return self._started_at + (timestamp - self._first_timestamp) / self.speed
