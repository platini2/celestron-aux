"""Status panels for the GPS and the focuser, and manual focuser control.

Each monitor learns from every reply its device sends, so values the hand
controller asks for show up too, and can also poll the device periodically.
Polling is off until enabled and never happens in listen-only mode.

The focuser can also be moved, with the same safety as the mount: a move lasts
only while it is confirmed again within a dead-man deadline, and switching the
panel off stops it.
"""

from __future__ import annotations

import logging
import math
import struct
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .bus import AuxBus, BusEvent
from .catalog import Addr, Cmd
from .motion import MotionRefused
from .protocol import AuxPacket
from .protocol.units import bam_to_degrees, format_dms

log = logging.getLogger(__name__)

_TICK = 0.1


class DeviceMonitor:
    """Bus listener for one device, with optional periodic polling."""

    address: int = 0
    interval: float = 1.0

    def __init__(self, bus: AuxBus, *, enabled: bool = False,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._bus = bus
        self._clock = clock
        self._lock = threading.Lock()
        self._enabled = enabled
        self._updated: Optional[float] = None
        self._next_poll = 0.0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"aux-monitor-{self.address:02x}", daemon=True)

    # --- what subclasses provide ---------------------------------------------

    def requests(self) -> List[Tuple[int, bytes]]:
        """(command, data) pairs to send on each poll."""
        raise NotImplementedError

    def update(self, command: int, data: bytes) -> bool:
        """Record a reply from the device; return True if it was used. Called under the lock."""
        raise NotImplementedError

    def values(self) -> Dict[str, Any]:
        """The current readings for display. Called under the lock."""
        raise NotImplementedError

    def on_enable(self) -> None:
        """Called (under the lock) when polling is switched on."""

    def on_disable(self) -> None:
        """Called (without the lock, so it may send) when polling is switched off."""

    def on_tick(self, now: float) -> None:
        """Called (without the lock) about ten times a second."""

    # --- shared behaviour -------------------------------------------------------

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        if not value:
            self.on_disable()
        with self._lock:
            if value and not self._enabled:
                self._next_poll = 0.0  # poll straight away
                self.on_enable()
            self._enabled = value

    def __call__(self, event: BusEvent) -> None:
        packet = event.frame
        if not isinstance(packet, AuxPacket) or packet.source != self.address or not packet.data:
            return
        with self._lock:
            if self.update(packet.command, packet.data):
                self._updated = self._clock()

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            age = None if self._updated is None else round(self._clock() - self._updated, 1)
            return {"enabled": self._enabled, "age": age, **self.values()}

    def poll(self) -> None:
        for command, data in self.requests():
            if not self._bus.send(AuxPacket(Addr.SCANNER, self.address, command, data)):
                return

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()

    def _run(self) -> None:
        while not self._stop.wait(_TICK):
            now = self._clock()
            self.on_tick(now)
            with self._lock:
                due = self._enabled and now >= self._next_poll
                if due:
                    self._next_poll = now + self.interval
            if due and not self._bus.listen_only:
                self.poll()


