"""An emulated StarSense AutoAlign camera (the original, at 0xb4) that photographs a real sky.

It answers captures as a real camera does (measured on firmware 1.02.13343):

* 0x90 sets the exposure (``HH LL 00``) and is answered with no data after
  about 0.8 + 2 s per 1000 units (2.8 s for 1000); the status is then ``03 00``.
* 0x94 takes the picture and is answered after about 10 s with ``09 NN`` (NN
  points found) or ``01 00`` (none).
* 0x92 is answered ``09 NN`` and the star list follows as a 3C frame; asked
  again, it repeats the same list, answered after about 5 s.
* 0x91 gives the status (``01 00`` after power-on); while 0x90 or 0x94 runs
  the camera answers nothing at all.
* 0x3F gives the alignment centre, (640, 480) until 0x3E sets another.
* 0x9F, which CPWI asks every 8 s after the star list until it is answered,
  gets the status, like 0x94 (``09 NN``). This is a guess: no real camera's
  answer to 0x9F has been recorded yet.

The stars are the Hipparcos catalogue as seen from where the emulated mount
points (the location comes from the GPS emulator or from CPWI), else the sky of
the recorded frame. Longer exposures reach fainter stars: magnitude 9 at 1000.
As in real frames, about 0.7 extra points per catalogue star come from fainter
stars the catalogue does not hold; they are placed at random and listed last.
A full list holds 99 points and one all-zero record, like the recorded frame.
"""

from __future__ import annotations

import math
import random
import struct
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from . import star_catalog
from ._sample_data import SAMPLE_STAR_LIST
from .catalog import Addr
from .bus import BusEvent
from .device_emulators import GET_VERSION, DeviceEmulator
from .protocol import AuxPacket, StarSenseFrame
from .starsense import STAR_RECORD, parse_star_list

ADDRESS = Addr.STARSENSE_CAMERA
SET_CENTRE, GET_CENTRE = 0x3E, 0x3F
SET_EXPOSURE, GET_STATUS, GET_LIST, CAPTURE = 0x90, 0x91, 0x92, 0x94
GET_IMAGE3 = 0x9F  # answered with the status: a guess, see above
IDLE, READY, FOUND = 0x01, 0x03, 0x09

RECORDED_SKY = (159.229, 38.641, 92.6)  # RA, Dec (J2000) and rotation of tests/fixtures/starsense_frame.hex
DEFAULT_EXPOSURE = 1000
MAX_POINTS = 99
EXTRA_POINTS_PER_STAR = 0.7  # the recorded frame: 41 points without a catalogue star for 58 with one
CENTROID_NOISE = 0.3         # pixels; the recorded frame matches the catalogue to 0.6 px rms

KEPT_CAPTURES = 4  # the last captures kept for the page (an alignment takes about four)

SkySource = Callable[[], Optional[Tuple[float, float, float]]]  # RA, Dec, rotation; None: below the horizon


def limiting_magnitude(exposure: int) -> float:
    """The faintest stars an exposure records: magnitude 9 at 1000, a magnitude per 2.5x."""
    return star_catalog.MAG_LIMIT + 2.5 * math.log10(max(exposure, 1) / DEFAULT_EXPOSURE)


