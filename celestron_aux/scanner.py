"""Active discovery of devices on the bus."""

from __future__ import annotations

import time
from typing import Callable, Iterable, List

from .bus import AuxBus
from .catalog import DEVICE_NAMES, Addr, Cmd
from .protocol import AuxPacket
from .registry import DeviceRegistry

KNOWN_ADDRESSES: List[int] = sorted(address for address in DEVICE_NAMES if address != Addr.SCANNER)
ALL_ADDRESSES: List[int] = [address for address in range(0x01, 0xFF) if address != Addr.SCANNER]


class BusScanner:
    def __init__(
        self,
        bus: AuxBus,
        registry: DeviceRegistry,
        *,
        reply_wait: float = 0.5,
        mount_wait: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._bus = bus
        self._registry = registry
        self._reply_wait = reply_wait
        self._mount_wait = mount_wait
        self._sleep = sleep

    def scan(self, full: bool = False) -> None:
        """Ask every address for its firmware version, then identify the mount.

        ``full`` probes every possible address instead of only the known ones.
        Blocks until done (several seconds; about 40 s for a full scan).
        """
        self._registry.clear()
        self._probe(ALL_ADDRESSES if full else KNOWN_ADDRESSES)
        self._sleep(self._reply_wait)
        if self.identify_mount():
            self._sleep(self._mount_wait)

    def identify_mount(self) -> bool:
        """Ask the azimuth motor for the mount model. False if no motor was found."""
        if not self._registry.contains(Addr.AZM_MOTOR):
            return False
        return self._bus.send(AuxPacket(Addr.SCANNER, Addr.AZM_MOTOR, Cmd.MTR_GET_MODEL))

    def _probe(self, addresses: Iterable[int]) -> None:
        for address in addresses:
            if not self._bus.send(AuxPacket(Addr.SCANNER, address, Cmd.GET_VERSION)):
                return