class GpsMonitor(DeviceMonitor):
    """Link state, fix position, UTC date and time, and satellite information."""

    address = Addr.GPS
    interval = 5.0

    GPS_GET_SAT_INFO = 0x07

    def __init__(self, bus: AuxBus, **kwargs: Any) -> None:
        super().__init__(bus, **kwargs)
        self._linked: Optional[bool] = None
        self._time_valid: Optional[bool] = None
        self._latitude: Optional[float] = None
        self._longitude: Optional[float] = None
        self._time: Optional[Tuple[int, int, int]] = None
        self._date: Optional[Tuple[int, int]] = None
        self._year: Optional[int] = None
        self._satellites: Optional[Tuple[int, int]] = None

    def requests(self) -> List[Tuple[int, bytes]]:
        return [(command, b"") for command in (
            Cmd.GPS_LINKED, Cmd.GPS_TIME_VALID, Cmd.GPS_GET_LAT, Cmd.GPS_GET_LONG,
            Cmd.GPS_GET_YEAR, Cmd.GPS_GET_DATE, Cmd.GPS_GET_TIME, self.GPS_GET_SAT_INFO,
        )]

    def update(self, command: int, data: bytes) -> bool:
        if command == Cmd.GPS_LINKED:
            self._linked = bool(data[0])
        elif command == Cmd.GPS_TIME_VALID:
            self._time_valid = bool(data[0])
        elif command == Cmd.GPS_GET_LAT and len(data) >= 3:
            self._latitude = bam_to_degrees(data[:3])
        elif command == Cmd.GPS_GET_LONG and len(data) >= 3:
            self._longitude = bam_to_degrees(data[:3])
        elif command == Cmd.GPS_GET_TIME and len(data) >= 3:
            self._time = (data[0], data[1], data[2])
        elif command == Cmd.GPS_GET_DATE and len(data) >= 2:
            self._date = (data[0], data[1])
        elif command == Cmd.GPS_GET_YEAR:
            self._year = int.from_bytes(data, "big")
        elif command == self.GPS_GET_SAT_INFO and len(data) >= 2:
            self._satellites = (data[0], data[1])
        else:
            return False
        return True

    def values(self) -> Dict[str, Any]:
        return {
            "linked": self._linked,
            "time_valid": self._time_valid,
            "latitude": _angle(self._latitude),
            "longitude": _angle(self._longitude),
            "utc": self._utc_text(),
            "satellites": None if self._satellites is None else list(self._satellites),
        }

    def _utc_text(self) -> Optional[str]:
        date = time_text = None
        if self._date is not None:
            month, day = self._date
            date = f"{self._year:04d}-{month:02d}-{day:02d}" if self._year else f"{month:02d}-{day:02d}"
        if self._time is not None:
            time_text = "{:02d}:{:02d}:{:02d}".format(*self._time)
        parts = [part for part in (date, time_text) if part]
        return " ".join(parts) if parts else None


