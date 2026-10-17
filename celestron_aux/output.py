"""Where traffic goes: the console, a CSV log and a raw capture file."""

from __future__ import annotations

import csv
import threading
from pathlib import Path
from typing import IO, Any, List, Optional, Sequence, Tuple

from . import catalog
from .bus import BusEvent, Direction
from .catalog import Addr
from .decoding import describe_packet
from .protocol import AuxPacket, CorruptFrame, StarSenseFrame
from .starsense import parse_star_list

_NAME_WIDTH = 21


def _named(name: str, address: int) -> str:
    return f"{name:<{_NAME_WIDTH}} (0x{address:02x})"


class ConsoleView:
    """Prints bus traffic and status messages. Safe to use from any thread."""

    def __init__(self, stream: IO[str], verbose: bool = False, enabled: bool = True) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self.verbose = verbose
        self.enabled = enabled  # False silences all console output (used by the web interface)

    # --- bus listener ---------------------------------------------------------

    def __call__(self, event: BusEvent) -> None:
        if self.enabled:
            self.lines(self.format_event(event))

    def format_event(self, event: BusEvent) -> List[str]:
        stamp = f"{event.timestamp:14.6f}"
        dump = f" --- {event.raw.hex()}" if self.verbose else ""
        frame = event.frame
        if isinstance(frame, AuxPacket):
            d = describe_packet(frame)
            return [
                f"{stamp} - {_named(d.sender, d.sender_id)} -> {_named(d.receiver, d.receiver_id)}"
                f" --- {_named(d.command, d.command_id)} --- {d.value}{dump}"
            ]
        if isinstance(frame, StarSenseFrame):
            stars = parse_star_list(frame.payload)
            lines = [f"{stamp} - Starsense HC data - {len(stars)} stars{dump}"]
            lines += [
                f"{'':17}Star {index:3d} - x {star.x:8.2f}  y {star.y:8.2f}"
                for index, star in enumerate(stars, start=1)
            ]
            return lines
        if isinstance(frame, CorruptFrame):
            return [f"{stamp} - corrupt frame ({frame.reason}): {frame.raw.hex()}"]
        return [f"{stamp} - unrecognised event {frame!r}"]

    # --- status output ----------------------------------------------------------

    def lines(self, lines: Sequence[str]) -> None:
        if not self.enabled:
            return
        with self._lock:
            for line in lines:
                print(line, file=self._stream)
            self._stream.flush()

    def message(self, text: str) -> None:
        self.lines([text])

    def banner(self, title: str) -> None:
        rule = "-" * max(23, len(title) + 2)
        self.lines([rule, f" {title}", rule])

    def traffic_header(self) -> None:
        self.message(
            f"{'Timestamp':>14} - {'Sender':<{_NAME_WIDTH}} {'Hex':<6} -> "
            f"{'Receiver':<{_NAME_WIDTH}} {'Hex':<6} --- {'Command':<{_NAME_WIDTH}} {'Hex':<6} --- Value"
        )

    def device_table(self, devices: List[Tuple[int, str]], mount_model: Optional[int]) -> None:
        self.banner(f"Mount: {catalog.mount_name(mount_model)}")
        self.banner("Detected devices")
        if not devices:
            self.message("(none)")
        self.lines([
            f"{index:2d}) {_named(catalog.device_name(address), address)} - {version}"
            for index, (address, version) in enumerate(devices)
        ])


CSV_HEADER = (
    "timestamp", "sender", "sender_id", "receiver", "receiver_id",
    "command", "command_id", "command_data", "raw_packet",
)


class CsvLogger:
    """Bus listener that appends one CSV row per frame while active."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._file: Optional[IO[str]] = None
        self._writer: Optional[Any] = None  # csv writer objects have no public type
        self.path: Optional[Path] = None

    @property
    def active(self) -> bool:
        return self._file is not None

    def start(self, path: Path) -> None:
        """Start a fresh log at ``path`` (overwritten) with a header row."""
        with self._lock:
            self._close_locked()
            self._file = path.open("w", newline="", encoding="utf-8")
            self._writer = csv.writer(self._file)
            self._writer.writerow(CSV_HEADER)
            self._file.flush()
            self.path = path

    def stop(self) -> None:
        with self._lock:
            self._close_locked()

    def __call__(self, event: BusEvent) -> None:
        with self._lock:
            if self._writer is None or self._file is None:
                return
            self._writer.writerow(self.row_for(event))
            self._file.flush()

    @staticmethod
    def row_for(event: BusEvent) -> Tuple[str, ...]:
        stamp = f"{event.timestamp:.6f}"
        frame = event.frame
        if isinstance(frame, AuxPacket):
            d = describe_packet(frame)
            return (stamp, d.sender, f"0x{d.sender_id:02x}", d.receiver, f"0x{d.receiver_id:02x}",
                    d.command, f"0x{d.command_id:02x}", d.value, event.raw.hex())
        if isinstance(frame, StarSenseFrame):
            stars = len(parse_star_list(frame.payload))
            return (stamp, catalog.device_name(Addr.STARSENSE_CAMERA), f"0x{Addr.STARSENSE_CAMERA:02x}",
                    "All", "", "STARSENSE_STAR_LIST", "", f"{stars} stars", event.raw.hex())
        reason = frame.reason if isinstance(frame, CorruptFrame) else ""
        return (stamp, "", "", "", "", "CORRUPT_FRAME", "", reason, event.raw.hex())

    def _close_locked(self) -> None:
        if self._file is not None:
            self._file.close()
        self._file = None
        self._writer = None


class RawRecorder:
    """Raw tap that records every chunk sent or received, replayable with playback mode."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._file: Optional[IO[str]] = None
        self.path: Optional[Path] = None

    @property
    def active(self) -> bool:
        return self._file is not None

    def start(self, path: Path) -> None:
        with self._lock:
            self._close_locked()
            self._file = path.open("w", encoding="ascii")
            self.path = path

    def stop(self) -> None:
        with self._lock:
            self._close_locked()

    def __call__(self, direction: Direction, raw: bytes, timestamp: float) -> None:
        with self._lock:
            if self._file is None:
                return
            self._file.write(f"{timestamp:.6f} {raw.hex()}\n")
            self._file.flush()

    def _close_locked(self) -> None:
        if self._file is not None:
            self._file.close()
        self._file = None
