"""The telescope's sky as the traffic on the bus shows it.

Everything the page's Earth and sky spheres draw can be worked out from what
the devices say to each other:

* where the telescope is: a real GPS's answers, or the location CPWI sends a
  StarSense AutoAlign V2;
* where its plates are: every StarSense star list is plate-solved on Hipparcos
  (:mod:`star_solver`), and a StarSense V2 reports its own solves;
* where it points and where it has been: the motors' positions, which CPWI and
  the hand controller ask for all the time.

The motors' positions are angles of the motors, not of the sky. Each plate
solve ties the two together: at that moment the motors read so much and the
telescope looked there. One solve fixes where the axes' zeros are, taking the
mount as level and alt-az; with three or more, the tilt of the first axis is
fitted too (a mount off level, a wedge or an equatorial mount), the way the
camera's own alignment does. From then on every position is a point on the sky.

The scanner's own emulators talk on the bus like real devices, and what they
say counts too: with emulators running this is the emulators' data as it
reaches the bus, which checks the emulators the way CPWI sees them. Each piece
is labelled with whether an emulator said it (a device counts as emulated
while its emulator runs), so the page can tell real data from emulated.
"""

from __future__ import annotations

import math
import struct
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Deque, Dict, List, Optional, Sequence, Tuple

from .bus import BusEvent
from .catalog import Addr, Cmd
from .mount_emulator import Frame
from .protocol import AuxPacket, StarSenseFrame
from .protocol.units import bam_to_degrees
from .sky import altaz_to_j2000, j2000_to_altaz
from .star_solver import StarListSolver, list_id

STEPS = 1 << 24                  # 24-bit motor positions: one turn
MOTOR_GET_POSITION = 0x01
V2_START_SOLVE, V2_PLATE_STATUS, V2_SET_GEOLOCATION, V2_SET_TIME_NOW = 0x01, 0x02, 0x31, 0x32
EPOCH_2000 = datetime(2000, 1, 1, tzinfo=timezone.utc).timestamp()
CLOCK_SLACK = 120.0              # seconds: a device's clock this close to the scanner's is taken as the same
V2_SOLVED = 1
PATH_STEP = 0.05                 # degrees the motors turn before the path gets a new point
MAX_PATH = 20000                 # motor readings kept for the path
MAX_PLATES = 64
CAPTURE_WINDOW = 60.0            # seconds: the motors at a capture command stand for the star list that follows
LEVEL_IS_GOOD = 0.5              # degrees rms: a level alt-az fit this good is kept
SIGN_MARGIN = 0.5                # degrees rms another axis direction must win by

Vector = Tuple[float, float, float]
Sample = Tuple[float, float, float, float]  # motor azm, motor alt, sky alt, sky az (degrees)


# --- the mount model ---------------------------------------------------------------------

@dataclass(frozen=True)
class MountModel:
    """How the motors' angles turn into altitude and azimuth.

    The first axis points ``inclination`` degrees above the horizon towards azimuth
    ``rotation`` (90: a level alt-az mount). The telescope's angle around it is
    ``azm_sign`` x azm motor + ``azm_offset``; its angle above that axis's equator is
    ``alt_sign`` x alt motor + ``alt_offset``.
    """

    inclination: float
    rotation: float
    azm_offset: float
    alt_offset: float
    azm_sign: int
    alt_sign: int
    rms: float          # degrees, between the fitted and the solved plates
    plates: int

    @property
    def kind(self) -> str:
        return "alt-az" if self.inclination >= 89.999 else "tilted"

    def pointing(self, azm: float, alt: float) -> Tuple[float, float]:
        frame = Frame.for_axis(self.inclination, self.rotation)
        return frame.pointing(self.azm_sign * azm + self.azm_offset, self.alt_sign * alt + self.alt_offset)

    def json(self) -> Dict[str, object]:
        return {"kind": self.kind, "inclination": round(self.inclination, 3), "rotation": round(self.rotation % 360, 3),
                "rms": round(self.rms, 3), "plates": self.plates}