class FocuserMonitor(DeviceMonitor):
    """Position, whether a move is in progress, the travel limits, and manual moves."""

    address = Addr.FOCUSER
    interval = 1.0

    FOCUS_GET_POSITION = 0x01
    FOCUS_GOTO = 0x02
    FOCUS_SLEW_DONE = 0x13
    FOCUS_MOVE_POS = 0x24  # one byte: rate 1-9, 0 stops
    FOCUS_MOVE_NEG = 0x25
    FOCUS_GET_LIMITS = 0x2C

    MIN_RATE, MAX_RATE = 1, 9
    MAX_POSITION = 0xFFFFFF  # positions are 24-bit
    DEADMAN = 1.0  # seconds a held move survives without being confirmed again

    def __init__(self, bus: AuxBus, **kwargs: Any) -> None:
        super().__init__(bus, **kwargs)
        self._position: Optional[int] = None
        self._moving: Optional[bool] = None
        self._limits: Optional[Tuple[int, int]] = None
        self._ask_limits = True
        self.deadman = self.DEADMAN
        self._direction = 0  # +1 / -1 while a held move is running
        self._rate = 0
        self._deadline = 0.0

    # --- manual control ---------------------------------------------------------

    def move(self, direction: int, rate: int) -> None:
        """Start or keep moving. Must be repeated within ``deadman`` seconds."""
        if direction not in (1, -1):
            raise ValueError("direction must be 1 or -1")
        if not self.MIN_RATE <= rate <= self.MAX_RATE:
            raise ValueError(f"rate must be between {self.MIN_RATE} and {self.MAX_RATE}")
        with self._lock:
            if not self._enabled:
                raise MotionRefused("Focuser control is off. Switch it on to move the focuser.")
            self._deadline = self._clock() + self.deadman
            if (self._direction, self._rate) == (direction, rate):
                return  # already moving like this: only the deadline needed refreshing
            previous = (self._direction, self._rate)
            self._direction, self._rate = direction, rate
        command = self.FOCUS_MOVE_POS if direction > 0 else self.FOCUS_MOVE_NEG
        if not self._bus.send(AuxPacket(Addr.SCANNER, self.address, command, bytes((rate,)))):
            with self._lock:
                self._direction, self._rate = previous
            raise MotionRefused("The focuser move could not be sent.")

    def stop(self) -> bool:
        """Stop the focuser. Always sends, even if it seems idle."""
        with self._lock:
            self._direction = self._rate = 0
        return self._bus.send(AuxPacket(Addr.SCANNER, self.address, self.FOCUS_MOVE_POS, b"\x00"))

    def goto(self, position: int) -> None:
        """Move to an absolute position, inside the travel limits when they are known."""
        if not 0 <= position <= self.MAX_POSITION:
            raise ValueError(f"position must be between 0 and {self.MAX_POSITION}")
        with self._lock:
            if not self._enabled:
                raise MotionRefused("Focuser control is off. Switch it on to move the focuser.")
            limits = self._limits
            self._next_poll = 0.0  # report the new position promptly
        if limits and not limits[0] <= position <= limits[1]:
            raise ValueError(f"position must be within the travel limits, {limits[0]} to {limits[1]}")
        if not self._bus.send(AuxPacket(Addr.SCANNER, self.address, self.FOCUS_GOTO, position.to_bytes(3, "big"))):
            raise MotionRefused("The focuser move could not be sent.")

    @property
    def moving_by_hand(self) -> bool:
        with self._lock:
            return self._direction != 0

    def on_tick(self, now: float) -> None:
        with self._lock:
            expired = self._direction != 0 and now > self._deadline
        if expired:
            log.warning("focuser move was not confirmed in time; stopping it")
            self.stop()

    def on_disable(self) -> None:
        if self.moving_by_hand:
            self.stop()

    def close(self) -> None:
        super().close()
        if self.moving_by_hand:
            self.stop()

    # --- readings ---------------------------------------------------------------

    def on_enable(self) -> None:
        self._ask_limits = True  # the limits rarely change: read them once per enable

    def requests(self) -> List[Tuple[int, bytes]]:
        commands = [self.FOCUS_GET_POSITION, self.FOCUS_SLEW_DONE]
        with self._lock:
            if self._ask_limits:
                self._ask_limits = False
                commands.append(self.FOCUS_GET_LIMITS)
        return [(command, b"") for command in commands]

    def update(self, command: int, data: bytes) -> bool:
        if command == self.FOCUS_GET_POSITION and len(data) >= 3:
            self._position = int.from_bytes(data[:3], "big")
        elif command == self.FOCUS_SLEW_DONE:
            self._moving = data[0] != 0xFF
        elif command == self.FOCUS_GET_LIMITS and len(data) >= 8:
            self._limits = (int.from_bytes(data[0:4], "big"), int.from_bytes(data[4:8], "big"))
        else:
            return False
        return True

    def values(self) -> Dict[str, Any]:
        return {
            "position": self._position,
            "moving": self._moving,
            "limits": None if self._limits is None else list(self._limits),
            "direction": self._direction,
        }


def _angle(degrees: Optional[float]) -> Optional[Dict[str, Any]]:
    if degrees is None:
        return None
    return {"degrees": round(degrees, 6), "dms": format_dms(degrees)}


# --- dew heater controller -------------------------------------------------------------

def _signed_milli(data: bytes) -> float:
    return int.from_bytes(data, "big", signed=True) / 1000


