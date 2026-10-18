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
from .discovery import DiscoveryTracker
from .gps_emulator import FIRMWARE_VERSION, GpsEmulator
from .keepalive import DEFAULT_INTERVAL, KeepAlive
from .monitors import FocuserMonitor, GpsMonitor
from .motion import MountMotion
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
    quiet: bool = False  # no console output at all (the web interface shows the traffic)
    listen_only: bool = False  # never transmit anything
    motion_enabled: bool = False   # mount position polling and manual slews
    gps_polling: bool = False      # ask the GPS for its status every few seconds
    focuser_polling: bool = False  # ask the focuser for its position every second
    keepalive: Optional[bool] = None  # None: use the transport's default
    keepalive_interval: float = DEFAULT_INTERVAL


class ScannerApp:
    def __init__(self, transport: Transport, config: Optional[AppConfig] = None,
                 stream: IO[str] = sys.stdout, *, csv_log: Optional[CsvLogger] = None,
                 raw_recorder: Optional[RawRecorder] = None,
                 discovery: Optional[DiscoveryTracker] = None) -> None:
        """``csv_log``, ``raw_recorder`` and ``discovery`` may be shared with a
        longer-lived owner (the web session keeps them across connections); the
        app then leaves closing the log files to that owner."""
        config = config or AppConfig()
        self.config = config
        self.transport = transport
        self.clock = SessionClock()
        self.bus = AuxBus(transport, self.clock)
        self.bus.listen_only = config.listen_only
        self.registry = DeviceRegistry()
        self.console = ConsoleView(stream, verbose=config.verbose, enabled=not config.quiet)
        self._owns_sinks = csv_log is None and raw_recorder is None
        self.csv_log = csv_log or CsvLogger()
        self.raw_recorder = raw_recorder or RawRecorder()
        self.discovery = discovery if discovery is not None else DiscoveryTracker()
        self.images = StarImageSaver(config.image_dir, enabled=config.save_starsense_images)
        self.gps = GpsEmulator(self.bus.send)
        self.scanner = BusScanner(self.bus, self.registry)
        self.motion = MountMotion(self.bus, enabled=config.motion_enabled)
        self.gps_monitor = GpsMonitor(self.bus, enabled=config.gps_polling)
        self.focuser = FocuserMonitor(self.bus, enabled=config.focuser_polling)
        keepalive_on = transport.keepalive_by_default if config.keepalive is None else config.keepalive
        self.keepalive = KeepAlive(self.bus, config.keepalive_interval, enabled=keepalive_on)
        self._scan_lock = threading.Lock()

        # Order matters only cosmetically: state first, then output, then reactions.
        for listener in (self.registry, self.discovery, self.motion, self.gps_monitor, self.focuser,
                         self.console, self.csv_log, self.images, self.gps):
            self.bus.subscribe(listener)
        self.bus.subscribe(self.raw_recorder.on_event)
        self.bus.add_raw_tap(self.raw_recorder)

    # --- lifecycle ----------------------------------------------------------

    def start(self, initial_scan: bool = True) -> None:
        """Open the transport and start background work. Raises OSError on connection failure."""
        from . import __copyright__, __version__

        self.console.banner(f"AUXBUS scanner {__version__}", __copyright__)
        self.transport.open()
        self.console.message(f"Connected: {self.transport.description}")
        if self.config.log_csv and not self.csv_log.active:
            self.csv_log.start(self.config.csv_path)
        if self.config.record_raw and not self.raw_recorder.active:
            self.raw_recorder.start(self.config.raw_path)
        self.console.banner("Starting AUXBUS monitor")
        self.bus.start()
        self.keepalive.start()
        self.motion.start()
        self.gps_monitor.start()
        self.focuser.start()
        if initial_scan and not self.listen_only:
            self.start_scan()

    def close(self) -> None:
        self.motion.close()  # stops the motors first if anything was moving
        self.gps_monitor.close()
        self.focuser.close()
        self.keepalive.stop()
        self.bus.stop()
        if self._owns_sinks:
            self.csv_log.stop()
            self.raw_recorder.stop()
        self.console.banner("Closed")

    # --- operations ---------------------------------------------------------

    def send(self, frame: OutgoingFrame) -> bool:
        return self.bus.send(frame)

    def mark(self, text: str) -> str:
        """Note what just happened, in every log. Raises ValueError for empty text."""
        return self.bus.mark(text).text

    def start_scan(self, full: bool = False) -> bool:
        """Scan in the background. Returns False if a scan is already running."""
        if not self._scan_lock.acquire(blocking=False):
            return False
        self._in_background("aux-scan", self._scan, full)
        return True

    @property
    def listen_only(self) -> bool:
        return self.bus.listen_only

    @listen_only.setter
    def listen_only(self, enabled: bool) -> None:
        if enabled and self.motion.moving:
            self.motion.stop()  # last chance: once listen-only is on, not even a stop can be sent
        self.config.listen_only = enabled
        self.bus.listen_only = enabled

    @property
    def scanning(self) -> bool:
        return self._scan_lock.locked()

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

    def set_csv_log(self, enabled: bool) -> None:
        if enabled and not self.csv_log.active:
            self.csv_log.start(self.config.csv_path)
        elif not enabled and self.csv_log.active:
            self.csv_log.stop()

    def toggle_csv_log(self) -> bool:
        self.set_csv_log(not self.csv_log.active)
        return self.csv_log.active

    def set_raw_recording(self, enabled: bool) -> None:
        if enabled and not self.raw_recorder.active:
            self.raw_recorder.start(self.config.raw_path)
        elif not enabled and self.raw_recorder.active:
            self.raw_recorder.stop()

    def toggle_raw_recording(self) -> bool:
        self.set_raw_recording(not self.raw_recorder.active)
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
