"""Wires the components together and exposes the operations the UI needs."""

from __future__ import annotations

import logging
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Callable, Dict, List, Optional, Protocol, Tuple

from ._sample_data import SAMPLE_STAR_LIST
from .bus import AuxBus, OutgoingFrame
from . import catalog
from .catalog import Addr
from .clock import SessionClock
from .discovery import DiscoveryTracker
from .gps_emulator import GpsEmulator
from .keepalive import DEFAULT_INTERVAL, KeepAlive
from .monitors import FocuserMonitor, GpsMonitor, DewMonitor, PlateSolver
from .motion import MountMotion
from .output import ConsoleView, CsvLogger, RawRecorder
from .protocol import AuxPacket, StarSenseFrame
from .protocol.units import format_version, parse_version
from .registry import DeviceRegistry
from .scanner import BusScanner
from .device_emulators import DewHeaterEmulator, FocuserEmulator, StarSenseV1Emulator, WifiModuleEmulator
from .mount_emulator import MountEmulator
from .starsense_emulator import StarSenseEmulator
from .starsense import StarImageSaver, run_capture_sequence
from .transport import Transport
from .wifi_bridge import WifiBridge

log = logging.getLogger(__name__)


def default_emulated_versions() -> Dict[str, Dict[int, bytes]]:
    """The firmware versions each emulator reports unless told otherwise (those of real devices)."""
    from .gps_emulator import FIRMWARE_VERSION as GPS_VERSION
    from .starsense_emulator import ADDRESS as STARSENSE_ADDRESS, FIRMWARE_VERSION as STARSENSE_VERSION

    return {"gps": {Addr.GPS: GPS_VERSION}, "starsense": {STARSENSE_ADDRESS: STARSENSE_VERSION},
            "focuser": dict(FocuserEmulator.versions), "dew": dict(DewHeaterEmulator.versions),
            "wifi": dict(WifiModuleEmulator.versions), "mount": dict(MountEmulator.versions),
            "starsense1": dict(StarSenseV1Emulator.versions)}


