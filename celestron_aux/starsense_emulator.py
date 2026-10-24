"""Emulates a StarSense AutoAlign V2 camera (bus address 0xbd).

It answers CPWI the way a real camera does: same commands, same reply lengths
and the same sequence (a solve is "in progress" for a few seconds, then solved).
Every reply was modelled on captures of a real camera.

When the telescope emulator runs too, a plate solve reports where that emulated
telescope points when the solve starts, converted to J2000 with the location and
time CPWI sent the camera, plus a solve error of a few arcseconds. CPWI's
alignment then agrees with the mount, as with a real camera on a real mount.

Otherwise plate solves return four alignment points in turn, the four points of
a real alignment session. Each point is kept as an hour angle and a Dec, so it stays
in the same place relative to the meridian whatever the time; its RA is worked
out from the location and time CPWI sends. Without a location the recorded RA
is used as is.

The positions are only consistent with the emulator, not with the real sky:
after an emulated alignment, GoTos will not land on real stars. It is meant for
testing CPWI's StarSense workflow without a camera.
"""

from __future__ import annotations

import logging
import math
import random
import struct
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, List, Optional, Tuple

from .bus import BusEvent
from .catalog import Addr
from .protocol import AuxPacket
from .sky import altaz_to_j2000, altaz_to_radec, gmst_degrees, parallactic_angle

log = logging.getLogger(__name__)

ADDRESS = Addr.STARSENSE_CAMERA_V2
FIRMWARE_VERSION = bytes.fromhex("230c0005")  # 35.12.5, as reported by a real camera

START_PLATE_SOLVE, GET_PLATE_STATUS = 0x01, 0x02
START_CALIBRATE, GET_CALIBRATE_STATUS = 0x03, 0x04
IS_CALIBRATED, GET_CALIBRATION_DATA = 0x07, 0x08
SET_MOUNT_INFO, SET_GEOLOCATION, SET_TIME_NOW = 0x30, 0x31, 0x32
GET_IMAGE_ROTATION, GET_TELESCOPE_POSITION, GET_VERSION = 0x45, 0x4B, 0xFE
# All-Star Polar Alignment (ASPA), as CPWI uses it (worked out from captures of a real camera):
#   0x13  start (01) or stop (00) an ASPA, or go on to the adjustment step (04, sent by CPWI
#         once the completion reaches 100 %); the camera answers 00.
#   0x15  the estimated polar axis: 01 + RA/Dec (J2000, radians), or 04 + zeros without one.
#         Here: where the emulated wedge's or equatorial mount's polar axis really points.
#   0x17  progress: a status (01), the completion (one byte, 0-100), the uncertainty in degrees
#         (float32), then three bytes not understood (a real camera sent 00 00 68).
# CPWI turns the RA axis about 5 degrees between solves; the completion grows with that turn,
# 100 % at ASPA_SPAN degrees: after 4 turns, so at the 5th solve (0, 25, 50, 75, 100 %).
# The uncertainty shrinks with it.
ASPA_SPAN = 20.0
ASPA_UNCERTAINTY = (5.0, 0.05)  # degrees, at 0 % and at 100 %
ASPA_TAIL = bytes.fromhex("000068")
ASPA_IDLE = bytes((0, 0)) + struct.pack(">f", 0.0) + ASPA_TAIL  # before an ASPA has a solve, as a real camera
NO_ESTIMATE = bytes.fromhex("040000000000000000")
SET_ASPA, GET_ASPA_POLE, GET_ASPA_PROGRESS = 0x13, 0x15, 0x17
#   0x16  the polar alignment error, asked in the adjustment step: status 01, then the altitude
#         error and the azimuth error in degrees (float32), worked out with CPWI's adjustment
#         screen; 00 + zeros outside a measurement, as a real camera.
GET_ASPA_ERROR = 0x16
ASPA_ERROR_NONE = bytes(9)

IN_PROGRESS, SOLVED = 0, 1
EPOCH_2000 = datetime(2000, 1, 1, tzinfo=timezone.utc)

# The camera-to-telescope offset seen on a real camera: 1.1178 deg, at 117.6 deg
# from the image rotation; and the calibration result it reported (pixels).
MIN_SOLVE_ALTITUDE = 5.0  # degrees: lower than this a real camera sees ground or haze, and fails
FAILED = 4                # the status a real camera reports for a failed solve
SOLVE_ERROR = 12.0  # arcsec: the scatter of an emulated solve on each axis (never exactly zero)
OFFSET_LENGTH = 1.1178
OFFSET_ANGLE = 117.6
CALIBRATION_XY = (230, 122)


