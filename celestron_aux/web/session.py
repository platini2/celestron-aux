"""The web interface's connection lifecycle.

A WebSession outlives individual connections: the browser can connect,
disconnect and connect again without restarting the program. The traffic feed,
the CSV log and the raw capture stay open across connections, so nothing
recorded is lost or overwritten, and settings chosen before connecting take
effect from the very first packet.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import IO, Dict, Optional, Tuple

from ..app import AppConfig, ScannerApp, default_emulated_versions
from ..discovery import DiscoveryTracker
from ..gps_emulator import validate_position
from ..protocol.units import format_version, parse_version
from ..output import CsvLogger, RawRecorder
from ..transport import Transport
from ..transport.factory import create_transport
from ..device_emulators import DewHeaterEmulator
from ..mount_emulator import MountSetup
from ..transport.local import LocalBusTransport
from ..transport.playback import PlaybackTransport
from ..transport.tcp import DEFAULT_PORT
from ..wifi_bridge import DEFAULT_PORT as DEFAULT_WIFI_PORT
from .feed import TrafficFeed

log = logging.getLogger(__name__)


class AlreadyConnected(Exception):
    pass


LOCAL_MODE = "local"  # the local bus: emulators and Wi-Fi apps, no telescope


class NeedsConfirmation(Exception):
    """Starting the emulated mount would disconnect the telescope; ask first."""


class WebSession:
    def __init__(self, config: AppConfig, stream: Optional[IO[str]] = None) -> None:
        config.quiet = True  # the browser shows the traffic; the console stays silent
        self.config = config
        self.feed = TrafficFeed()
        self.csv_log = CsvLogger()
        self.raw_recorder = RawRecorder()
        self.discovery = DiscoveryTracker()  # kept across connections, like the logs
        self._stream = stream
        self._lock = threading.RLock()
        self._app: Optional[ScannerApp] = None
        self._mode: Optional[str] = None
        self._gps: Optional[Tuple[float, float]] = None
        self._emulations: set = set()  # kinds to emulate, kept across connections
        self._versions: Dict[Tuple[str, int], bytes] = {}  # emulated firmware versions you changed
        self._wifi: Optional[Tuple[int, bool, bool]] = None  # Wi-Fi dongle (port, pass-through, announce) when on
        # The emulated telescope: its setup and motor firmware versions (None: the model's own).
        self._mount: Tuple[MountSetup, Optional[str], Optional[str]] = (MountSetup(), None, None)
        # What the emulated dew heater controller measures, as set on the page.
        self._dew: Dict[str, object] = {k: (list(v) if isinstance(v, list) else v)
                                        for k, v in DewHeaterEmulator.DEFAULTS.items()}
        self._replay_file: Optional[Path] = None  # uploaded capture, deleted on disconnect
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

    def replay(self, capture: bytes, name: str, speed: float) -> str:
        """Play back an uploaded raw capture as if it were a live connection."""
        handle, path = tempfile.mkstemp(prefix="celestron-aux-replay-", suffix=".txt")
        with os.fdopen(handle, "wb") as file:
            file.write(capture)
        try:
            description = self.connect_transport(PlaybackTransport(Path(path), speed=speed, name=name),
                                                 mode="playback", scan=False)
        except Exception:
            Path(path).unlink(missing_ok=True)
            raise
        with self._lock:
            self._replay_file = Path(path)
        return description

    def connect_transport(self, transport: Transport, *, mode: Optional[str] = None, scan: bool = True) -> str:
        with self._lock:
            if "mount" in self._emulations and mode != LOCAL_MODE:
                raise AlreadyConnected("The emulated mount is running. Stop it before connecting to a telescope.")
            if self._app is not None:
                if self._mode != LOCAL_MODE:
                    raise AlreadyConnected("Already connected. Disconnect first.")
                self.disconnect()  # a real connection replaces the local bus; emulators carry on there
            kwargs = {"stream": self._stream} if self._stream else {}
            app = ScannerApp(transport, self.config, csv_log=self.csv_log, raw_recorder=self.raw_recorder,
                             discovery=self.discovery, **kwargs)
            app.bus.subscribe(self.feed)  # before start, so the browser sees the very first packet
            if self._gps is not None:
                app.enable_gps_emulation(*self._gps)
            for (kind, address), version in self._versions.items():
                app.emulated_versions(kind)[address] = version
            app.dew_emulator.set_conditions(**self._dew)  # type: ignore[arg-type]
            for kind in sorted(self._emulations):
                if kind == "mount":
                    app.mount_emulator.configure(*self._mount)
                app.enable_emulation(kind)  # nothing has answered yet on a fresh connection
            self.feed.note(f"Connecting: {transport.description}")
            try:
                app.start(initial_scan=scan)
            except Exception as exc:
                transport.close()
                self.feed.note(f"Could not connect: {exc}")
                raise
            if self._wifi is not None:  # only once connected, so a failed connection leaves no port open
                try:
                    app.start_wifi_dongle(*self._wifi)
                except OSError as exc:
                    self.feed.note(f"Wi-Fi dongle not started: {exc}")
            self._app = app
            self._mode = mode
        self.feed.note(f"Connected: {transport.description}")
        return transport.description

    def disconnect(self) -> bool:
        """Close the current connection. Returns False if there was none."""
        with self._lock:
            if self._mode == LOCAL_MODE:
                self._emulations.discard("mount")  # closing the local bus switches the emulated mount off
            app, self._app, self._mode = self._app, None, None
            replay_file, self._replay_file = self._replay_file, None
        if app is None:
            return False
        app.close()
        if replay_file is not None:
            replay_file.unlink(missing_ok=True)
        self.feed.note(f"Disconnected: {app.transport.description}")
        return True

    def close(self) -> None:
        self.disconnect()
        self.csv_log.stop()
        self.raw_recorder.stop()
        self.feed.close()

    # --- settings that survive reconnects ------------------------------------

    @property
    def listen_only(self) -> bool:
        return self.config.listen_only

    def set_listen_only(self, enabled: bool) -> None:
        with self._lock:
            self.config.listen_only = enabled
            if self._app is not None:
                self._app.listen_only = enabled

    def set_motion_enabled(self, enabled: bool) -> None:
        with self._lock:
            self.config.motion_enabled = enabled
            if self._app is not None:
                self._app.motion.enabled = enabled

    def set_gps_polling(self, enabled: bool) -> None:
        with self._lock:
            self.config.gps_polling = enabled
            if self._app is not None:
                self._app.gps_monitor.enabled = enabled

    def set_focuser_polling(self, enabled: bool) -> None:
        with self._lock:
            self.config.focuser_polling = enabled
            if self._app is not None:
                self._app.focuser.enabled = enabled

    def set_dew_polling(self, enabled: bool) -> None:
        with self._lock:
            self.config.dew_polling = enabled
            if self._app is not None:
                self._app.dew.enabled = enabled

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

    def emulated_versions(self) -> Dict[str, Dict[int, str]]:
        """The version each emulator reports, per address, formatted."""
        with self._lock:
            versions = default_emulated_versions()
            for (kind, address), version in self._versions.items():
                versions[kind][address] = version
        return {kind: {address: format_version(v) for address, v in by_address.items()}
                for kind, by_address in versions.items()}

    def set_emulated_version(self, kind: str, address: int, text: str) -> str:
        """Change an emulator's version, now and on later connections. ValueError if invalid."""
        defaults = default_emulated_versions()
        if kind not in defaults or address not in defaults[kind]:
            raise ValueError(f"there is no {kind} emulator at 0x{address:02x}")
        version = parse_version(text)
        with self._lock:
            self._versions[(kind, address)] = version
            if self._app is not None:
                self._app.set_emulated_version(kind, address, text)
        return format_version(version)

    def ensure_bus(self) -> None:
        """Open the local bus if nothing is connected, so emulators and Wi-Fi apps have a bus."""
        with self._lock:
            if self._app is None:
                self.connect_transport(LocalBusTransport(), mode=LOCAL_MODE, scan=False)

    def set_wifi(self, enabled: bool, port: int, passthrough: bool, announce: bool = True) -> list:
        """Start, stop or reconfigure the Wi-Fi dongle. Returns where it listens; OSError if it cannot."""
        if enabled:
            self.ensure_bus()
        with self._lock:
            listening: list = []
            if self._app is not None:
                if not enabled:
                    self._app.stop_wifi_dongle()
                elif self._app.wifi.running and self._app.wifi.port == port:
                    self._app.wifi.passthrough = passthrough  # no need to reconnect the apps
                    self._app.wifi.announce = announce
                    listening = list(self._app.wifi.listening)
                else:
                    listening = self._app.start_wifi_dongle(port, passthrough, announce)
            self._wifi = (port, passthrough, announce) if enabled else None
            return listening

    def wifi_state(self) -> dict:
        with self._lock:
            configured = self._wifi
            app = self._app
        state = {"enabled": configured is not None, "port": configured[0] if configured else DEFAULT_WIFI_PORT,
                 "passthrough": configured[1] if configured else False,
                 "announce": configured[2] if configured else True, "listening": [], "clients": []}
        if app is not None and app.wifi.running:
            snapshot = app.wifi.snapshot()
            state.update(listening=snapshot["listening"], clients=snapshot["clients"])
        return state

    def set_mount(self, enabled: bool, setup: MountSetup, azm_version: Optional[str] = None,
                  alt_version: Optional[str] = None, confirm: bool = False) -> None:
        """Start or stop the emulated telescope.

        It runs on the local bus, never next to a real telescope: starting it while
        one is connected raises NeedsConfirmation unless ``confirm``, which then
        disconnects the telescope. ValueError for an invalid setup.
        """
        if not enabled:
            self.set_emulation("mount", False)
            return
        setup = setup.validated()
        for version in (azm_version, alt_version):
            if version:
                parse_version(version)
        with self._lock:
            current = self._app
            physical = current is not None and self._mode != LOCAL_MODE
            if current is not None and physical and not confirm:
                raise NeedsConfirmation(f"Connected to {current.transport.description}. Starting the emulated "
                                        "mount disconnects it.")
            if physical:
                self.disconnect()
            self._mount = (setup, azm_version or None, alt_version or None)
            self.ensure_bus()
            if self._app is not None:
                if self._app.mount_emulator.enabled:
                    self._app.disable_emulation("mount")  # a new setup is a new power-on
                self._app.mount_emulator.configure(*self._mount)
            self.set_emulation("mount", True)

    def adjust_mount(self, inclination: float, rotation: float) -> None:
        """Turn the emulated wedge's or mount's knobs while it runs. ValueError if not possible."""
        from dataclasses import replace
        with self._lock:
            if self._app is None or "mount" not in self._emulations:
                raise ValueError("the emulated telescope is not running")
            self._app.mount_emulator.adjust(inclination, rotation)
            setup, azm, alt = self._mount
            self._mount = (replace(setup, inclination=inclination, rotation=rotation % 360), azm, alt)

    def mount_state(self) -> dict:
        with self._lock:
            setup, azm_version, alt_version = self._mount
            app = self._app
            enabled = "mount" in self._emulations
        status = app.mount_emulator.snapshot() if app is not None and enabled else None
        return {"enabled": enabled, "setup": {"model": setup.model, "wedge": setup.wedge,
                                              "initial_alt": setup.initial_alt, "initial_az": setup.initial_az,
                                              "inclination": setup.inclination, "rotation": setup.rotation,
                                              "polar_error_alt": setup.polar_error_alt,
                                              "polar_error_az": setup.polar_error_az},
                "azm_version": azm_version, "alt_version": alt_version, "status": status}

    def set_dew_conditions(self, **conditions: object) -> None:
        """Change what the emulated dew controller measures, now and on later connections."""
        DewHeaterEmulator(lambda packet: None).set_conditions(**conditions)  # type: ignore[arg-type]  # validate
        with self._lock:
            self._dew.update({k: v for k, v in conditions.items() if v is not None})
            if self._app is not None:
                self._app.dew_emulator.set_conditions(**conditions)  # type: ignore[arg-type]

    def dew_state(self) -> dict:
        with self._lock:
            app, conditions = self._app, dict(self._dew)
        status = app.dew_emulator.snapshot() if app is not None else None
        return {"conditions": conditions, "status": status}

    def emulating(self, kind: str) -> bool:
        with self._lock:
            return kind in self._emulations

    def set_emulation(self, kind: str, enabled: bool) -> None:
        """Raises ValueError if a real device of that kind is on the bus."""
        if enabled:
            self.ensure_bus()
        with self._lock:
            if self._app is not None:
                if enabled:
                    self._app.enable_emulation(kind)
                else:
                    self._app.disable_emulation(kind)
            (self._emulations.add if enabled else self._emulations.discard)(kind)

    @property
    def starsense_emulation(self) -> bool:
        return self.emulating("starsense")

    def set_starsense_emulation(self, enabled: bool) -> None:
        self.set_emulation("starsense", enabled)

    @property
    def gps_position(self) -> Optional[Tuple[float, float]]:
        with self._lock:
            return self._gps

    def set_gps(self, position: Optional[Tuple[float, float]]) -> None:
        if position is not None:
            validate_position(*position)
            self.ensure_bus()
        with self._lock:
            self._gps = position
            if self._app is not None:
                if position is None:
                    self._app.disable_gps_emulation()
                else:
                    self._app.enable_gps_emulation(*position)