class DewMonitor(DeviceMonitor):
    """Readings of a dew heater controller (0x17), and its heater, 12 V and LED controls.

    The outputs are switched with 0x14: ports 0 and 1 are taken to be the heaters and
    port 2 the 12 V output (only port 2 is confirmed). The controller does not report a
    heater's on/off state, so the last state commanded on the bus is shown.
    """

    address = Addr.DEW_HEATER
    interval = 2.0
    CHANNELS = 2
    PORT_12V = 2

    def __init__(self, bus: AuxBus, **kwargs: Any) -> None:
        super().__init__(bus, **kwargs)
        self._power: Optional[Tuple[float, float]] = None           # volts, amps
        self._environment: Optional[Tuple[float, float, int]] = None  # temperature, dew point, humidity
        self._heaters: Dict[int, Dict[str, Any]] = {}
        self._outputs: Dict[int, bool] = {}  # port -> on, as last commanded
        self._port_12v: Optional[bool] = None
        self._led: Optional[int] = None

    def requests(self) -> List[Tuple[int, bytes]]:
        return [(0x00, b""), (0x18, b""), (0x12, b"\x00"), (0x12, b"\x01"), (0x11, bytes((self.PORT_12V,))), (0x02, b"")]

    def __call__(self, event: BusEvent) -> None:
        packet = event.frame
        # Also note outputs switched by anyone (CPWI, the hand controller): the controller never reports them.
        if isinstance(packet, AuxPacket) and packet.destination == self.address and packet.command == 0x14 \
                and len(packet.data) >= 2:
            with self._lock:
                self._outputs[packet.data[0]] = bool(packet.data[1])
            return
        super().__call__(event)

    def update(self, command: int, data: bytes) -> bool:
        if command == 0x00 and len(data) >= 4:
            self._power = (_signed_milli(data[0:2]), _signed_milli(data[2:4]))
        elif command == 0x18 and len(data) >= 9:
            self._environment = (_signed_milli(data[0:4]), _signed_milli(data[4:8]), data[8])
        elif command == 0x12 and len(data) >= 10:
            channel = data[0] - 1
            self._heaters[channel] = {"automatic": bool(data[1]), "power": round(data[2] / 2.55),
                                      "amps": _signed_milli(data[3:5]), "aggression": data[5],
                                      "temperature": _signed_milli(data[6:10])}
        elif command == 0x11 and len(data) >= 2 and data[0] & 0xF0 == 0x10:
            self._port_12v = bool(data[1])
        elif command in (0x02, 0x20) and data:
            self._led = data[0]
        else:
            return False
        return True

    def values(self) -> Dict[str, Any]:
        heaters = []
        for channel in range(self.CHANNELS):
            heater = dict(self._heaters.get(channel, {}))
            heater["on"] = self._outputs.get(channel)
            heaters.append(heater)
        port_12v = self._outputs.get(self.PORT_12V, self._port_12v)
        return {
            "voltage": None if self._power is None else self._power[0],
            "current": None if self._power is None else self._power[1],
            "ambient": None if self._environment is None else self._environment[0],
            "dew_point": None if self._environment is None else self._environment[1],
            "humidity": None if self._environment is None else self._environment[2],
            "heaters": heaters, "port_12v": port_12v, "led": self._led,
        }

    # --- controls --------------------------------------------------------------------

    def _command(self, command: int, data: bytes) -> None:
        if not self._bus.send(AuxPacket(Addr.SCANNER, self.address, command, data)):
            raise MotionRefused("The command could not be sent.")
        with self._lock:
            self._next_poll = 0.0  # show the result promptly

    def set_output(self, port: int, on: bool) -> None:
        """Switch a heater (0, 1) or the 12 V output (2) on or off."""
        if port not in (0, 1, self.PORT_12V):
            raise ValueError("port must be 0 or 1 (heaters) or 2 (12 V output)")
        self._command(0x14, bytes((port, 1 if on else 0)))

    def set_automatic(self, channel: int, aggression: int) -> None:
        if channel not in (0, 1) or not 1 <= aggression <= 10:
            raise ValueError("choose heater 1 or 2 and an aggressiveness of 1 to 10")
        self._command(0x16, bytes((channel, aggression)))

    def set_manual(self, channel: int, percent: float) -> None:
        if channel not in (0, 1) or not 0 <= percent <= 100:
            raise ValueError("choose heater 1 or 2 and a power of 0 to 100 %")
        self._command(0x17, bytes((channel, round(percent * 2.55))))

    def set_led(self, brightness: int) -> None:
        if not 0 <= brightness <= 255:
            raise ValueError("the LED brightness is 0 to 255")
        self._command(0x20, bytes((brightness,)))


# --- StarSense plate solving --------------------------------------------------------------

