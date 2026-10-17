"""Wires the components together and exposes the operations the UI needs."""

from __future__ import annotations

import logging
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Callable, List, Optional, Tuple

from ._sample_data import SAMPLE_STAR_LIST
from .bus import AuxBus, OutgoingFrame
from .catalog import Addr
from .clock import SessionClock
from .gps_emulator import FIRMWARE_VERSION, GpsEmulator
from .keepalive import DEFAULT_INTERVAL, KeepAlive
from .output import ConsoleView, CsvLogger, RawRecorder
from .protocol import StarSenseFrame
from .protocol.units import format_version
from .registry import DeviceRegistry
from .scanner import BusScanner
from .starsense import StarImageSaver, run_capture_sequence
from .transport import Transport

log = logging.getLogger(__name__)


@dataclass
class AppConfig:
    csv_path: Path = Path("auxbuslog.csv")
    raw_path: Path = Path("rawoutput.txt")
    image_dir: Path = Path(".")
    log_csv: bool = False
    record_raw: bool = False
    save_starsense_images: bool = True
    verbose: bool = False
    keepalive: Optional[bool] = None  # None: use the transport's default
    keepalive_interval: float = DEFAULT_INTERVAL


class ScannerApp:
    def __init__(self, transport: Transport, config: Optional[AppConfig] = None,
                 stream: IO[str] = sys.stdout) -> None:
        config = config or AppConfig()
        self.config = config
        self.transport = transport
        self.clock = SessionClock()
        self.bus = AuxBus(transport, self.clock)
        self.registry = DeviceRegistry()
        self.console = ConsoleView(stream, verbose=config.verbose)
        self.csv_log = CsvLogger()
        self.raw_recorder = RawRecorder()
        self.images = StarImageSaver(config.image_dir, enabled=config.save_starsense_images)
        self.gps = GpsEmulator(self.bus.send)
        self.scanner = BusScanner(self.bus, self.registry)
        keepalive_on = transport.keepalive_by_default if config.keepalive is None else config.keepalive
        self.keepalive = KeepAlive(self.bus, config.keepalive_interval, enabled=keepalive_on)
        self._scan_lock = threading.Lock()

        # Order matters only cosmetically: state first, then output, then reactions.
        for listener in (self.registry, self.console, self.csv_log, self.images, self.gps):
            self.bus.subscribe(listener)
        self.bus.add_raw_tap(self.raw_recorder)

    # --- lifecycle ----------------------------------------------------------

    def start(self, initial_scan: bool = True) -> None:
        """Open the transport and start background work. Raises OSError on connection failure."""
        from . import __version__

        self.console.banner(f"AUXBUS scanner {__version__}")
        self.transport.open()
        self.console.message(f"Connected: {self.transport.description}")
        if self.config.log_csv:
            self.csv_log.start(self.config.csv_path)
        if self.config.record_raw:
            self.raw_recorder.start(self.config.raw_path)
        self.console.banner("Starting AUXBUS monitor")
        self.bus.start()
        self.keepalive.start()
        if initial_scan:
            self.start_scan()

    def close(self) -> None:
        self.keepalive.stop()
        self.bus.stop()
        self.csv_log.stop()
        self.raw_recorder.stop()
        self.console.banner("Closed")

    # --- operations ---------------------------------------------------------

    def send(self, frame: OutgoingFrame) -> bool:
        return self.bus.send(frame)

    def start_scan(self, full: bool = False) -> bool:
        """Scan in the background. Returns False if a scan is already running."""
        if not self._scan_lock.acquire(blocking=False):
            return False
        self._in_background("aux-scan", self._scan, full)
        return True

    def identify_mount(self) -> bool:
        return self.scanner.identify_mount()

    def show_devices(self) -> None:
        self.console.device_table(self.registry.devices(), self.registry.mount_model)

    def devices(self) -> List[Tuple[int, str]]:
        return self.registry.devices()

    def enable_gps_emulation(self, latitude: float, longitude: float) -> None:
        self.gps.enable(latitude, longitude)
        self.registry.add(Addr.GPS, f"{format_version(FIRMWARE_VERSION)} (emulated)")

    def disable_gps_emulation(self) -> None:
        self.gps.disable()

    def toggle_csv_log(self) -> bool:
        if self.csv_log.active:
            self.csv_log.stop()
        else:
            self.csv_log.start(self.config.csv_path)
        return self.csv_log.active

    def toggle_raw_recording(self) -> bool:
        if self.raw_recorder.active:
            self.raw_recorder.stop()
        else:
            self.raw_recorder.start(self.config.raw_path)
        return self.raw_recorder.active

    def start_starsense_capture(self) -> None:
        self._in_background("aux-starsense-capture", run_capture_sequence, self.bus.send)

    def send_sample_star_list(self) -> bool:
        return self.bus.send(StarSenseFrame(SAMPLE_STAR_LIST))

    @property
    def out_of_frame_bytes(self) -> int:
        return self.bus.out_of_frame_bytes

    # --- internals ----------------------------------------------------------

    def _scan(self, full: bool) -> None:
        try:
            self.console.banner("Initiating AUXBUS scan" + (" (all addresses)" if full else ""))
            self.console.traffic_header()
            self.scanner.scan(full)
            self.console.banner("Finished AUXBUS scan")
            self.show_devices()
        finally:
            self._scan_lock.release()

    @staticmethod
    def _in_background(name: str, target: Callable[..., object], *args: object) -> None:
        def run() -> None:
            try:
                target(*args)
            except Exception:
                log.exception("%s failed", name)

        threading.Thread(target=run, name=name, daemon=True).start()
