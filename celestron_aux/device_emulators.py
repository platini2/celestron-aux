"""Emulated accessories: a focuser and a dew heater controller.

Each answers requests sent to its bus address while enabled, as the real device
does. They are for testing software such as CPWI without the accessory.

* The dew heater controller gives the replies CPWI expects from the real one,
  as worked out by community reverse engineering, including the companion
  address 0xbb that CPWI uses to detect it.
* The focuser follows how the INDI Celestron focuser driver talks to the real
  focus motor: position, go-to, move at rates 1-9, slew done, travel limits and
  calibration. Its speeds are invented (no capture of a real one moving yet).
"""

from __future__ import annotations

import math
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .bus import BusEvent
from .catalog import Addr
from .protocol import AuxPacket

GET_VERSION, GET_MODEL = 0xFE, 0x05


class DeviceEmulator:
    """Answers requests to ``addresses`` while enabled. Subclasses implement ``answer``."""

    addresses: Tuple[int, ...] = ()
    versions: Dict[int, bytes] = {}  # GET_VERSION reply per address

    def __init__(self, send: Callable[[AuxPacket], object], clock: Callable[[], float] = time.monotonic) -> None:
        self._send = send
        self._clock = clock
        self._lock = threading.Lock()
        self._enabled = False
        self.versions = dict(type(self).versions)  # per instance: can be changed while running

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def enable(self) -> None:
        with self._lock:
            self._enabled = True

    def disable(self) -> None:
        with self._lock:
            self._enabled = False

    def __call__(self, event: BusEvent) -> None:
        packet = event.frame
        if not isinstance(packet, AuxPacket) or packet.destination not in self.addresses:
            return
        if packet.source in self.addresses:
            return  # our own reply
        with self._lock:
            if not self._enabled:
                return
            if packet.command == GET_VERSION:
                reply: Optional[bytes] = self.versions[packet.destination]
            else:
                reply = self.answer(packet.destination, packet.command, packet.data)
        if reply is not None:
            self._send(AuxPacket(packet.destination, packet.source, packet.command, reply))

    def answer(self, address: int, command: int, data: bytes) -> Optional[bytes]:
        """The reply payload, or None to stay silent. Called with the lock held."""
        raise NotImplementedError


# --- focuser --------------------------------------------------------------------------

