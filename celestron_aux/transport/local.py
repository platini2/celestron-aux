"""A bus with no telescope on it: only the scanner, its emulators and Wi-Fi apps.

Nothing written goes anywhere and nothing is ever received from outside. Every
packet the scanner (or an emulator, or a Wi-Fi app) sends still enters the
traffic stream, so it is decoded and logged, and the emulators answer requests
addressed to them, exactly as on a real bus.
"""

from __future__ import annotations

import time

from .base import Transport


class LocalBusTransport(Transport):
    echoes_tx = False  # sent packets are reported by the bus layer, like any connection

    @property
    def description(self) -> str:
        return "local bus (no telescope)"

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def read(self) -> bytes:
        time.sleep(self.poll_interval)
        return b""

    def write(self, data: bytes) -> None:
        pass  # there is no telescope to send to
