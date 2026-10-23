"""Emulates a telescope mount: its two motor controllers, AZM/RA (0x10) and ALT/Dec (0x11).

The motors answer the commands that apps and hand controllers send, and move
over time: go-to (fast, then a slow approach), the direction buttons at rates
1-9, tracking rates, set position (sync) and the settings CPWI reads when it
connects. Replies copy those of real motors where captures exist (a NexStar SLT
talking to CPWI): set and move commands are acknowledged with 01, as the real
motors do. Unknown commands get no answer, so they show up as unknown traffic.

Where the emulated telescope points is worked out from the two motor angles:

* alt-az: the azimuth axis is vertical, so the angles are azimuth and altitude;
* on a wedge, or on an equatorial (polar) mount, the first axis is tilted by the
  wedge's inclination and rotated to its azimuth, so the telescope turns
  around a polar axis.

When both motors read 0 the telescope points at the initial altitude and azimuth.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .catalog import Addr
from .device_emulators import DeviceEmulator
from .protocol import AuxPacket
from .protocol.units import parse_version

STEPS = 1 << 24                      # 24-bit positions: one turn of the axis
ACK = b"\x01"                        # what real motors answer to set and move commands
SIDEREAL = 15.041                    # arcsec per second
SLEW_FACTOR = 2.0                    # go-to and rate 9 run at twice the mount's rate-9 speed
SLOW_APPROACH = 1.0                  # degrees per second for MTR_GOTO_SLOW
# The real CPC's custom rate 9 and autoguide rate (0x80 = 50 %), as CPWI read them.
CUSTOM_RATE9 = bytes.fromhex("0e74125c")
AUTOGUIDE_RATE = 0x80
# Direction button rates 1-9 as a fraction of sidereal (1-5), then degrees per second.
_BUTTON_RATES = {1: 2 * SIDEREAL / 3600, 2: 4 * SIDEREAL / 3600, 3: 8 * SIDEREAL / 3600,
                 4: 16 * SIDEREAL / 3600, 5: 32 * SIDEREAL / 3600, 6: 0.5, 7: 1.0, 8: 2.0}

ALTAZ, GEM = "altaz", "gem"                       # mount types
NO_WEDGE, OPTIONAL_WEDGE, POLAR = "none", "optional", "polar"


@dataclass(frozen=True)
class MountSpec:
    key: str
    name: str
    model_id: int
    kind: str              # ALTAZ or GEM
    wedge: str             # NO_WEDGE, OPTIONAL_WEDGE or POLAR (equatorial mounts)
    firmware: str          # latest motor firmware known for it
    firmware_source: str
    rate9: float = 4.0     # degrees per second at rate 9 (used for go-to too)


# Wedge compatibility from Celestron's wedges (93665 for SE 6/8, Evolution and
# Origin; HD Pro for CPC, NexStar GPS and Evolution 9.25; the 4/5SE tripod has a
# built-in wedge). Firmware from Celestron's firmware update history, or from
# real mounts for older models that it does not list.
MOUNTS: Tuple[MountSpec, ...] = (
    MountSpec("slt", "NexStar SLT", 0x0783, ALTAZ, NO_WEDGE, "5.18", "a real NexStar SLT", rate9=3.0),
    MountSpec("se45", "NexStar 4/5SE", 0x0B83, ALTAZ, OPTIONAL_WEDGE, "6.19", "assumed: newest legacy version seen"),
    MountSpec("se68", "NexStar 6/8SE", 0x0C82, ALTAZ, OPTIONAL_WEDGE, "6.19", "assumed: newest legacy version seen"),
    MountSpec("cpc", "CPC Deluxe", 0x1189, ALTAZ, OPTIONAL_WEDGE, "6.19", "a real CPC"),
    MountSpec("gt", "GT Series", 0x1283, ALTAZ, NO_WEDGE, "6.19", "assumed: newest legacy version seen"),
    MountSpec("nexstar-gps", "NexStar GPS", 0x0001, ALTAZ, OPTIONAL_WEDGE, "6.19", "assumed: newest legacy version seen"),
    MountSpec("evolution", "NexStar Evolution", 0x1687, ALTAZ, OPTIONAL_WEDGE, "7.18.5028", "Celestron, Feb 2025"),
    MountSpec("origin", "Origin", 0x1D8A, ALTAZ, OPTIONAL_WEDGE, "7.22.4150", "Celestron"),
    MountSpec("advanced-gt", "Advanced GT", 0x0682, GEM, POLAR, "6.19", "assumed: newest legacy version seen"),
    MountSpec("avx", "AVX", 0x1485, GEM, POLAR, "7.15.8270", "Celestron, Nov 2018"),
    MountSpec("cgx", "CGX", 0x1788, GEM, POLAR, "7.18.2214", "Celestron, Jul 2023"),
    MountSpec("cgx-l", "CGX-L", 0x1888, GEM, POLAR, "7.18.5020", "Celestron, Feb 2025"),
)
MOUNTS_BY_KEY: Dict[str, MountSpec] = {spec.key: spec for spec in MOUNTS}


# --- geometry -----------------------------------------------------------------------
# Directions are unit vectors (north, east, up); azimuth runs from north through east.

Vector = Tuple[float, float, float]


def _vector(alt: float, az: float) -> Vector:
    a, z = math.radians(alt), math.radians(az)
    return (math.cos(a) * math.cos(z), math.cos(a) * math.sin(z), math.sin(a))


def _dot(u: Vector, v: Vector) -> float:
    return u[0] * v[0] + u[1] * v[1] + u[2] * v[2]


def _cross(u: Vector, v: Vector) -> Vector:
    return (u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0])


def _wrap(angle: float) -> float:
    """An angle in [0, 360): a hair below 0 becomes 0, never 360."""
    angle %= 360
    return 0.0 if angle >= 360 - 1e-9 else angle


def _normalize(v: Vector) -> Vector:
    length = math.sqrt(_dot(v, v))
    return (v[0] / length, v[1] / length, v[2] / length)


@dataclass(frozen=True)
class Frame:
    """The mount's axes: z along the first (azimuth or polar) axis."""

    x: Vector
    y: Vector
    z: Vector

    @classmethod
    def for_axis(cls, inclination: float, rotation: float) -> "Frame":
        """A first axis raised ``inclination`` degrees above the horizon, towards azimuth ``rotation``."""
        z = _vector(inclination, rotation)
        up, north = (0.0, 0.0, 1.0), (1.0, 0.0, 0.0)
        reference = north if abs(_dot(z, up)) > 0.999 else up  # angle 0: north (alt-az) or the meridian above
        x = _normalize(tuple(r - _dot(reference, z) * c for r, c in zip(reference, z)))  # type: ignore[arg-type]
        return cls(x, _cross(z, x), z)

    def pointing(self, first: float, second: float) -> Tuple[float, float]:
        """Altitude and azimuth for angles around the first axis and above its equator."""
        a, e = math.radians(first), math.radians(second)
        v = tuple(math.cos(e) * math.cos(a) * x + math.cos(e) * math.sin(a) * y + math.sin(e) * z
                  for x, y, z in zip(self.x, self.y, self.z))
        alt = math.degrees(math.asin(max(-1.0, min(1.0, v[2]))))
        return alt, _wrap(math.degrees(math.atan2(v[1], v[0])))

    def angles(self, alt: float, az: float) -> Tuple[float, float]:
        """The reverse of pointing()."""
        v = _vector(alt, az)
        second = math.degrees(math.asin(max(-1.0, min(1.0, _dot(v, self.z)))))
        first = _wrap(math.degrees(math.atan2(_dot(v, self.y), _dot(v, self.x))))
        return first, second