class StarSenseV1Emulator(DeviceEmulator):
    """The original StarSense AutoAlign camera at 0xb4."""

    addresses = (ADDRESS,)
    versions = {ADDRESS: bytes.fromhex("0102341f")}  # 1.2.13343, as a real camera reported

    def __init__(self, send: Callable[[AuxPacket], object],
                 send_frame: Optional[Callable[[StarSenseFrame, AuxPacket], object]] = None,
                 clock: Callable[[], float] = time.monotonic, rng: Optional[random.Random] = None,
                 time_scale: float = 1.0) -> None:
        super().__init__(send, clock)
        self._send_frame = send_frame
        self._rng = rng or random.Random()
        self.time_scale = time_scale  # tests make the camera faster
        #: Where the camera looks: (RA, Dec, rotation), None when it sees no sky. Unset: the recorded sky.
        self.sky_source: Optional[SkySource] = None
        #: Answer every capture with the star list recorded from a real camera, unchanged
        #: (to tell whether a client rejects the generated lists or something else).
        self.recorded_list = False
        self._last_sky: Optional[Tuple[float, float, float]] = None
        self._kept: Deque[Dict[str, Any]] = deque(maxlen=KEPT_CAPTURES)
        #: Set by ``sky_source``: where its sky came from (the telescope, with the pointing,
        #: place and time used, or the recorded sky and why), kept with each capture.
        self.sky_details: Dict[str, Any] = {}
        self._status = bytes((IDLE, 0))
        self._busy = False
        self._exposure = DEFAULT_EXPOSURE
        self._centre = (640, 480)
        self._list: Optional[bytes] = None
        self._list_sent = False
        self._timers: List[threading.Timer] = []
        self._captures = 0

    # --- control -------------------------------------------------------------------

    def disable(self) -> None:
        super().disable()
        with self._lock:
            timers, self._timers = self._timers, []
            self._busy = False
            self._status, self._list, self._list_sent = bytes((IDLE, 0)), None, False
        for timer in timers:
            timer.cancel()

    def snapshot(self) -> dict:
        with self._lock:
            sky = self._last_sky
            return {"enabled": self._enabled, "captures": self._captures, "exposure": self._exposure,
                    "busy": self._busy, "points": self._status[1] if self._status[0] == FOUND else 0,
                    "recorded_list": self.recorded_list,
                    "sky": None if sky is None else {"ra": round(sky[0], 3), "dec": round(sky[1], 3),
                                                     "rotation": round(sky[2], 1)}}

    # --- what the camera sees ----------------------------------------------------------

    def star_list(self, exposure: int = DEFAULT_EXPOSURE) -> bytes:
        """A star list payload for one exposure of the sky the camera looks at now."""
        return self._expose(exposure)[0]

    def clear_captures(self) -> None:
        """Forget the kept captures (the page's charts and the spheres' plates)."""
        with self._lock:
            self._kept.clear()

    def captures(self) -> List[Dict[str, Any]]:
        """The last captures, newest first: when, where, the exposure and every point of the
        list (x, y, and the Hipparcos star's magnitude and number, or none for a fainter star)."""
        with self._lock:
            return [dict(capture) for capture in reversed(self._kept)]

    def _expose(self, exposure: int) -> Tuple[bytes, List[Tuple[float, float, Optional[float], int]]]:
        """The star list payload, and its points with the catalogue star behind each (mag, HIP; None, 0 if none)."""
        if self.recorded_list:
            self._last_sky = None
            self.sky_details = {"source": "recorded list"}
            return SAMPLE_STAR_LIST, [(star.x, star.y, None, 0) for star in parse_star_list(SAMPLE_STAR_LIST)]
        self.sky_details = {"source": "recorded", "reason": "no sky source"}
        sky = RECORDED_SKY if self.sky_source is None else self.sky_source()
        self._last_sky = sky
        if sky is None:
            return b"", []
        limit = limiting_magnitude(exposure)
        stars = [star for star in star_catalog.in_view(*sky) if star.mag <= limit]
        points: List[Tuple[float, float, Optional[float], int]] = [
            (star.x + self._rng.gauss(0, CENTROID_NOISE), star.y + self._rng.gauss(0, CENTROID_NOISE), star.mag, star.hip)
            for star in stars]
        width, height = star_catalog.SENSOR_SIZE
        # Fainter stars than the catalogue holds: none for short exposures, about three times
        # more per magnitude beyond 9.
        extra = 0
        if limit >= star_catalog.MAG_LIMIT:
            extra = round(len(stars) * EXTRA_POINTS_PER_STAR * 3 ** (limit - star_catalog.MAG_LIMIT))
        points += [(self._rng.uniform(0, width), self._rng.uniform(0, height), None, 0) for _ in range(extra)]
        points = points[:MAX_POINTS]
        records = [STAR_RECORD.pack(x, y) for x, y, _, _ in points]
        if len(points) >= MAX_POINTS:
            records.append(bytes(STAR_RECORD.size))  # a full list ends with an all-zero record, counted
        return b"".join(records), points

    # --- requests --------------------------------------------------------------------

    def __call__(self, event: BusEvent) -> None:
        packet = event.frame
        if not isinstance(packet, AuxPacket) or packet.destination != ADDRESS or packet.source == ADDRESS:
            return
        with self._lock:
            if not self._enabled or self._busy:
                return  # a real camera answers nothing while it sets the exposure or captures
            reply = self._answer_now(packet)
        if reply is not None:
            self._send(AuxPacket(ADDRESS, packet.source, packet.command, reply))

    def answer(self, address: int, command: int, data: bytes) -> Optional[bytes]:
        raise NotImplementedError("requests are handled in __call__")

    def _answer_now(self, packet: AuxPacket) -> Optional[bytes]:
        """The immediate reply, or None (none, or one sent later). Called with the lock held."""
        command, data = packet.command, packet.data
        if command == GET_VERSION:
            return self.versions[ADDRESS]
        if command in (GET_STATUS, GET_IMAGE3):
            return self._status
        if command == GET_CENTRE:
            return struct.pack("<II", *self._centre)
        if command == SET_CENTRE and len(data) >= 8:
            self._centre = struct.unpack("<II", data[:8])  # assumed: the layout 0x3F answers with
            return b""
        if command == SET_EXPOSURE and len(data) >= 2:
            exposure = struct.unpack(">H", data[:2])[0]
            self._start(packet, 0.8 + 2.0 * exposure / 1000, lambda: self._exposure_set(exposure))
        elif command == CAPTURE:
            self._start(packet, self._rng.uniform(9.8, 11.9), self._captured)
        elif command == GET_LIST:
            self._list_requested(packet)
        return None

    def _start(self, request: AuxPacket, delay: float, finish: Callable[[], bytes]) -> None:
        """Answer ``request`` after ``delay`` seconds, busy until then. Called with the lock held."""
        self._busy = True
        source, command = request.source, request.command

        def done() -> None:
            with self._lock:
                if not self._enabled:
                    return
                self._busy = False
                reply = finish()
            self._send(AuxPacket(ADDRESS, source, command, reply))

        self._later(delay, done)

    def _later(self, delay: float, action: Callable[[], object]) -> None:
        timer = threading.Timer(delay * self.time_scale, action)
        timer.daemon = True
        self._timers = [t for t in self._timers if t.is_alive()] + [timer]
        timer.start()

    def _exposure_set(self, exposure: int) -> bytes:
        self._exposure = exposure
        self._status = bytes((READY, 0))
        return b""

    def _captured(self) -> bytes:
        self._captures += 1
        self._list, points = self._expose(self._exposure)
        sky = self._last_sky
        self._kept.append({
            "number": self._captures, "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "exposure": self._exposure, "recorded_list": self.recorded_list, "details": dict(self.sky_details),
            "sky": None if sky is None else {"ra": round(sky[0], 4), "dec": round(sky[1], 4),
                                             "rotation": round(sky[2], 2)},
            "points": [[round(x, 1), round(y, 1), None if mag is None else round(mag, 2), hip]
                       for x, y, mag, hip in points],
        })
        self._list_sent = False
        count = min(len(self._list) // STAR_RECORD.size, 255)
        self._status = bytes((FOUND, count)) if count else bytes((IDLE, 0))
        return self._status

    def _list_requested(self, request: AuxPacket) -> None:
        star_list = self._list
        reply = AuxPacket(ADDRESS, request.source, request.command, self._status)
        if not star_list:
            self._later(0.1, lambda: self._send(reply))
            return
        delay = 5.0 if self._list_sent else 0.1  # a repeated request takes about 5 s
        self._list_sent = True

        def send_list() -> None:
            if self.enabled and self._send_frame is not None:
                self._send_frame(StarSenseFrame(star_list), reply)

        def answer() -> None:
            if self.enabled:
                self._send(reply)
                self._later(0.4, send_list)

        self._later(delay, answer)
