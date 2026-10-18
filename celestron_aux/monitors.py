"""Status panels for the GPS and the focuser.

Each monitor learns from every reply its device sends, so values the hand
controller asks for show up too, and can also poll the device periodically.
Polling is off until enabled and never happens in listen-only mode.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .bus import AuxBus, BusEvent
from .catalog import Addr, Cmd
from .protocol import AuxPacket
from .protocol.units import bam_to_degrees, format_dms

log = logging.getLogger(__name__)

_TICK = 0.1


class DeviceMonitor:
    """Bus listener for one device, with optional periodic polling."""

    address: int = 0
    interval: float = 1.0

    def __init__(self, bus: AuxBus, *, enabled: bool = False,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._bus = bus
        self._clock = clock
        self._lock = threading.Lock()
        self._enabled = enabled
        self._updated: Optional[float] = None
        self._next_poll = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"aux-monitor-{self.address:02x}", daemon=True)

    # --- what subclasses provide ---------------------------------------------

    def requests(self) -> List[Tuple[int, bytes]]:
        """(command, data) pairs to send on each poll."""
        raise NotImplementedError

    def update(self, command: int, data: bytes) -> bool:
        """Record a reply from the device; return True if it was used. Called under the lock."""
        raise NotImplementedError

    def values(self) -> Dict[str, Any]:
        """The current readings for display. Called under the lock."""
        raise NotImplementedError

    def on_enable(self) -> None:
        """Called (under the lock) when polling is switched on."""

    # --- shared behaviour -------------------------------------------------------

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        with self._lock:
            if value and not self._enabled:
                self._next_poll = 0.0  # poll straight away
                self.on_enable()
            self._enabled = value

    def __call__(self, event: BusEvent) -> None:
        packet = event.frame
        if not isinstance(packet, AuxPacket) or packet.source != self.address or not packet.data:
            return
        with self._lock:
            if self.update(packet.command, packet.data):
                self._updated = self._clock()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            age = None if self._updated is None else round(self._clock() - self._updated, 1)
            return {"enabled": self._enabled, "age": age, **self.values()}

    def poll(self) -> None:
        for command, data in self.requests():
            if not self._bus.send(AuxPacket(Addr.SCANNER, self.address, command, data)):
                return

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(_TICK):
            now = self._clock()
            with self._lock:
                due = self._enabled and now >= self._next_poll
                if due:
                    self._next_poll = now + self.interval
            if due and not self._bus.listen_only:
                self.poll()


class GpsMonitor(DeviceMonitor):
    """Link state, fix position, UTC date and time, and satellite information."""

    address = Addr.GPS
    interval = 5.0

    GPS_GET_SAT_INFO = 0x07

    def __init__(self, bus: AuxBus, **kwargs: Any) -> None:
        super().__init__(bus, **kwargs)
        self._linked: Optional[bool] = None
        self._time_valid: Optional[bool] = None
        self._latitude: Optional[float] = None
        self._longitude: Optional[float] = None
        self._time: Optional[Tuple[int, int, int]] = None
        self._date: Optional[Tuple[int, int]] = None
        self._year: Optional[int] = None
        self._satellites: Optional[Tuple[int, int]] = None

    def requests(self) -> List[Tuple[int, bytes]]:
        return [(command, b"") for command in (
            Cmd.GPS_LINKED, Cmd.GPS_TIME_VALID, Cmd.GPS_GET_LAT, Cmd.GPS_GET_LONG,
            Cmd.GPS_GET_YEAR, Cmd.GPS_GET_DATE, Cmd.GPS_GET_TIME, self.GPS_GET_SAT_INFO,
        )]

    def update(self, command: int, data: bytes) -> bool:
        if command == Cmd.GPS_LINKED:
            self._linked = bool(data[0])
        elif command == Cmd.GPS_TIME_VALID:
            self._time_valid = bool(data[0])
        elif command == Cmd.GPS_GET_LAT and len(data) >= 3:
            self._latitude = bam_to_degrees(data[:3])
        elif command == Cmd.GPS_GET_LONG and len(data) >= 3:
            self._longitude = bam_to_degrees(data[:3])
        elif command == Cmd.GPS_GET_TIME and len(data) >= 3:
            self._time = (data[0], data[1], data[2])
        elif command == Cmd.GPS_GET_DATE and len(data) >= 2:
            self._date = (data[0], data[1])
        elif command == Cmd.GPS_GET_YEAR:
            self._year = int.from_bytes(data, "big")
        elif command == self.GPS_GET_SAT_INFO and len(data) >= 2:
            self._satellites = (data[0], data[1])
        else:
            return False
        return True

    def values(self) -> Dict[str, Any]:
        return {
            "linked": self._linked,
            "time_valid": self._time_valid,
            "latitude": _angle(self._latitude),
            "longitude": _angle(self._longitude),
            "utc": self._utc_text(),
            "satellites": None if self._satellites is None else list(self._satellites),
        }

    def _utc_text(self) -> Optional[str]:
        date = time_text = None
        if self._date is not None:
            month, day = self._date
            date = f"{self._year:04d}-{month:02d}-{day:02d}" if self._year else f"{month:02d}-{day:02d}"
        if self._time is not None:
            time_text = "{:02d}:{:02d}:{:02d}".format(*self._time)
        parts = [part for part in (date, time_text) if part]
        return " ".join(parts) if parts else None


class FocuserMonitor(DeviceMonitor):
    """Position, whether a move is in progress, and the calibrated travel limits."""

    address = Addr.FOCUSER
    interval = 1.0

    FOCUS_GET_POSITION = 0x01
    FOCUS_SLEW_DONE = 0x13
    FOCUS_GET_LIMITS = 0x2C

    def __init__(self, bus: AuxBus, **kwargs: Any) -> None:
        super().__init__(bus, **kwargs)
        self._position: Optional[int] = None
        self._moving: Optional[bool] = None
        self._limits: Optional[Tuple[int, int]] = None
        self._ask_limits = True

    def on_enable(self) -> None:
        self._ask_limits = True  # the limits rarely change: read them once per enable

    def requests(self) -> List[Tuple[int, bytes]]:
        commands = [self.FOCUS_GET_POSITION, self.FOCUS_SLEW_DONE]
        with self._lock:
            if self._ask_limits:
                self._ask_limits = False
                commands.append(self.FOCUS_GET_LIMITS)
        return [(command, b"") for command in commands]

    def update(self, command: int, data: bytes) -> bool:
        if command == self.FOCUS_GET_POSITION and len(data) >= 3:
            self._position = int.from_bytes(data[:3], "big")
        elif command == self.FOCUS_SLEW_DONE:
            self._moving = data[0] != 0xFF
        elif command == self.FOCUS_GET_LIMITS and len(data) >= 8:
            self._limits = (int.from_bytes(data[0:4], "big"), int.from_bytes(data[4:8], "big"))
        else:
            return False
        return True

    def values(self) -> Dict[str, Any]:
        return {
            "position": self._position,
            "moving": self._moving,
            "limits": None if self._limits is None else list(self._limits),
        }


def _angle(degrees: Optional[float]) -> Optional[Dict[str, Any]]:
    if degrees is None:
        return None
    return {"degrees": round(degrees, 6), "dms": format_dms(degrees)}