def _vector(alt: float, az: float) -> Vector:
    a, z = math.radians(alt), math.radians(az)
    return (math.cos(a) * math.cos(z), math.cos(a) * math.sin(z), math.sin(a))


def _separation(a: Tuple[float, float], b: Tuple[float, float]) -> float:
    u, v = _vector(*a), _vector(*b)
    dot = max(-1.0, min(1.0, u[0] * v[0] + u[1] * v[1] + u[2] * v[2]))
    return math.degrees(math.acos(dot))


def _rms(model: MountModel, samples: Sequence[Sample]) -> float:
    errors = [_separation(model.pointing(azm, alt), (sky_alt, sky_az)) for azm, alt, sky_alt, sky_az in samples]
    return math.sqrt(sum(e * e for e in errors) / len(errors))


def _for_axis(samples: Sequence[Sample], inclination: float, rotation: float,
              azm_sign: int, alt_sign: int) -> MountModel:
    """The best model with the first axis pointing this way: the zeros are averages.

    Each direction is reached two ways: at (first, second) and, over the top of the
    first axis, at (first + 180, 180 - second), as a telescope past the zenith or an
    equatorial mount after a meridian flip. Each plate takes the way that agrees
    with the others.
    """
    frame = Frame.for_axis(inclination, rotation)
    ways = []
    for azm, alt, sky_alt, sky_az in samples:
        first, second = frame.angles(sky_alt, sky_az)
        ways.append([(math.radians(f - azm_sign * azm), math.radians(e - alt_sign * alt))
                     for f, e in ((first, second), (first + 180, 180 - second))])

    def nearest(reference: Tuple[float, float]) -> Tuple[float, float]:
        chosen = [min(pair, key=lambda w: _apart(w[0], reference[0]) + _apart(w[1], reference[1])) for pair in ways]
        return _mean_angle([w[0] for w in chosen]), _mean_angle([w[1] for w in chosen])

    best: Optional[MountModel] = None
    for start in ways[0]:
        offsets = nearest(start)
        offsets = nearest((math.radians(offsets[0]), math.radians(offsets[1])))
        model = MountModel(inclination, rotation, offsets[0], offsets[1], azm_sign, alt_sign, 0.0, len(samples))
        model = MountModel(**{**model.__dict__, "rms": _rms(model, samples)})
        if best is None or model.rms < best.rms:
            best = model
    assert best is not None
    return best


def _apart(a: float, b: float) -> float:
    """How far apart two angles in radians are, the short way round."""
    return abs(math.remainder(a - b, 2 * math.pi))


def _mean_angle(radians: Sequence[float]) -> float:
    """The mean of angles, in degrees, minding the wrap at 360."""
    return math.degrees(math.atan2(sum(map(math.sin, radians)), sum(map(math.cos, radians))))


def _level(samples: Sequence[Sample], azm_sign: int, alt_sign: int) -> MountModel:
    return _for_axis(samples, 90.0, 0.0, azm_sign, alt_sign)


def _nelder_mead(f: Callable[[List[float]], float], start: List[float], step: List[float],
                 iterations: int = 300) -> Tuple[List[float], float]:
    points = [list(start)] + [[s + (step[i] if i == j else 0.0) for i, s in enumerate(start)] for j in range(len(start))]
    values = [f(p) for p in points]
    for _ in range(iterations):
        order = sorted(range(len(points)), key=values.__getitem__)
        points, values = [points[i] for i in order], [values[i] for i in order]
        if values[-1] - values[0] < 1e-10:
            break
        centre = [sum(p[i] for p in points[:-1]) / (len(points) - 1) for i in range(len(start))]
        worst = points[-1]
        reflected = [c + (c - w) for c, w in zip(centre, worst)]
        fr = f(reflected)
        if fr < values[0]:
            expanded = [c + 2 * (c - w) for c, w in zip(centre, worst)]
            fe = f(expanded)
            points[-1], values[-1] = (expanded, fe) if fe < fr else (reflected, fr)
        elif fr < values[-2]:
            points[-1], values[-1] = reflected, fr
        else:
            contracted = [c + 0.5 * (w - c) for c, w in zip(centre, worst)]
            fc = f(contracted)
            if fc < values[-1]:
                points[-1], values[-1] = contracted, fc
            else:
                best = points[0]
                points = [best] + [[b + 0.5 * (p - b) for b, p in zip(best, q)] for q in points[1:]]
                values = [values[0]] + [f(p) for p in points[1:]]
    i = min(range(len(points)), key=values.__getitem__)
    return points[i], values[i]