@dataclass(frozen=True)
class SkyPoint:
    hour_angle: float  # hours, west positive
    dec: float         # degrees
    ra: float          # degrees, as recorded; used when no location is known
    rotation: float    # degrees, image rotation reported for this point


# The four points of a real StarSense alignment (CPWI listed them as RA 18h30m,
# 20h03m, 0h13m and 22h54m).
ALIGNMENT_POINTS: Tuple[SkyPoint, ...] = (
    SkyPoint(hour_angle=+2.7413, dec=-3.6622, ra=277.6558, rotation=47.86),
    SkyPoint(hour_angle=+1.2012, dec=+19.1698, ra=300.8853, rotation=48.20),
    SkyPoint(hour_angle=-2.9554, dec=-1.3381, ra=3.4718, rotation=50.31),
    SkyPoint(hour_angle=-1.6253, dec=+0.7317, ra=343.6209, rotation=49.45),
)


def offset_position(ra: float, dec: float, length: float, angle: float) -> Tuple[float, float]:
    """The point ``length`` degrees from (ra, dec) at position angle ``angle`` (north through east)."""
    ra1, dec1, dist, pa = map(math.radians, (ra, dec, length, angle))
    sine = math.sin(dec1) * math.cos(dist) + math.cos(dec1) * math.sin(dist) * math.cos(pa)
    dec2 = math.asin(max(-1.0, min(1.0, sine)))  # rounding may leave it a hair beyond 1 near a pole
    ra2 = ra1 + math.atan2(math.sin(pa) * math.sin(dist) * math.cos(dec1),
                           math.cos(dist) - math.sin(dec1) * math.sin(dec2))
    return math.degrees(ra2) % 360, math.degrees(dec2)


def _unit(ra: float, dec: float) -> Tuple[float, float, float]:
    r, d = math.radians(ra), math.radians(dec)
    return (math.cos(d) * math.cos(r), math.cos(d) * math.sin(r), math.sin(d))


def _turn_around(pole: Optional[Tuple[float, float]], a: Tuple[float, float], b: Tuple[float, float]) -> float:
    """Degrees between two sky positions, seen around the polar axis (the angle the RA axis
    turned), or simply the angle between them when there is no polar axis."""
    u: Tuple[float, ...] = _unit(*a)
    v: Tuple[float, ...] = _unit(*b)
    if pole is not None:
        p = _unit(*pole)
        u = tuple(x - sum(i * j for i, j in zip(u, p)) * y for x, y in zip(u, p))
        v = tuple(x - sum(i * j for i, j in zip(v, p)) * y for x, y in zip(v, p))
    norm = math.sqrt(sum(x * x for x in u) * sum(x * x for x in v))
    if norm < 1e-12:
        return 0.0  # a solve at the pole itself: no turn can be seen
    cosine = max(-1.0, min(1.0, sum(x * y for x, y in zip(u, v)) / norm))
    return math.degrees(math.acos(cosine))


def _radec(ra: float, dec: float) -> bytes:
    return struct.pack(">ff", math.radians(ra), math.radians(dec))