# --- one motor controller --------------------------------------------------------------

@dataclass
class MotorAxis:
    """A motor's angle, moving over time: a go-to, a button move and tracking."""

    clock: Callable[[], float]
    rate9: float
    degrees: float = 0.0
    button: float = 0.0                   # degrees per second, signed
    tracking: float = 0.0                 # degrees per second, signed
    target: Optional[float] = None
    goto_speed: float = 0.0
    updated: float = field(default=0.0)
    # User limits (MTR_SET_USER_LIMIT_MIN/MAX, enabled with MTR_SET_USER_LIMIT_ENABLED), as the
    # app sets them: signed degrees around the axis's zero. Off until the app turns them on.
    limit_min: float = -180.0
    limit_max: float = 180.0
    limits_on: bool = False

    def __post_init__(self) -> None:
        self.updated = self.clock()

    @property
    def moving(self) -> bool:
        """Slewing (a go-to or a button), as MTR_IS_GOTO_OVER reports; tracking does not count."""
        return self.target is not None or self.button != 0

    def advance(self, now: Optional[float] = None) -> None:
        now = self.clock() if now is None else now
        elapsed, self.updated = now - self.updated, now
        if elapsed <= 0:
            return
        # The signed travel, not just the end point: a move can wrap past 0/360, and its
        # direction decides which limit it can reach.
        if self.target is not None:
            remaining = ((self.target - self.degrees + 180) % 360) - 180
            step = self.goto_speed * elapsed
            travel = remaining if abs(remaining) <= step else math.copysign(step, remaining)
            if abs(remaining) <= step:
                self.target = None
        else:
            travel = (self.button + self.tracking) * elapsed
        before = self.degrees
        self.degrees = before + travel
        self._keep_within_limits(before, travel)
        self.degrees %= 360

    def _keep_within_limits(self, before: float, travel: float) -> None:
        """Stop at a user limit the motion crossed, like a real motor controller."""
        if not self.limits_on:
            return
        old = ((before + 180) % 360) - 180
        new = old + travel
        if self.limit_min - 1e-9 <= old <= self.limit_max + 1e-9:  # it was inside: it may not leave
            if new > self.limit_max:
                self.degrees = self.limit_max
            elif new < self.limit_min:
                self.degrees = self.limit_min
            else:
                return
            self.button, self.target, self.tracking = 0.0, None, 0.0

    def goto(self, degrees: float, speed: float) -> None:
        self.target, self.goto_speed, self.button = degrees % 360, speed, 0.0

    def move(self, rate: int, direction: int) -> None:
        """A direction button. Rate 0 only stops button motion: a go-to carries on, as on a real
        motor (CPWI sends rate 0 in its normal traffic, also while a go-to runs)."""
        if rate == 0:
            self.button = 0.0
            return
        self.target = None  # pressing a button takes over from a go-to
        self.button = direction * (self.rate9 * SLEW_FACTOR if rate >= 9 else _BUTTON_RATES.get(rate, 0.0))

    def set_position(self, degrees: float) -> None:
        self.degrees, self.target = degrees % 360, None



    @property
    def steps(self) -> int:
        return int(round(self.degrees / 360 * STEPS)) % STEPS


