"""The web interface's connection lifecycle.

A WebSession outlives individual connections: the browser can connect,
disconnect and connect again without restarting the program. The traffic feed,
the CSV log and the raw capture stay open across connections, so nothing
recorded is lost or overwritten, and settings chosen before connecting take
effect from the very first packet.
"""

from __future__ import annotations

import logging
import threading
from typing import IO, Optional, Tuple

from ..app import AppConfig, ScannerApp
from ..gps_emulator import validate_position
from ..output import CsvLogger, RawRecorder
from ..transport import Transport
from ..transport.factory import create_transport
from ..transport.tcp import DEFAULT_PORT
from .feed import TrafficFeed

log = logging.getLogger(__name__)


class AlreadyConnected(Exception):
    pass


class WebSession:
    def __init__(self, config: AppConfig, stream: Optional[IO[str]] = None) -> None:
        config.quiet = True  # the browser shows the traffic; the console stays silent
        self.config = config
        self.feed = TrafficFeed()
        self.csv_log = CsvLogger()
        self.raw_recorder = RawRecorder()
        self._stream = stream
        self._lock = threading.RLock()
        self._app: Optional[ScannerApp] = None
        self._mode: Optional[str] = None
        self._gps: Optional[Tuple[float, float]] = None
        if config.log_csv:
            self.csv_log.start(config.csv_path)
        if config.record_raw:
            self.raw_recorder.start(config.raw_path)

    # --- connection ---------------------------------------------------------

    @property
    def app(self) -> Optional[ScannerApp]:
        with self._lock:
            return self._app

    @property
    def mode(self) -> Optional[str]:
        with self._lock:
            return self._mode

    def connect(self, mode: str, target: str, *, tcp_port: int = DEFAULT_PORT, scan: bool = True) -> str:
        """Open a connection; raises OSError or ValueError if it cannot be made."""
        return self.connect_transport(create_transport(mode, target, tcp_port=tcp_port), mode=mode, scan=scan)

    def connect_transport(self, transport: Transport, *, mode: Optional[str] = None, scan: bool = True) -> str:
        with self._lock:
            if self._app is not None:
                raise AlreadyConnected("Already connected. Disconnect first.")
            app = ScannerApp(transport, self.config, csv_log=self.csv_log, raw_recorder=self.raw_recorder,
                             **({"stream": self._stream} if self._stream else {}))
            app.bus.subscribe(self.feed)  # before start, so the browser sees the very first packet
            if self._gps is not None:
                app.enable_gps_emulation(*self._gps)
            self.feed.note(f"Connecting: {transport.description}")
            try:
                app.start(initial_scan=scan)
            except Exception as exc:
                transport.close()
                self.feed.note(f"Could not connect: {exc}")
                raise
            self._app = app
            self._mode = mode
        self.feed.note(f"Connected: {transport.description}")
        return transport.description

    def disconnect(self) -> bool:
        """Close the current connection. Returns False if there was none."""
        with self._lock:
            app, self._app, self._mode = self._app, None, None
        if app is None:
            return False
        app.close()
        self.feed.note(f"Disconnected: {app.transport.description}")
        return True

    def close(self) -> None:
        self.disconnect()
        self.csv_log.stop()
        self.raw_recorder.stop()
        self.feed.close()

    # --- settings that survive reconnects ------------------------------------

    def set_keepalive(self, enabled: bool) -> None:
        with self._lock:
            self.config.keepalive = enabled
            if self._app is not None:
                self._app.keepalive.enabled = enabled

    def keepalive_enabled(self) -> bool:
        with self._lock:
            if self._app is not None:
                return self._app.keepalive.enabled
            return bool(self.config.keepalive)

    def set_csv_log(self, enabled: bool) -> None:
        with self._lock:
            self.config.log_csv = enabled
            if enabled and not self.csv_log.active:
                self.csv_log.start(self.config.csv_path)
            elif not enabled:
                self.csv_log.stop()

    def set_raw_recording(self, enabled: bool) -> None:
        with self._lock:
            self.config.record_raw = enabled
            if enabled and not self.raw_recorder.active:
                self.raw_recorder.start(self.config.raw_path)
            elif not enabled:
                self.raw_recorder.stop()

    def set_images(self, enabled: bool) -> None:
        with self._lock:
            self.config.save_starsense_images = enabled
            if self._app is not None:
                self._app.images.enabled = enabled

    @property
    def gps_position(self) -> Optional[Tuple[float, float]]:
        with self._lock:
            return self._gps

    def set_gps(self, position: Optional[Tuple[float, float]]) -> None:
        if position is not None:
            validate_position(*position)
        with self._lock:
            self._gps = position
            if self._app is not None:
                if position is None:
                    self._app.disable_gps_emulation()
                else:
                    self._app.enable_gps_emulation(*position)
