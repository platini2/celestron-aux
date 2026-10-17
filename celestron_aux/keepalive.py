"""Periodic traffic that stops the Wi-Fi module from dropping an idle connection."""

from __future__ import annotations

import threading

from .bus import AuxBus
from .catalog import Addr, Cmd
from .protocol import AuxPacket

DEFAULT_INTERVAL = 10.0
_KEEPALIVE_PACKET = AuxPacket(Addr.SCANNER, Addr.AZM_MOTOR, Cmd.GET_VERSION)


class KeepAlive:
    """Sends a harmless version request whenever the bus has been idle too long."""

    def __init__(self, bus: AuxBus, interval: float = DEFAULT_INTERVAL, enabled: bool = False) -> None:
        if interval <= 0:
            raise ValueError("interval must be positive")
        self._bus = bus
        self.interval = interval
        self.enabled = enabled
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="aux-keepalive", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(min(1.0, self.interval)):
            if self.enabled and self._bus.seconds_since_last_tx() >= self.interval:
                self._bus.send(_KEEPALIVE_PACKET)
