"""StarSense camera support: star-list decoding, images and capture control.

A StarSense bulk frame carries 8-byte star records: two little-endian float32
values, the star's x and y pixel position on the sensor. An all-zero record
marks the end of the list.
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Callable, List, Tuple

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


def run_capture_sequence(
    send: Callable[[AuxPacket], object],
    pause: float = 4.0,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Ask the StarSense camera to take an exposure and return its star list."""
    camera = Addr.STARSENSE_CAMERA
    send(AuxPacket(Addr.SCANNER, camera, Cmd.SS_RESET_CAPTURE, bytes.fromhex("03e800")))
    sleep(pause)
    send(AuxPacket(Addr.SCANNER, camera, Cmd.SS_START_CAPTURE, b"\x00"))
    sleep(pause)
    send(AuxPacket(Addr.SCANNER, camera, Cmd.SS_GET_CAPTURE_DATA))