def _angle(data: bytes) -> float:
    """A 16- or 24-bit position as degrees."""
    return int.from_bytes(data[:3], "big") / (1 << (8 * min(len(data), 3))) * 360


def _track_rate(data: bytes) -> float:
    """MTR_PTRACK/NTRACK: 24-bit arcsec/1024 per second, or 16-bit 0xffff/0xfffe/0xfffd for
    the sidereal, solar and lunar rates. Returns degrees per second."""
    value = int.from_bytes(data[:3], "big")
    if len(data) == 2 and value >= 0xFFFD:
        arcsec = {0xFFFF: SIDEREAL, 0xFFFE: 15.0, 0xFFFD: 14.685}[value]
    else:
        arcsec = value / 1024
    return arcsec / 3600


# --- the mount ---------------------------------------------------------------------------

@dataclass
class MountSetup:
    """What the emulated telescope is and how it is set up."""

    model: str = "slt"
    wedge: bool = False
    initial_alt: float = 0.0
    initial_az: float = 0.0
    inclination: float = 40.0  # the wedge or polar axis: normally the latitude
    rotation: float = 0.0      # its azimuth: 0 towards north, 180 towards south

    def validated(self) -> "MountSetup":
        spec = MOUNTS_BY_KEY.get(self.model)
        if spec is None:
            raise ValueError("unknown mount; choose one of: " + ", ".join(MOUNTS_BY_KEY))
        if not -90 <= self.initial_alt <= 90:
            raise ValueError("the initial altitude must be between -90 and 90 degrees")
        if not 0 <= self.inclination <= 90:
            raise ValueError("the wedge inclination must be between 0 and 90 degrees")
        if self.wedge and spec.wedge == NO_WEDGE:
            raise ValueError(f"the {spec.name} cannot be used on a wedge")
        return MountSetup(self.model, self.wedge or spec.wedge == POLAR, self.initial_alt,
                          self.initial_az % 360, self.inclination, self.rotation % 360)

    @property
    def spec(self) -> MountSpec:
        return MOUNTS_BY_KEY[self.model]


