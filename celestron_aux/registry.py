"""Tracks which devices answered on the bus and which mount they belong to."""

from __future__ import annotations

import threading
from typing import Dict, List, Optional, Set, Tuple

from .bus import BusEvent
from .catalog import Addr, Cmd
from .protocol import AuxPacket
from .protocol.units import format_version


EMULATED = " (emulated)"


class DeviceRegistry:
    """Bus listener that learns devices from their firmware-version replies."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._devices: Dict[int, str] = {}
        self._mount_model: Optional[int] = None
        self._emulated: Set[int] = set()  # addresses answered by this program; kept across scans

    def __call__(self, event: BusEvent) -> None:
        packet = event.frame
        if not isinstance(packet, AuxPacket) or not packet.data:
            return
        if packet.command == Cmd.GET_VERSION:
            self.add(packet.source, format_version(packet.data))
        elif packet.command == Cmd.MTR_GET_MODEL and packet.source == Addr.AZM_MOTOR:
            with self._lock:
                self._mount_model = int.from_bytes(packet.data, "big")

    def add(self, address: int, version: str) -> None:
        """Record a device; the first version seen for an address is kept.

        Devices this program emulates are labelled as such, even when they are
        found again by a scan.
        """
        with self._lock:
            if address in self._emulated and not version.endswith(EMULATED):
                version += EMULATED
            self._devices.setdefault(address, version)

    def set_mount_model(self, model: Optional[int]) -> None:
        with self._lock:
            self._mount_model = model

    def is_emulated(self, address: int) -> bool:
        with self._lock:
            return address in self._emulated

    def set_emulated(self, address: int, emulated: bool) -> None:
        with self._lock:
            (self._emulated.add if emulated else self._emulated.discard)(address)

    def remove(self, address: int) -> None:
        with self._lock:
            self._devices.pop(address, None)

    def clear(self) -> None:
        with self._lock:
            self._devices.clear()
            self._mount_model = None

    def contains(self, address: int) -> bool:
        with self._lock:
            return address in self._devices

    def devices(self) -> List[Tuple[int, str]]:
        """(address, firmware version) pairs, in discovery order."""
        with self._lock:
            return list(self._devices.items())

    @property
    def mount_model(self) -> Optional[int]:
        with self._lock:
            return self._mount_model
