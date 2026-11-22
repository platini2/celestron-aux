"""StarSense camera support: star-list decoding, images and capture control.

A StarSense bulk frame carries 8-byte star records: two little-endian float32
values, the star's x and y pixel position on the sensor, brightest star first.
A list can hold an all-zero record anywhere, which the camera's star count
includes; it is not a star and is skipped.

A capture is three requests, each answered only when the camera has finished:
0x90 sets the exposure (about 1 to 3 s, once 7.4 s), 0x94 takes the picture
and finds the stars (about 10 s, answered ``01 00`` when it found none or
``09 NN`` with the count), and 0x92 asks for the list (answered ``09 NN``, then
the 3C frame). While 0x90 or 0x94 runs the camera answers nothing else.
"""

from __future__ import annotations

import logging
import struct
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple

from .bus import BusEvent
from .catalog import Addr, Cmd
from .protocol import AuxPacket, StarSenseFrame

if TYPE_CHECKING:
    from PIL import Image

log = logging.getLogger(__name__)

STAR_RECORD = struct.Struct("<ff")
SENSOR_RESOLUTION: Tuple[int, int] = (1280, 960)
_EMPTY_RECORD = bytes(STAR_RECORD.size)


@dataclass(frozen=True)
class Star:
    x: float
    y: float


def parse_star_list(payload: bytes) -> List[Star]:
    stars = []
    for offset in range(0, len(payload) - STAR_RECORD.size + 1, STAR_RECORD.size):
        record = payload[offset:offset + STAR_RECORD.size]
        if record != _EMPTY_RECORD:
            stars.append(Star(*STAR_RECORD.unpack(record)))
    return stars


def render_star_image(
    stars: List[Star], size: Tuple[int, int] = SENSOR_RESOLUTION, radius: int = 2
) -> "Image.Image":
    """Draw stars as white dots on black, in sensor pixel coordinates."""
    from PIL import Image, ImageDraw  # optional dependency, only needed here

    image = Image.new("L", size)
    draw = ImageDraw.Draw(image)
    for star in stars:
        draw.ellipse(
            (star.x - radius, star.y - radius, star.x + radius, star.y + radius), fill=255
        )
    return image


class StarImageSaver:
    """Bus listener that saves every received star list as a PNG."""

    def __init__(self, directory: Path, enabled: bool = True,
                 size: Tuple[int, int] = SENSOR_RESOLUTION) -> None:
        self.directory = directory
        self.enabled = enabled
        self.size = size
        self._lock = threading.Lock()
        self._counter = 0

    def __call__(self, event: BusEvent) -> None:
        if not self.enabled or not isinstance(event.frame, StarSenseFrame):
            return
        image = render_star_image(parse_star_list(event.frame.payload), self.size)
        path = self._next_path()
        self.directory.mkdir(parents=True, exist_ok=True)
        image.save(path)
        log.info("saved StarSense image %s", path)

    def _next_path(self) -> Path:
        with self._lock:
            self._counter += 1
            counter = self._counter
        return self.directory / f"starsense-{datetime.now():%Y%m%d-%H%M%S}-{counter:03d}.png"


CAPTURE_EXPOSURE = 1000  # the value CPWI and the hand controller send; it behaves like an exposure setting
STATUS_NONE_FOUND = 0x01  # also what the camera reports after power-on, before any capture
STATUS_FOUND = 0x09
# Generous limits: real cameras took up to 7.4 s to set the exposure and 11.9 s to capture.
_TIMEOUTS = {Cmd.SS_RESET_CAPTURE: 15.0, Cmd.SS_START_CAPTURE: 30.0, Cmd.SS_GET_CAPTURE_DATA: 10.0}


class StarSenseCapture:
    """Bus listener that runs a capture, sending each request when the previous one is answered."""

    def __init__(self, send: Callable[[AuxPacket], object],
                 timeouts: Optional[Dict[int, float]] = None) -> None:
        self._send = send
        self._timeouts = dict(_TIMEOUTS if timeouts is None else timeouts)
        self._condition = threading.Condition()
        self._waiting_for: Optional[int] = None
        self._reply: Optional[bytes] = None
        self._running = threading.Lock()

    def __call__(self, event: BusEvent) -> None:
        packet = event.frame
        if (not isinstance(packet, AuxPacket) or packet.source != Addr.STARSENSE_CAMERA
                or packet.destination != Addr.SCANNER):
            return
        with self._condition:
            if packet.command == self._waiting_for and self._reply is None:
                self._reply = packet.data
                self._condition.notify_all()

    def run(self, exposure: int = CAPTURE_EXPOSURE) -> Optional[int]:
        """Capture once. Returns the camera's star count, or None if it found none or stopped answering.

        The star list itself arrives as a StarSense frame, which StarImageSaver and the page pick up.
        """
        if not self._running.acquire(blocking=False):
            log.info("a StarSense capture is already running")
            return None
        try:
            if self._ask(Cmd.SS_RESET_CAPTURE, struct.pack(">HB", exposure, 0)) is None:
                return None
            status = self._ask(Cmd.SS_START_CAPTURE, b"\x00")
            if status is None:
                return None
            if len(status) < 2 or status[0] != STATUS_FOUND or status[1] == 0:
                log.warning("StarSense camera found no stars (status %s)", status.hex())
                return None
            listed = self._ask(Cmd.SS_GET_CAPTURE_DATA, b"")
            return None if listed is None else status[1]
        finally:
            self._running.release()

    def _ask(self, command: int, data: bytes) -> Optional[bytes]:
        with self._condition:
            self._waiting_for, self._reply = command, None
        try:
            if not self._send(AuxPacket(Addr.SCANNER, Addr.STARSENSE_CAMERA, command, data)):
                log.warning("StarSense capture stopped: could not send 0x%02x", command)
                return None
            with self._condition:
                if not self._condition.wait_for(lambda: self._reply is not None, self._timeouts[command]):
                    log.warning("StarSense capture stopped: no answer to 0x%02x", command)
                    return None
                return self._reply
        finally:
            with self._condition:
                self._waiting_for = None