def _tilted(samples: Sequence[Sample]) -> MountModel:
    """The best model with the first axis pointing anywhere: a coarse search over where it
    points, each motor either way, then a fine one around the best."""
    best: Optional[MountModel] = None
    for azm_sign in (1, -1):
        for alt_sign in (1, -1):
            for inclination in range(-85, 90, 10):
                for rotation in range(0, 360, 15):
                    model = _for_axis(samples, float(inclination), float(rotation), azm_sign, alt_sign)
                    if best is None or model.rms < best.rms:
                        best = model
    assert best is not None
    signs = best.azm_sign, best.alt_sign

    def cost(p: List[float]) -> float:
        return _for_axis(samples, max(-90.0, min(90.0, p[0])), p[1], *signs).rms

    found, _ = _nelder_mead(cost, [best.inclination, best.rotation], [4.0, 8.0])
    found, _ = _nelder_mead(cost, found, [0.3, 0.6])
    return _for_axis(samples, max(-90.0, min(90.0, found[0])), found[1] % 360, *signs)


def fit_mount(samples: Sequence[Sample]) -> Optional[MountModel]:
    """The mount model that best explains ``samples`` (motor angles and where the plates were).

    One plate: a level alt-az mount, motors turning the usual way. Two or more: each
    motor may turn either way. If a level mount cannot explain them (more than
    half a degree off), the first axis may tilt too, given at least three plates
    (two plates fit any four-parameter model exactly, so they prove nothing).
    """
    if not samples:
        return None
    best = _level(samples, 1, 1)
    if len(samples) >= 2:
        for signs in ((1, -1), (-1, 1), (-1, -1)):
            other = _level(samples, *signs)
            if other.rms < best.rms - SIGN_MARGIN:
                best = other
    if len(samples) >= 3 and best.rms > LEVEL_IS_GOOD:
        tilted = _tilted(samples)
        if tilted.rms < best.rms / 2:
            return tilted
    return best


# --- the listener ------------------------------------------------------------------------

@dataclass
class _Plate:
    kind: str                       # "V1 capture" or "V2 solve"
    number: int
    time: float                     # Unix time it was taken
    axes: Optional[Tuple[float, float]]  # the motors then, azm and alt degrees
    emulated: bool = False          # taken by one of the scanner's emulators
    key: Optional[str] = None       # a V1 star list's id, solved by the star solver
    sky: Optional[Tuple[float, float]] = None  # RA, Dec (J2000), once known


def _iso(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="milliseconds")