class Emulator(Protocol):
    """What every device emulator offers (GPS emulation works differently: it needs a position)."""

    versions: Dict[int, bytes]  # GET_VERSION reply per address, changeable while running

    @property
    def enabled(self) -> bool: ...

    def enable(self) -> None: ...

    def disable(self) -> None: ...


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
    dew_polling: bool = False      # ask the dew heater controller for its readings every 2 s
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
        self.wifi = WifiBridge(self.bus)
        self.gps = GpsEmulator(self._emulator_send)
        self.starsense_v2 = StarSenseEmulator(self._emulator_send)
        self.focuser_emulator = FocuserEmulator(self._emulator_send)
        self.dew_emulator = DewHeaterEmulator(self._emulator_send)
        self.wifi_module = WifiModuleEmulator(self._emulator_send)
        self.mount_emulator = MountEmulator(self._emulator_send)
        self.starsense_v1 = StarSenseV1Emulator(self._emulator_send)
        self.starsense_v2.pointing_source = self.mount_emulator.sky_pointing  # solves follow the emulated mount
        self.emulators: Dict[str, Emulator] = {
            "starsense": self.starsense_v2, "focuser": self.focuser_emulator, "dew": self.dew_emulator,
            "wifi": self.wifi_module, "mount": self.mount_emulator, "starsense1": self.starsense_v1,
        }
        self.scanner = BusScanner(self.bus, self.registry)
        self.motion = MountMotion(self.bus, enabled=config.motion_enabled)
        self.gps_monitor = GpsMonitor(self.bus, enabled=config.gps_polling)
        self.focuser = FocuserMonitor(self.bus, enabled=config.focuser_polling)
        self.dew = DewMonitor(self.bus, enabled=config.dew_polling)
        self.plate_solver = PlateSolver(self.bus)
        keepalive_on = transport.keepalive_by_default if config.keepalive is None else config.keepalive
        self.keepalive = KeepAlive(self.bus, config.keepalive_interval, enabled=keepalive_on)
        self._scan_lock = threading.Lock()

        # Order matters only cosmetically: state first, then output, then reactions.
        for listener in (self.registry, self.discovery, self.motion, self.gps_monitor, self.focuser, self.dew,
                         self.plate_solver,
                         self.console, self.csv_log, self.images, self.gps, self.starsense_v2,
                         self.focuser_emulator, self.dew_emulator, self.wifi_module, self.mount_emulator,
                         self.starsense_v1,
                         self.wifi):
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
        self.dew.start()
        self.plate_solver.start()
        if initial_scan and not self.listen_only:
            self.start_scan()

    def close(self) -> None:
        self.wifi.stop()
        self.motion.close()  # stops the motors first if anything was moving
        self.gps_monitor.close()
        self.focuser.close()
        self.dew.close()
        self.plate_solver.close()
        self.keepalive.stop()
        self.bus.stop()
        if self._owns_sinks:
            self.csv_log.stop()
            self.raw_recorder.stop()
        self.console.banner("Closed")

    # --- operations ---------------------------------------------------------

    def send(self, frame: OutgoingFrame) -> bool:
        return self.bus.send(frame)

    def _emulator_send(self, packet: AuxPacket) -> bool:
        """Where an emulator's reply goes: to the Wi-Fi apps only, when the Wi-Fi dongle runs
        without pass-through and the reply is for an app; otherwise onto the bus."""
        if self.wifi.wants_virtual(packet):
            self.bus.publish(packet, origin="emulated")
            self.wifi.deliver(packet.encode())
            return True
        return self.bus.send(packet, pace=False)  # a reply: real devices answer at once

    # --- Wi-Fi dongle -------------------------------------------------------------------

    def start_wifi_dongle(self, port: int, passthrough: bool, announce: bool = True) -> List[str]:
        """Let apps connect over TCP. Returns where it listens; OSError if it cannot listen.

        The dongle also answers as a SkyPortal Wi-Fi module (0xb3), unless a real
        one has already answered on the bus.
        """
        self.wifi.passthrough = passthrough
        self.wifi.announce = announce
        listening = self.wifi.start(port)
        try:
            self.enable_emulation("wifi")
        except ValueError:
            log.info("a real SkyPortal Wi-Fi module is on the bus; the dongle will not answer as 0xb3")
        return listening

    def stop_wifi_dongle(self) -> None:
        self.wifi.stop()
        self.disable_emulation("wifi")

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
        if enabled and self.focuser.moving_by_hand:
            self.focuser.stop()
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
        self.registry.set_emulated(Addr.GPS, True)
        self.registry.add(Addr.GPS, format_version(self.gps.versions[Addr.GPS]))

    def disable_gps_emulation(self) -> None:
        if self.gps.enabled:
            self.gps.disable()
            self.registry.set_emulated(Addr.GPS, False)
            self.registry.remove(Addr.GPS)

    def enable_emulation(self, kind: str) -> None:
        """Answer as an emulated device ("starsense", "focuser" or "dew").

        Refused if a real device has already answered at one of its addresses.
        """
        emulator = self.emulators[kind]
        versions = emulator.versions
        if emulator.enabled:
            return
        for address in versions:
            if self.registry.contains(address):
                raise ValueError(f"a {catalog.device_name(address)} (0x{address:02x}) already answered on the bus; "
                                 "two devices on one address would garble it")
        emulator.enable()
        for address, version in versions.items():
            self.registry.set_emulated(address, True)
            self.registry.add(address, format_version(version))
        if kind == "mount":
            self.registry.set_mount_model(self.mount_emulator.setup.spec.model_id)

    def disable_emulation(self, kind: str) -> None:
        emulator = self.emulators[kind]
        if emulator.enabled:
            emulator.disable()
            for address in emulator.versions:
                self.registry.set_emulated(address, False)
                self.registry.remove(address)
            if kind == "mount":
                self.registry.set_mount_model(None)

    def emulated_versions(self, kind: str) -> Dict[int, bytes]:
        """The firmware versions an emulator reports ("gps" included), per address."""
        return self.gps.versions if kind == "gps" else self.emulators[kind].versions

    def set_emulated_version(self, kind: str, address: int, text: str) -> str:
        """Change the version an emulator reports. Returns it formatted; ValueError if invalid."""
        versions = self.emulated_versions(kind)
        if address not in versions:
            raise ValueError(f"the {kind} emulator has no address 0x{address:02x}")
        versions[address] = parse_version(text)
        if self.registry.contains(address) and self.registry.is_emulated(address):
            self.registry.remove(address)  # list it with the new version
            self.registry.add(address, format_version(versions[address]))
        return format_version(versions[address])

    def enable_starsense_emulation(self) -> None:
        self.enable_emulation("starsense")

    def disable_starsense_emulation(self) -> None:
        self.disable_emulation("starsense")

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