class StarSenseEmulator:
    """Bus listener that answers requests sent to 0xbd while enabled."""

    def __init__(self, send: Callable[[AuxPacket], object], *, points: Tuple[SkyPoint, ...] = ALIGNMENT_POINTS,
                 solve_time: float = 8.0, calibrate_time: float = 7.0,
                 clock: Callable[[], float] = time.monotonic,
                 now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 rng: Optional[random.Random] = None) -> None:
        self._send = send
        self._points = points
        self.solve_time = solve_time
        self.calibrate_time = calibrate_time
        self._clock = clock
        self._now = now
        self._lock = threading.Lock()
        self._enabled = False
        self._location: Optional[Tuple[float, float]] = None  # lat, lon from CPWI
        self._time_offset = 0.0  # CPWI's clock minus ours, seconds
        self._solves = 0
        self._solve_started: Optional[float] = None
        self._solution: Optional[Tuple[float, float, float]] = None  # ra, dec, rotation
        self._calibration_started: Optional[float] = None
        self._calibration_star: Optional[Tuple[float, float]] = None
        self._calibration: Optional[List[float]] = None  # the six 0x08 slots
        self.versions = {ADDRESS: FIRMWARE_VERSION}  # can be changed while running
        self._aspa_active = False
        self._aspa_first: Optional[Tuple[float, float]] = None  # the first solve of the ASPA (RA, Dec)
        self._aspa_turn = 0.0  # degrees turned around the polar axis since then
        self._aspa_adjusting = False  # CPWI's adjustment step (0x13 04)
        self._rng = rng or random.Random()
        # Where an emulated telescope points: (altitude, azimuth, is alt-az), or None.
        self.pointing_source: Optional[Callable[[], Optional[Tuple[float, float, bool]]]] = None
        # Where an emulated mount's polar axis points: (altitude, azimuth), or None.
        self.polar_axis_source: Optional[Callable[[], Optional[Tuple[float, float]]]] = None

    # --- control ------------------------------------------------------------------

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

    def snapshot(self) -> dict:
        with self._lock:
            return {"enabled": self._enabled, "solves": self._solves, "calibrated": self._calibration is not None,
                    "location_known": self._location is not None}

    # --- bus listener -----------------------------------------------------------------

    def __call__(self, event: BusEvent) -> None:
        packet = event.frame
        if not isinstance(packet, AuxPacket) or packet.destination != ADDRESS or packet.source == ADDRESS:
            return
        with self._lock:
            if not self._enabled:
                return
            reply = self._answer(packet.command, packet.data)
        if reply is not None:
            self._send(AuxPacket(ADDRESS, packet.source, packet.command, reply))

    # --- the camera's behaviour (called with the lock held) -------------------------------

    def _answer(self, command: int, data: bytes) -> Optional[bytes]:
        if command == GET_VERSION:
            return self.versions[ADDRESS]
        if command in (SET_MOUNT_INFO, SET_GEOLOCATION, SET_TIME_NOW):
            self._store_setting(command, data)
            return b""
        if command == IS_CALIBRATED:
            return b"\x01" if self._calibration else b"\x00"
        if command == GET_CALIBRATION_DATA:
            slots = self._calibration or [0.0] * 6
            return struct.pack(">f", slots[data[0]] if data and data[0] < len(slots) else 0.0)
        if command == START_PLATE_SOLVE:
            self._solve_started = self._clock()
            pointing = self.pointing_source() if self.pointing_source is not None else None
            if pointing is not None and pointing[0] < MIN_SOLVE_ALTITUDE:
                self._solution = None  # looking at the ground: this solve will fail
            else:
                self._solution = self._next_point()
            return b""
        if command == GET_PLATE_STATUS:
            if not self._solve_done():
                return bytes(9)
            if self._solution is None:
                return bytes((FAILED,)) + bytes(8)
            ra, dec, _ = self._solution
            return bytes((SOLVED,)) + _radec(ra, dec)
        if command == GET_TELESCOPE_POSITION:
            if not self._solve_done():
                return bytes(9)  # like the plate status: nothing to report yet
            if self._solution is None:
                return bytes((FAILED,)) + bytes(8)
            ra, dec, rotation = self._solution
            return bytes((SOLVED,)) + _radec(*offset_position(ra, dec, OFFSET_LENGTH, rotation - OFFSET_ANGLE))
        if command == GET_IMAGE_ROTATION:
            rotation = self._solution[2] if self._solution else self._points[0].rotation
            return struct.pack(">f", rotation)
        if command == START_CALIBRATE and len(data) >= 8:
            ra, dec = (math.degrees(v) for v in struct.unpack(">ff", data[:8]))
            self._calibration_star = (ra % 360, dec)
            self._calibration_started = self._clock()
            return b""
        if command == GET_CALIBRATE_STATUS:
            return self._calibration_status()
        if command == SET_ASPA:
            mode = data[0] if data else 0
            if mode == 1:    # start: a new measurement
                self._aspa_active, self._aspa_first, self._aspa_turn = True, None, 0.0
                self._aspa_adjusting = False
            elif mode == 0:  # stop
                self._aspa_active, self._aspa_first, self._aspa_turn = False, None, 0.0
                self._aspa_adjusting = False
            else:            # 04: the adjustment step, keeping the finished measurement
                self._aspa_adjusting = True
            return b"\x00"
        if command == GET_ASPA_POLE:
            return self._aspa_pole()
        if command == GET_ASPA_PROGRESS:
            return self._aspa_progress()
        if command == GET_ASPA_ERROR:
            return self._aspa_error()
        return None  # unknown command: a real camera may answer, but we do not guess

    def _aspa_error(self) -> bytes:
        """How far the emulated polar axis is from the celestial pole: altitude error, then azimuth
        error as an angle on the sky (so CPWI's total error is the real distance), in degrees."""
        axis = self.polar_axis_source() if self.polar_axis_source is not None else None
        measured = self._aspa_adjusting or (self._aspa_active and self._aspa_turn >= ASPA_SPAN)
        if axis is None or self._location is None or not measured:
            return ASPA_ERROR_NONE
        latitude = self._location[0]
        pole_alt, pole_az = abs(latitude), (180.0 if latitude < 0 else 0.0)
        alt_error = axis[0] - pole_alt
        azimuth_error = (((axis[1] - pole_az + 180) % 360) - 180) * math.cos(math.radians(pole_alt))
        return bytes((SOLVED,)) + struct.pack(">ff", alt_error, azimuth_error)

    def _pole_j2000(self) -> Optional[Tuple[float, float]]:
        """Where the emulated mount's polar axis points (J2000 RA/Dec), if there is one."""
        axis = self.polar_axis_source() if self.polar_axis_source is not None else None
        if axis is None or self._location is None:
            return None
        when = self._now() + timedelta(seconds=self._time_offset)
        return altaz_to_j2000(axis[0], axis[1], self._location[0], self._location[1], when)

    def _aspa_pole(self) -> bytes:
        pole = self._pole_j2000()
        return NO_ESTIMATE if pole is None else bytes((SOLVED,)) + _radec(*pole)

    def _aspa_progress(self) -> bytes:
        if not self._aspa_active or self._aspa_first is None:
            return ASPA_IDLE
        completion = min(100, int(round(self._aspa_turn / ASPA_SPAN * 100)))
        start, final = ASPA_UNCERTAINTY
        uncertainty = start - (start - final) * completion / 100
        return bytes((SOLVED, completion)) + struct.pack(">f", uncertainty) + ASPA_TAIL

    def _aspa_note_solve(self, ra: float, dec: float) -> None:
        """Follow how far the solves have turned around the polar axis since the ASPA started."""
        if not self._aspa_active:
            return
        if self._aspa_first is None:
            self._aspa_first = (ra, dec)
            return
        self._aspa_turn = max(self._aspa_turn, _turn_around(self._pole_j2000(), self._aspa_first, (ra, dec)))

    def _store_setting(self, command: int, data: bytes) -> None:
        if command == SET_GEOLOCATION and len(data) >= 8:
            self._location = struct.unpack(">ff", data[:8])
        elif command == SET_TIME_NOW and len(data) >= 4:
            sent = EPOCH_2000 + timedelta(seconds=int.from_bytes(data[:4], "big"))
            self._time_offset = (sent - self._now()).total_seconds()

    def _next_point(self) -> Tuple[float, float, float]:
        solved = self._solve_emulated_telescope()
        if solved is not None:
            self._solves += 1
            self._aspa_note_solve(solved[0], solved[1])
            return solved
        point = self._points[self._solves % len(self._points)]
        self._solves += 1
        if self._location is None:
            ra = point.ra
        else:
            when = self._now() + timedelta(seconds=self._time_offset)
            ra = (gmst_degrees(when) + self._location[1] - point.hour_angle * 15) % 360
        return ra, point.dec, point.rotation

    def _solve_emulated_telescope(self) -> Optional[Tuple[float, float, float]]:
        """RA/Dec (J2000) of where the emulated telescope points now, with a small solve error."""
        pointing = self.pointing_source() if self.pointing_source is not None else None
        if pointing is None or self._location is None:
            return None
        alt, az, alt_az = pointing
        latitude, longitude = self._location
        when = self._now() + timedelta(seconds=self._time_offset)
        ra, dec = altaz_to_j2000(alt, az, latitude, longitude, when)
        dec += self._rng.gauss(0, SOLVE_ERROR) / 3600
        ra += self._rng.gauss(0, SOLVE_ERROR) / 3600 / max(0.01, math.cos(math.radians(dec)))
        rotation = self._points[0].rotation
        if alt_az:  # the camera's view turns with the field on an alt-az mount
            rotation += parallactic_angle(*altaz_to_radec(alt, az, latitude, longitude, when), latitude, longitude, when)
        return ra % 360, max(-90.0, min(90.0, dec)), rotation % 360

    def _solve_done(self) -> bool:
        return self._solve_started is not None and self._clock() - self._solve_started >= self.solve_time

    def _calibration_status(self) -> bytes:
        if self._calibration_started is None or self._calibration_star is None:
            return bytes(5)
        if self._clock() - self._calibration_started < self.calibrate_time:
            return bytes(5)
        if self._calibration is None:
            ra, dec = self._calibration_star
            rotation = self._solution[2] if self._solution else self._points[0].rotation
            # Slots 0-1 sit where a real camera put them: the star shifted like 0x4b.
            offset_ra, offset_dec = offset_position(ra, dec, OFFSET_LENGTH, rotation - OFFSET_ANGLE)
            # A real camera stored the time one day ahead; copied as observed.
            stamp = self._now().timestamp() + self._time_offset + 86400
            self._calibration = [offset_ra, offset_dec, ra, dec, rotation, stamp]
        x, y = CALIBRATION_XY
        return bytes((SOLVED,)) + x.to_bytes(2, "big") + y.to_bytes(2, "big")