class MountEmulator(DeviceEmulator):
    """Two motor controllers that move like a real mount's."""

    addresses = (Addr.AZM_MOTOR, Addr.ALT_MOTOR)
    versions = {Addr.AZM_MOTOR: parse_version("5.18"), Addr.ALT_MOTOR: parse_version("5.18")}

    def __init__(self, send: Callable[[AuxPacket], object], clock: Callable[[], float] = time.monotonic) -> None:
        super().__init__(send, clock)
        self.configure(MountSetup())

    def configure(self, setup: MountSetup, azm_version: Optional[str] = None, alt_version: Optional[str] = None) -> None:
        """Set up the telescope (a new power-on: both motors read 0). ValueError if invalid."""
        setup = setup.validated()
        versions = {Addr.AZM_MOTOR: parse_version(azm_version or setup.spec.firmware),
                    Addr.ALT_MOTOR: parse_version(alt_version or setup.spec.firmware)}
        with self._lock:
            self.setup = setup
            self.versions = versions
            self.frame = (Frame.for_axis(setup.inclination, setup.rotation) if setup.wedge
                          else Frame.for_axis(90.0, 0.0))
            self._origin = self.frame.angles(setup.initial_alt, setup.initial_az)  # motor angles 0 = here
            self.axes = {address: MotorAxis(self._clock, setup.spec.rate9) for address in self.addresses}
            self.backlash = {address: [0, 0] for address in self.addresses}  # positive, negative (real: 0)
            self.approach_negative = {address: 0 for address in self.addresses}  # real: positive
            self.cordwrap = {"on": False, "position": 0}
            self.autoguide_rate = AUTOGUIDE_RATE
            self.custom_rate9 = {address: CUSTOM_RATE9 for address in self.addresses}
            self.custom_rate9_enabled = {address: 0 for address in self.addresses}
            self.user_limits = {address: [bytes(2), bytes(2)] for address in self.addresses}  # off, 0 and 0

    # --- where it points ----------------------------------------------------------------

    def pointing_for(self, azm: float, alt: float) -> Tuple[float, float]:
        return self.frame.pointing(self._origin[0] + azm, self._origin[1] + alt)

    def _advance(self) -> None:
        for axis in self.axes.values():
            axis.advance()

    def sky_pointing(self) -> Optional[Tuple[float, float, bool]]:
        """Altitude, azimuth and whether it is alt-az, while emulating; else None."""
        with self._lock:
            if not self._enabled:
                return None
            self._advance()
            alt, az = self.pointing_for(self.axes[Addr.AZM_MOTOR].degrees, self.axes[Addr.ALT_MOTOR].degrees)
            return alt, az, not self.setup.wedge

    def snapshot(self) -> dict:
        with self._lock:
            self._advance()
            azm, alt = self.axes[Addr.AZM_MOTOR], self.axes[Addr.ALT_MOTOR]
            pointing = self.pointing_for(azm.degrees, alt.degrees)
            target = None
            if azm.target is not None or alt.target is not None:
                target = self.pointing_for(azm.target if azm.target is not None else azm.degrees,
                                           alt.target if alt.target is not None else alt.degrees)
            spec = self.setup.spec
            return {
                "enabled": self._enabled, "model": spec.key, "name": spec.name, "wedge": self.setup.wedge,
                "axes": {"azm": round(azm.degrees, 4), "alt": round(alt.degrees, 4)},
                "alt": round(pointing[0], 4), "az": round(pointing[1], 4),
                "target": None if target is None else {"alt": round(target[0], 4), "az": round(target[1], 4)},
                "moving": azm.moving or alt.moving, "tracking": azm.tracking != 0 or alt.tracking != 0,
            }

    # --- the motor protocol (called with the lock held) ------------------------------------

    def answer(self, address: int, command: int, data: bytes) -> Optional[bytes]:
        axis = self.axes[address]
        self._advance()
        if command == 0x01:                                   # MTR_GET_POSITION
            return axis.steps.to_bytes(3, "big")
        if command in (0x02, 0x17) and len(data) >= 2:       # MTR_GOTO_FAST / MTR_GOTO_SLOW
            axis.goto(_angle(data), axis.rate9 * SLEW_FACTOR if command == 0x02 else SLOW_APPROACH)
            return ACK
        if command == 0x04 and len(data) >= 2:                # MTR_SET_POSITION (sync)
            axis.set_position(_angle(data))
            return ACK
        if command == 0x05:                                   # MTR_GET_MODEL
            return self.setup.spec.model_id.to_bytes(2, "big")
        if command in (0x06, 0x07) and len(data) >= 2:        # tracking, positive / negative
            axis.tracking = _track_rate(data) * (1 if command == 0x06 else -1)
            return ACK
        if command in (0x24, 0x25) and data:                  # direction buttons, rate 0-9
            axis.move(min(data[0], 9), 1 if command == 0x24 else -1)
            return ACK
        if command == 0x13:                                   # MTR_IS_GOTO_OVER
            return b"\x00" if axis.moving else b"\xff"
        if command == 0x0B:                                   # MTR_MVSWITCH: level (ALT): go to 0
            axis.goto(0.0, axis.rate9 * SLEW_FACTOR)
            return ACK
        if command == 0x12:                                   # MTR_IS_MVSWITCH_OVER
            return b"\x00" if axis.moving else b"\xff"
        if command in (0x10, 0x11) and data:                  # set backlash, positive / negative
            self.backlash[address][command - 0x10] = data[0]
            return ACK
        if command in (0x40, 0x41):                           # get backlash
            return bytes((self.backlash[address][command - 0x40],))
        if command == 0xFC:                                   # MTR_IS_APPROACH_DIR_NEG
            return bytes((self.approach_negative[address],))
        if command == 0xFD and data:
            self.approach_negative[address] = 1 if data[0] else 0
            return ACK
        if command in (0x38, 0x39):                           # cord wrap on / off
            self.cordwrap["on"] = command == 0x38
            return ACK
        if command == 0x3A and len(data) >= 3:
            self.cordwrap["position"] = int.from_bytes(data[:3], "big")
            return ACK
        if command == 0x3B:
            return b"\xff" if self.cordwrap["on"] else b"\x00"
        if command == 0x3C:
            return self.cordwrap["position"].to_bytes(3, "big")
        if command == 0x46 and data:                          # autoguide rate
            self.autoguide_rate = data[0]
            return ACK
        if command == 0x47:
            return bytes((self.autoguide_rate,))
        if command == 0x26:                                   # MTR_AUX_GUIDE: accepted, not modelled
            return ACK
        if command == 0x27:                                   # MTR_IS_AUX_GUIDE_ACTIVE
            return b"\x00"
        if command in (0x1A, 0x1B) and len(data) >= 2:        # user limits, minimum / maximum
            # Assumed 16-bit positions, like a short go-to (no capture of CPWI setting them yet).
            limit = ((_angle(data[:2]) + 180) % 360) - 180
            if command == 0x1A:
                axis.limit_min = limit
            else:
                axis.limit_max = limit
            self.user_limits[address][command - 0x1A] = bytes(data[:2])
            return ACK
        if command in (0x1C, 0x1D):
            return self.user_limits[address][command - 0x1C]
        if command == 0x1E:                                   # are the user limits on?
            return b"\x01" if axis.limits_on else b"\x00"
        if command == 0x1F and data:
            axis.limits_on = bool(data[0])
            return ACK
        if command == 0x20 and len(data) >= 4:                # custom rate 9: set, get, enable, is enabled
            self.custom_rate9[address] = bytes(data[:4])
            return ACK
        if command == 0x21:
            return self.custom_rate9[address]
        if command == 0x22 and data:
            self.custom_rate9_enabled[address] = 1 if data[0] else 0
            return ACK
        if command == 0x23:
            return bytes((self.custom_rate9_enabled[address],))
        return None


def mount_table() -> List[dict]:
    """The supported mounts, for the page."""
    return [{"key": s.key, "name": s.name, "model_id": s.model_id, "kind": s.kind, "wedge": s.wedge,
             "firmware": s.firmware, "firmware_source": s.firmware_source} for s in MOUNTS]