class FocuserEmulator(DeviceEmulator):
    """A Celestron focus motor at 0x12 that moves over time."""

    addresses = (Addr.FOCUSER,)
    versions = {Addr.FOCUSER: bytes.fromhex("07102454")}  # 7.16.9300, as a real focuser reported

    GET_POSITION, GOTO_FAST, SLEW_DONE = 0x01, 0x02, 0x13
    MOVE_POS, MOVE_NEG = 0x24, 0x25
    CALIBRATION_ENABLE, CALIBRATION_DONE, GET_LIMITS = 0x2A, 0x2B, 0x2C

    # Steps per second for rates 0-9 (invented, roughly a full travel in a minute at rate 9).
    SPEEDS = (0, 10, 25, 50, 100, 200, 400, 600, 800, 1000)
    LIMITS = (1000, 60000)
    CALIBRATION_STEPS = 12   # INDI reports calibration states 0-12
    CALIBRATION_TIME = 6.0   # seconds

    def __init__(self, send: Callable[[AuxPacket], object], clock: Callable[[], float] = time.monotonic,
                 position: int = 30000) -> None:
        super().__init__(send, clock)
        self._position = float(position)
        self._velocity = 0.0                 # steps per second, signed
        self._target: Optional[int] = None   # go-to target
        self._updated = clock()
        self._calibration_started: Optional[float] = None
        self._calibrated = True

    def snapshot(self) -> dict:
        with self._lock:
            self._advance()
            return {"enabled": self._enabled, "position": round(self._position), "moving": self._moving()}

    def answer(self, address: int, command: int, data: bytes) -> Optional[bytes]:
        self._advance()
        if command == self.GET_POSITION:
            return round(self._position).to_bytes(3, "big")
        if command == self.SLEW_DONE:
            return b"\x00" if self._moving() else b"\xff"
        if command == self.GOTO_FAST and len(data) >= 3:
            low, high = self.LIMITS
            self._target = min(max(int.from_bytes(data[:3], "big"), low), high)
            self._velocity = math.copysign(self.SPEEDS[9], self._target - self._position)
            return b"\x01"  # acknowledged like a motor controller (real motors answer 01)
        if command in (self.MOVE_POS, self.MOVE_NEG) and data:
            rate = min(data[0], 9)
            self._target = None
            self._velocity = self.SPEEDS[rate] * (1 if command == self.MOVE_POS else -1)
            return b"\x01"
        if command == self.GET_LIMITS:
            low, high = self.LIMITS
            return low.to_bytes(4, "big") + high.to_bytes(4, "big")
        if command == self.CALIBRATION_ENABLE and data:
            if data[0]:   # 1 starts a calibration, 0 aborts it (as the INDI driver sends)
                self._calibration_started = self._clock()
                self._calibrated = False
            else:
                self._calibration_started = None
            return b""
        if command == self.CALIBRATION_DONE:
            return self._calibration_status()
        return None

    def _moving(self) -> bool:
        return self._velocity != 0

    def _advance(self) -> None:
        """Move the position on to now, stopping at a go-to target or a travel limit."""
        now = self._clock()
        elapsed, self._updated = now - self._updated, now
        if not self._velocity:
            return
        position = self._position + self._velocity * elapsed
        low, high = self.LIMITS
        if self._target is not None and (position - self._target) * self._velocity >= 0:
            position, self._velocity, self._target = float(self._target), 0.0, None
        if not low <= position <= high:
            position, self._velocity = float(min(max(position, low), high)), 0.0
        self._position = position

    def _calibration_status(self) -> bytes:
        if self._calibration_started is None:
            return bytes((1 if self._calibrated else 0, 0))
        progress = (self._clock() - self._calibration_started) / self.CALIBRATION_TIME
        if progress >= 1:
            self._calibration_started, self._calibrated = None, True
            return bytes((1, self.CALIBRATION_STEPS))
        return bytes((0, max(1, int(progress * self.CALIBRATION_STEPS))))


# --- StarSense AutoAlign (the original camera) ----------------------------------------------

class StarSenseV1Emulator(DeviceEmulator):
    """The original StarSense AutoAlign camera at 0xb4: it answers discovery (its version), so
    scans find it; star lists are sent with the "Send sample star list" button."""

    addresses = (Addr.STARSENSE_CAMERA,)
    versions = {Addr.STARSENSE_CAMERA: bytes.fromhex("0102341f")}  # 1.2.13343, as a real camera reported

    def answer(self, address: int, command: int, data: bytes) -> Optional[bytes]:
        return None  # only discovery is emulated


# --- Wi-Fi module ---------------------------------------------------------------------------

class WifiModuleEmulator(DeviceEmulator):
    """A SkyPortal Wi-Fi module at 0xb3, as the Wi-Fi dongle presents itself."""

    addresses = (Addr.SKYPORTAL_WIFI,)
    versions = {Addr.SKYPORTAL_WIFI: bytes((3, 1)) + (9120).to_bytes(2, "big")}  # 3.1.9120, from a real module

    def answer(self, address: int, command: int, data: bytes) -> Optional[bytes]:
        return None  # only its version is known; it stays silent for anything else


# --- dew heater controller -----------------------------------------------------------------

def _milli(value: float, size: int) -> bytes:
    """A value in thousandths, big-endian, as the controller encodes them (volts, amps, degrees)."""
    return int(value * 1000).to_bytes(size, "big", signed=True)


