"""Answers GPS queries on behalf of a GPS module that is not there.

Useful for mounts whose hand controller waits for a GPS fix: the emulator
replies with a fixed location and the computer's UTC clock.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone
from typing import Callable, Optional, Tuple

from .bus import BusEvent
from .catalog import Addr, Cmd
from .protocol import AuxPacket
from .protocol.units import degrees_to_bam

FIRMWARE_VERSION = bytes((11, 1))


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class GpsEmulator:
    """Bus listener; replies only while enabled."""

    def __init__(
        self,
        send: Callable[[AuxPacket], object],
        now: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._send = send
        self._now = now
        self._lock = threading.Lock()
        self._position: Optional[Tuple[float, float]] = None

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._position is not None

    def enable(self, latitude: float, longitude: float) -> None:
        if not -90.0 <= latitude <= 90.0:
            raise ValueError("latitude must be between -90 and 90")
        if not -180.0 <= longitude <= 180.0:
            raise ValueError("longitude must be between -180 and 180")
        with self._lock:
            self._position = (latitude, longitude)

    def disable(self) -> None:
        with self._lock:
            self._position = None

    def __call__(self, event: BusEvent) -> None:
        request = event.frame
        if not isinstance(request, AuxPacket):
            return
        if request.destination != Addr.GPS or request.source == Addr.GPS:
            return
        reply = self.reply_to(request)
        if reply is not None:
            self._send(reply)

    def reply_to(self, request: AuxPacket) -> Optional[AuxPacket]:
        """Build the reply for a GPS request, or None if disabled or unsupported."""
        with self._lock:
            position = self._position
        if position is None:
            return None
        data = self._reply_data(request.command, position)
        if data is None:
            return None
        return AuxPacket(Addr.GPS, request.source, request.command, data)

    def _reply_data(self, command: int, position: Tuple[float, float]) -> Optional[bytes]:
        latitude, longitude = position
        if command in (Cmd.GPS_TIME_VALID, Cmd.GPS_LINKED):
            return b"\x01"
        if command == Cmd.GET_VERSION:
            return FIRMWARE_VERSION
        if command == Cmd.GPS_GET_LAT:
            return degrees_to_bam(latitude)
        if command == Cmd.GPS_GET_LONG:
            return degrees_to_bam(longitude)
        now = self._now()
        if command == Cmd.GPS_GET_TIME:
            return bytes((now.hour, now.minute, now.second))
        if command == Cmd.GPS_GET_DATE:
            return bytes((now.month, now.day))
        if command == Cmd.GPS_GET_YEAR:
            return now.year.to_bytes(2, "big")
        return None