class PlateSolver(DeviceMonitor):
    """Plate solves on a StarSense AutoAlign V2 (0xbd) or AutoGuider (0xb9), on request.

    Start a solve (0x01), ask how it is going (0x02) twice a second until it is solved
    or has failed, then ask for the telescope position (0x4b, V2 only) and the image
    rotation (0x45). Never polls on its own.
    """

    address = Addr.STARSENSE_CAMERA_V2
    CAMERAS = (Addr.STARSENSE_CAMERA_V2, Addr.STARSENSE_AUTOGUIDER)
    CHECK_EVERY = 0.5
    GIVE_UP_AFTER = 40.0

    def __init__(self, bus: AuxBus, **kwargs: Any) -> None:
        super().__init__(bus, **kwargs)
        self._camera = self.address
        self._solving_since: Optional[float] = None
        self._next_check = 0.0
        self._result: Dict[str, Any] = {}
        self._calibrated: Optional[bool] = None
        self._pending: List[Tuple[int, bytes]] = []  # requests to send from the monitor thread

    def requests(self) -> List[Tuple[int, bytes]]:
        return []

    def __call__(self, event: BusEvent) -> None:
        packet = event.frame
        if not isinstance(packet, AuxPacket) or packet.source not in self.CAMERAS or not packet.data:
            return
        with self._lock:
            if self.update(packet.command, packet.data):
                self._updated = self._clock()

    def update(self, command: int, data: bytes) -> bool:
        if command == 0x02 and len(data) >= 9:
            status = data[0]
            if status == 0:
                return True  # still solving
            ours = self._solving_since is not None  # a solve this box started, not CPWI's
            self._solving_since = None
            if status == 1:
                ra, dec = (math.degrees(v) for v in struct.unpack(">ff", data[1:9]))
                self._result = {"status": "Solved", "ra": ra % 360, "dec": dec}
                if ours:  # never add traffic to someone else's alignment (CPWI's ASPA)
                    self._send_later([(0x4B, b""), (0x45, b"")])
            else:
                self._result = {"status": "Failed" if status == 4 else f"Status {status}"}
            return True
        if command == 0x4B and len(data) >= 9 and data[0] == 1:
            ra, dec = (math.degrees(v) for v in struct.unpack(">ff", data[1:9]))
            self._result.update(telescope_ra=ra % 360, telescope_dec=dec)
            return True
        if command == 0x45 and len(data) >= 4:
            self._result["rotation"] = struct.unpack(">f", data[:4])[0]
            return True
        if command == 0x07:
            self._calibrated = bool(data[0])
            return True
        return False

    def _send_later(self, requests: List[Tuple[int, bytes]]) -> None:
        self._pending += requests  # called under the lock, from a bus listener: sent from on_tick

    def values(self) -> Dict[str, Any]:
        return {"camera": self._camera, "solving": self._solving_since is not None,
                "calibrated": self._calibrated, **self._result}

    def solve(self, camera: int) -> None:
        """Start a plate solve on that camera (0xbd or 0xb9)."""
        if camera not in self.CAMERAS:
            raise ValueError("no StarSense camera at that address")
        with self._lock:
            self._camera = camera
            self._result = {"status": "Solving"}
            self._solving_since = self._clock()
            self._next_check = self._clock() + self.CHECK_EVERY
        for command in (0x07, 0x01):
            if not self._bus.send(AuxPacket(Addr.SCANNER, camera, command, b"")):
                with self._lock:
                    self._solving_since, self._result = None, {}
                raise MotionRefused("The plate solve could not be started.")

    def on_tick(self, now: float) -> None:
        with self._lock:
            pending, self._pending = self._pending, []
            since = self._solving_since
            check = since is not None and now >= self._next_check
            if since is not None and check:
                if now - since > self.GIVE_UP_AFTER:
                    self._solving_since, self._result = None, {"status": "No answer"}
                    check = False
                else:
                    self._next_check = now + self.CHECK_EVERY
            camera = self._camera
        requests = pending + ([(0x02, b"")] if check else [])
        for command, data in requests:
            if command == 0x4B and camera != Addr.STARSENSE_CAMERA_V2:
                continue  # the AutoGuider has no telescope position command
            self._bus.send(AuxPacket(Addr.SCANNER, camera, command, data))