class SkyObserver:
    """Bus listener that keeps what the devices on the bus say about the sky: location,
    motor positions and plate solves. ``view()`` turns them into what the spheres draw."""

    def __init__(self, solver: StarListSolver, emulated: Callable[[int], bool],
                 wall_time: Callable[[float], float]) -> None:
        self._solver = solver
        self._emulated = emulated     # whether the device at this address is one of the scanner's emulators
        self._wall_time = wall_time   # a bus timestamp as Unix time
        self._lock = threading.Lock()
        self._azm: Optional[float] = None
        self._alt: Optional[float] = None
        self._axes_time = 0.0
        self._path: Deque[Tuple[float, float, float]] = deque(maxlen=MAX_PATH)  # Unix time, azm, alt
        self._gps: List[Optional[float]] = [None, None]
        self._cpwi_location: Optional[Tuple[float, float]] = None
        self._plates: Deque[_Plate] = deque(maxlen=MAX_PLATES)
        self._count = 0
        self._capture: Optional[Tuple[float, Optional[Tuple[float, float]]]] = None   # V1 capture command
        self._solve: Optional[Tuple[float, Optional[Tuple[float, float]]]] = None     # V2 solve start
        self._last_time: Optional[float] = None
        self._motors_emulated = False   # the last motor positions came from the emulated telescope
        self._gps_emulated = False      # the GPS location came from the GPS emulator
        # A replayed recording runs at replay time; a clock in the traffic (CPWI telling the camera
        # the time, a GPS's answers) gives the real time, as an offset to add.
        self._time_offset = 0.0
        self._time_from: Optional[str] = None  # whose clock the time comes from, once one is seen
        self._gps_date: List[Optional[int]] = [None, None, None]  # year, month, day
        self._model_cache: Tuple[Optional[Tuple[Sample, ...]], Optional[MountModel]] = (None, None)

    # -- listening

    def __call__(self, event: BusEvent) -> None:
        frame = event.frame
        stamp = self._wall_time(event.timestamp)
        with self._lock:
            if isinstance(frame, StarSenseFrame):
                self._star_list(frame, stamp)
            elif isinstance(frame, AuxPacket):
                self._packet(frame, stamp)
            else:
                return
            self._last_time = stamp

    def _axes(self) -> Optional[Tuple[float, float]]:
        return None if self._azm is None or self._alt is None else (self._azm, self._alt)

    def _packet(self, packet: AuxPacket, stamp: float) -> None:
        source, destination, command, data = packet.source, packet.destination, packet.command, packet.data
        if source in (Addr.AZM_MOTOR, Addr.ALT_MOTOR) and command == MOTOR_GET_POSITION and len(data) == 3:
            if destination in (Addr.AZM_MOTOR, Addr.ALT_MOTOR):
                return  # one motor answering the other
            degrees = int.from_bytes(data, "big") * 360.0 / STEPS
            if source == Addr.AZM_MOTOR:
                self._azm = degrees
            else:
                self._alt = degrees
            self._axes_time = stamp
            self._motors_emulated = self._emulated(source)
            self._extend_path(stamp)
        elif source == Addr.GPS and command in (Cmd.GPS_GET_LAT, Cmd.GPS_GET_LONG) and len(data) >= 3:
            self._gps[command - Cmd.GPS_GET_LAT] = bam_to_degrees(data[:3])
            self._gps_emulated = self._emulated(Addr.GPS)
        elif destination == Addr.STARSENSE_CAMERA_V2 and command == V2_SET_GEOLOCATION and len(data) >= 8:
            # The location CPWI knows: real even when the camera it tells is emulated.
            latitude, longitude = struct.unpack(">ff", data[:8])
            if -90 <= latitude <= 90 and -180 <= longitude <= 360:
                self._cpwi_location = (float(latitude), float(longitude))
        elif destination == Addr.STARSENSE_CAMERA_V2 and command == V2_SET_TIME_NOW and len(data) >= 4:
            self._clock_seen(EPOCH_2000 + int.from_bytes(data[:4], "big"), stamp, "CPWI")
        elif source == Addr.GPS and command in (Cmd.GPS_GET_YEAR, Cmd.GPS_GET_DATE, Cmd.GPS_GET_TIME):
            self._gps_clock(command, data, stamp)
        elif destination == Addr.STARSENSE_CAMERA and command == Cmd.SS_START_CAPTURE:
            self._capture = (stamp, self._axes())
        elif destination == Addr.STARSENSE_CAMERA_V2 and command == V2_START_SOLVE and not data:
            self._solve = (stamp, self._axes())
        elif (source == Addr.STARSENSE_CAMERA_V2 and command == V2_PLATE_STATUS and len(data) >= 9
              and data[0] == V2_SOLVED):
            self._v2_solved(data, stamp)

    def _clock_seen(self, real: float, stamp: float, source: str) -> None:
        self._time_from = source
        offset = real - stamp
        self._time_offset = offset if abs(offset) > CLOCK_SLACK else 0.0

    def _gps_clock(self, command: int, data: bytes, stamp: float) -> None:
        if command == Cmd.GPS_GET_YEAR and len(data) >= 2:
            self._gps_date[0] = int.from_bytes(data[:2], "big")
        elif command == Cmd.GPS_GET_DATE and len(data) >= 2:
            self._gps_date[1], self._gps_date[2] = data[0], data[1]
        elif command == Cmd.GPS_GET_TIME and len(data) >= 3 and None not in self._gps_date:
            year, month, day = self._gps_date
            try:
                real = datetime(year, month, day, data[0], data[1], data[2], tzinfo=timezone.utc)  # type: ignore[arg-type]
            except ValueError:
                return  # no fix yet: the GPS answers zeros
            self._clock_seen(real.timestamp(), stamp, "GPS emulator" if self._emulated(Addr.GPS) else "GPS")

    def _extend_path(self, stamp: float) -> None:
        axes = self._axes()
        if axes is None:
            return
        if self._path:
            _, azm, alt = self._path[-1]
            if abs(((axes[0] - azm + 180) % 360) - 180) + abs(((axes[1] - alt + 180) % 360) - 180) < PATH_STEP:
                return
        self._path.append((stamp, axes[0], axes[1]))

    def _taken(self, start: Optional[Tuple[float, Optional[Tuple[float, float]]]],
               stamp: float) -> Tuple[float, Optional[Tuple[float, float]]]:
        """When a plate was taken and the motors then: at the command that started it, if recent."""
        if start is not None and stamp - start[0] <= CAPTURE_WINDOW and start[1] is not None:
            return start
        return stamp, self._axes()

    def _star_list(self, frame: StarSenseFrame, stamp: float) -> None:
        key = list_id(frame.payload)
        if any(plate.key == key for plate in self._plates):
            return  # the camera sends the same list again when asked twice
        taken, axes = self._taken(self._capture, stamp)
        self._capture = None
        self._count += 1
        self._plates.append(_Plate("V1 capture", self._count, taken, axes, self._emulated(Addr.STARSENSE_CAMERA), key=key))

    def _v2_solved(self, data: bytes, stamp: float) -> None:
        ra, dec = (math.degrees(v) for v in struct.unpack(">ff", data[1:9]))
        sky = (ra % 360, dec)
        last = next((p for p in reversed(self._plates) if p.kind == "V2 solve"), None)
        if self._solve is None and last is not None and last.sky == sky:
            return  # asked again for a solve already kept
        taken, axes = self._taken(self._solve, stamp)
        self._solve = None
        self._count += 1
        self._plates.append(_Plate("V2 solve", self._count, taken, axes, self._emulated(Addr.STARSENSE_CAMERA_V2), sky=sky))

    # -- what the page shows

    def location(self) -> Optional[Tuple[Tuple[float, float], str]]:
        with self._lock:
            if self._gps[0] is not None and self._gps[1] is not None:
                return (self._gps[0], self._gps[1]), "GPS emulator" if self._gps_emulated else "GPS"
            if self._cpwi_location is not None:
                return self._cpwi_location, "CPWI"
            return None

    def has_data(self) -> bool:
        with self._lock:
            return bool(self._plates or self._path or self._cpwi_location
                        or (self._gps[0] is not None and self._gps[1] is not None))

    def clear_path(self) -> None:
        with self._lock:
            self._path.clear()
            axes = self._axes()
            if axes is not None:
                self._path.append((self._axes_time, axes[0], axes[1]))

    def _resolved(self) -> List[_Plate]:
        plates = list(self._plates)
        for plate in plates:
            if plate.sky is None and plate.key is not None:
                result = self._solver.result(plate.key)
                if result is not None and result.get("status") == "solved":
                    plate.sky = (float(result["ra"]), float(result["dec"]))  # type: ignore[arg-type]
        return plates

    def _fit(self, samples: List[Sample]) -> Optional[MountModel]:
        """fit_mount, done again only when the plates change (a tilted fit takes a moment)."""
        key = tuple((round(a, 4), round(b, 4), round(c, 4), round(d, 4)) for a, b, c, d in samples)
        with self._lock:
            if self._model_cache[0] == key:
                return self._model_cache[1]
        model = fit_mount(samples)
        with self._lock:
            self._model_cache = (key, model)
        return model

    def view(self, max_path: int = 3000) -> dict:
        """Where the telescope is and points, its path and its plates, from the bus traffic."""
        found = self.location()
        with self._lock:
            plates = self._resolved()
            path = list(self._path)
            axes, axes_time = self._axes(), self._axes_time
            shift = self._time_offset
            time_from = self._time_from
            now = (self._last_time if self._last_time is not None else time.time()) + shift
            emulated = (["telescope"] if self._motors_emulated and axes is not None else []) \
                + (["GPS"] if self._gps_emulated and self._gps[0] is not None else []) \
                + sorted({"StarSense " + ("V1" if p.kind == "V1 capture" else "V2") for p in plates if p.emulated})
            plate_status = {plate.key: (self._solver.result(plate.key) or {}).get("status")
                            for plate in plates if plate.key is not None}
        site = None if found is None else {"lat": found[0][0], "lon": found[0][1], "from": found[1]}
        model = None
        if found is not None:
            (latitude, longitude), _ = found
            samples = []
            for plate in plates:
                if plate.sky is not None and plate.axes is not None:
                    when = datetime.fromtimestamp(plate.time + shift, timezone.utc)
                    sky_alt, sky_az = j2000_to_altaz(plate.sky[0], plate.sky[1], latitude, longitude, when)
                    samples.append((plate.axes[0], plate.axes[1], sky_alt, sky_az))
            model = self._fit(samples)
        pointing = None
        trail: List[List[float]] = []
        if model is not None and found is not None:
            (latitude, longitude), _ = found
            if axes is not None:
                alt, az = model.pointing(*axes)
                ra, dec = altaz_to_j2000(alt, az, latitude, longitude, datetime.fromtimestamp(axes_time + shift, timezone.utc))
                pointing = {"alt": round(alt, 4), "az": round(az % 360, 4), "alt_az": model.kind == "alt-az",
                            "ra": round(ra % 360, 4), "dec": round(dec, 4)}
            stride = max(1, len(path) // max_path)
            for stamp, azm, alt_motor in path[::stride]:
                alt, az = model.pointing(azm, alt_motor)
                ra, dec = altaz_to_j2000(alt, az, latitude, longitude, datetime.fromtimestamp(stamp + shift, timezone.utc))
                trail.append([round(ra % 360, 3), round(dec, 3)])
        return {
            "time": _iso(now),
            "site": site,
            "pointing": pointing,
            "trail": trail,
            "plates": [{"kind": p.kind, "number": p.number, "time": _iso(p.time + shift),
                        "sky": None if p.sky is None else {"ra": round(p.sky[0], 4), "dec": round(p.sky[1], 4)},
                        "emulated": p.emulated,
                        "status": "solved" if p.sky is not None else ((plate_status.get(p.key) if p.key else None) or "unsolved")}
                       for p in plates],
            "model": None if model is None else model.json(),
            "motors": axes is not None,
            "emulated": emulated,  # what in this view an emulator said
            "time_from": time_from,  # whose clock set the time (None: this computer's)
        }