class DewHeaterEmulator(DeviceEmulator):
    """A Celestron Smart DewHeater Controller (0x17) with two heater channels and one 12 V port."""

    DETECTION = 0xBB  # the companion address CPWI asks first
    addresses = (Addr.DEW_HEATER, DETECTION)
    versions = {Addr.DEW_HEATER: bytes((1, 0)), DETECTION: bytes((1, 1)) + (1270).to_bytes(2, "big")}

    CHANNELS, PORTS_12V = 2, 1
    PORT_12V = 2  # outputs switched with 0x14: 0 and 1 the heaters (assumed), 2 the 12 V output
    MAX_AMPS_PER_CHANNEL = 2.5
    RECALIBRATION_TIME = 10.0
    # The conditions it reports, and the ranges they can be set to.
    DEFAULT_AMBIENT, DEFAULT_HUMIDITY, DEFAULT_VOLTAGE = 5.0, 75.0, 12.7  # degrees C, percent, volts
    DEFAULT_HEATER_TEMPERATURES = (7.0, 7.0)
    DEFAULTS: Dict[str, Any] = {"ambient": DEFAULT_AMBIENT, "humidity": DEFAULT_HUMIDITY, "voltage": DEFAULT_VOLTAGE,
                                "heater_temperatures": list(DEFAULT_HEATER_TEMPERATURES)}
    RANGES = {"ambient": (-30.0, 50.0), "humidity": (1.0, 100.0), "voltage": (11.0, 13.0), "heater": (-30.0, 60.0)}

    def __init__(self, send: Callable[[AuxPacket], object], clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(send, clock)
        self.input_voltage = self.DEFAULT_VOLTAGE
        self.current_limit = 5.0
        self.led_brightness = 128
        self.aggression = [5, 5]          # 1-10 = automatic, 0 = manual; both heating by default
        self.power_percent = [40.0, 40.0]  # manual power; automatic channels derive theirs
        self.strap_temperature = list(self.DEFAULT_HEATER_TEMPERATURES)
        self.ambient = self.DEFAULT_AMBIENT
        self.humidity = self.DEFAULT_HUMIDITY
        self.ports_enabled: List[int] = [1] * (self.CHANNELS + self.PORTS_12V)  # all on until switched off
        self._recalibration_started: Optional[float] = None

    def set_conditions(self, *, ambient: Optional[float] = None, humidity: Optional[float] = None,
                       voltage: Optional[float] = None, heater_temperatures: Optional[List[float]] = None) -> None:
        """What the controller measures, as set from the page. ValueError if out of range."""
        def check(name: str, value: float, key: Optional[str] = None) -> float:
            low, high = self.RANGES[key or name]
            if not low <= value <= high:
                raise ValueError(f"{name.replace('_', ' ')} must be between {low:g} and {high:g}")
            return float(value)
        with self._lock:
            if ambient is not None:
                self.ambient = check("ambient", ambient)
            if humidity is not None:
                self.humidity = check("humidity", humidity)
            if voltage is not None:
                self.input_voltage = check("voltage", voltage)
            if heater_temperatures is not None:
                if len(heater_temperatures) != self.CHANNELS:
                    raise ValueError(f"give {self.CHANNELS} heater temperatures")
                self.strap_temperature = [check("heater_temperature", t, "heater") for t in heater_temperatures]

    def snapshot(self) -> dict:
        with self._lock:
            powers = self._powers()
            return {
                "enabled": self._enabled, "ambient": self.ambient, "humidity": self.humidity,
                "voltage": self.input_voltage, "dew_point": round(self.dew_point(), 2),
                "led_brightness": self.led_brightness, "port_12v": bool(self.ports_enabled[self.PORT_12V]),
                "heaters": [{"on": bool(self.ports_enabled[c]), "automatic": self.aggression[c] != 0,
                             "aggression": self.aggression[c], "power": round(powers[c], 1),
                             "temperature": self.strap_temperature[c]} for c in range(self.CHANNELS)],
            }

    def dew_point(self) -> float:
        """Magnus formula."""
        a, b = 17.62, 243.12
        gamma = math.log(self.humidity / 100) + a * self.ambient / (b + self.ambient)
        return b * gamma / (a - gamma)

    def _powers(self) -> List[float]:
        # Automatic channels heat more the closer the strap is to the dew point (simple model).
        # A heater that was switched off draws nothing.
        powers = []
        for channel in range(self.CHANNELS):
            if not self.ports_enabled[channel]:
                powers.append(0.0)
            elif self.aggression[channel]:
                spread = self.strap_temperature[channel] - self.dew_point()
                powers.append(min(100.0, max(0.0, self.aggression[channel] * 10 - spread * 5)))
            else:
                powers.append(self.power_percent[channel])
        return powers

    def answer(self, address: int, command: int, data: bytes) -> Optional[bytes]:
        if address == self.DETECTION:
            return bytes((0x01, 0x07)) if command == GET_MODEL else None
        powers = self._powers()
        amps = [p * self.MAX_AMPS_PER_CHANNEL / 100 for p in powers]
        if command == 0x00:  # input power: volts, amps, two unknown bytes
            return _milli(self.input_voltage, 2) + _milli(sum(amps), 2) + b"\x00\x00"
        if command == 0x02:
            return bytes((self.led_brightness,))
        if command == 0x03 and len(data) >= 2:
            self.current_limit = int.from_bytes(data[:2], "big") / 1000
            return b""
        if command == 0x04:  # present and maximum input current limit
            return _milli(self.current_limit, 2) + _milli(10.0, 2)
        if command == 0x10:
            return bytes((self.CHANNELS + self.PORTS_12V,))
        if command == 0x11 and data:
            return self._port(data[0])
        if command == 0x12 and data:
            channel = data[0]
            if channel >= self.CHANNELS:
                return bytes((channel + 1,))
            return (bytes((channel + 1, int(self.aggression[channel] != 0), int(powers[channel] * 2.55 + 0.5)))
                    + _milli(amps[channel], 2) + bytes((self.aggression[channel],))
                    + _milli(self.strap_temperature[channel], 4))
        if command == 0x14 and len(data) >= 2:  # switch an output on or off; remembered
            if data[0] < len(self.ports_enabled):
                self.ports_enabled[data[0]] = 1 if data[1] else 0
            return b""
        if command == 0x16 and len(data) >= 2:
            if data[0] < self.CHANNELS and 1 <= data[1] <= 10:
                self.aggression[data[0]] = data[1]
            return b""
        if command == 0x17 and len(data) >= 2:
            if data[0] < self.CHANNELS:
                self.aggression[data[0]] = 0
                self.power_percent[data[0]] = float(int(data[1] / 2.55 + 0.5))
            return b""
        if command == 0x18:  # temperature, dew point (thousandths of a degree), humidity
            return _milli(self.ambient, 4) + _milli(self.dew_point(), 4) + bytes((int(self.humidity + 0.5),))
        if command == 0x19 and data:
            self._recalibration_started = self._clock() if data[0] else None
            return b""
        if command == 0x1A:
            running = (self._recalibration_started is not None
                       and self._clock() - self._recalibration_started < self.RECALIBRATION_TIME)
            return bytes((int(running),))
        if command == 0x20 and data:
            self.led_brightness = data[0]
            return b""
        if command == 0x21:
            return b"\xff"  # what a Celestron unit replies (meaning unknown)
        return None

    def _port(self, selector: int) -> bytes:
        """Port queries: 0 = how many 12 V ports, 1 = how many heaters, 0xff = USB hub, else a port."""
        if selector == 0x00:
            return bytes((self.PORTS_12V,))
        if selector == 0x01:
            return bytes((self.CHANNELS,))
        if selector == 0xFF:
            return b"\x00"  # status of the USB hub
        number = selector - 1
        if number <= self.PORTS_12V:
            on = self.ports_enabled[self.PORT_12V]
            volts = self.input_voltage if on else 0.0  # the 12 V output passes the input voltage through
            return bytes((0x10 | number, on, 0)) + _milli(0.0, 2) + _milli(volts, 2)
        return b"\x00"
