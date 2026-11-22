"""The stars a StarSense camera sees: a Hipparcos cut and the camera's geometry.

``data/hipparcos.bin.gz`` holds the Hipparcos main catalogue (ESA 1997) to V
magnitude 9, at J2000 (built by ``scripts/build_star_catalog.py``). The camera
geometry was measured on a real StarSense AutoAlign frame
(``tests/fixtures/starsense_frame.hex``): 1280 x 960 pixels of 19.41 arcseconds
(a 6.90 x 5.17 degree field), centred on pixel (640, 480), not mirrored. A sky
position angle ``rotation`` turns the view: at rotation 0 east is toward
smaller x and north toward smaller y. That frame matched with rotation 92.6.
"""

from __future__ import annotations

import bisect
import gzip
import math
import struct
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

CATALOG_PATH = Path(__file__).resolve().parent / "data" / "hipparcos.bin.gz"
_RECORD = struct.Struct("<ffhI")

SENSOR_SIZE: Tuple[int, int] = (1280, 960)
SENSOR_CENTRE: Tuple[float, float] = (640.0, 480.0)
PIXEL_SCALE = 19.41  # arcseconds per pixel
MAG_LIMIT = 9.0      # the catalogue's faintest stars


@dataclass(frozen=True)
class CatalogStar:
    ra: float    # degrees, J2000
    dec: float   # degrees, J2000
    mag: float   # V magnitude
    hip: int     # Hipparcos number


@dataclass(frozen=True)
class SeenStar:
    x: float
    y: float
    mag: float
    hip: int     # 0 for a point with no catalogue star behind it


_lock = threading.Lock()
_stars: Optional[List[CatalogStar]] = None
_decs: List[float] = []


def stars() -> List[CatalogStar]:
    """All catalogue stars, sorted by declination. Loaded once, on first use."""
    global _stars, _decs
    with _lock:
        if _stars is None:
            raw = gzip.decompress(CATALOG_PATH.read_bytes())
            _stars = [CatalogStar(ra, dec, mag / 100, hip) for ra, dec, mag, hip in _RECORD.iter_unpack(raw)]
            _decs = [star.dec for star in _stars]
        return _stars


def near(ra: float, dec: float, radius: float) -> List[CatalogStar]:
    """Catalogue stars within ``radius`` degrees of RA/Dec (J2000, degrees)."""
    catalog = stars()
    lo = bisect.bisect_left(_decs, dec - radius)
    hi = bisect.bisect_right(_decs, dec + radius)
    cos_radius = math.cos(math.radians(radius))
    sin_d0, cos_d0 = math.sin(math.radians(dec)), math.cos(math.radians(dec))
    found = []
    for star in catalog[lo:hi]:
        d = math.radians(star.dec)
        if sin_d0 * math.sin(d) + cos_d0 * math.cos(d) * math.cos(math.radians(star.ra - ra)) >= cos_radius:
            found.append(star)
    return found


def project(ra: float, dec: float, rotation: float, candidates: Sequence[CatalogStar],
            pixel_scale: float = PIXEL_SCALE) -> List[SeenStar]:
    """Where ``candidates`` fall on the sensor of a camera centred on RA/Dec, brightest first."""
    ra0, dec0 = math.radians(ra), math.radians(dec)
    sin_d0, cos_d0 = math.sin(dec0), math.cos(dec0)
    scale = math.radians(pixel_scale / 3600)
    turn = math.radians(rotation)
    cos_t, sin_t = math.cos(turn), math.sin(turn)
    width, height = SENSOR_SIZE
    cx, cy = SENSOR_CENTRE
    seen = []
    for star in candidates:
        r, d = math.radians(star.ra), math.radians(star.dec)
        cos_c = sin_d0 * math.sin(d) + cos_d0 * math.cos(d) * math.cos(r - ra0)
        if cos_c <= 0:
            continue
        xi = math.cos(d) * math.sin(r - ra0) / cos_c                                 # toward east
        eta = (cos_d0 * math.sin(d) - sin_d0 * math.cos(d) * math.cos(r - ra0)) / cos_c  # toward north
        u, v = -xi / scale, -eta / scale
        x, y = cx + u * cos_t - v * sin_t, cy + u * sin_t + v * cos_t
        if 0 <= x < width and 0 <= y < height:
            seen.append(SeenStar(x, y, star.mag, star.hip))
    seen.sort(key=lambda star: star.mag)
    return seen


def field_radius(pixel_scale: float = PIXEL_SCALE) -> float:
    """Degrees from the centre to a corner of the sensor."""
    return math.hypot(*SENSOR_SIZE) / 2 * pixel_scale / 3600


def in_view(ra: float, dec: float, rotation: float, pixel_scale: float = PIXEL_SCALE) -> List[SeenStar]:
    """The catalogue stars on the sensor, brightest first."""
    return project(ra, dec, rotation, near(ra, dec, field_radius(pixel_scale) + 0.01), pixel_scale)
