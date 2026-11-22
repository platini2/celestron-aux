"""The stars a StarSense camera sees: a Hipparcos cut and the camera's geometry.

``data/hipparcos.bin.gz`` holds the Hipparcos main catalogue (ESA 1997) to V
magnitude 9, at J2000 (built by ``scripts/build_star_catalog.py``). The camera
geometry was measured on a real StarSense AutoAlign frame
(``tests/fixtures/starsense_frame.hex``): 1280 x 960 pixels of 19.41 arcseconds
(a 6.90 x 5.17 degree field), centred on pixel (640, 480), not mirrored. A sky
position angle ``rotation`` turns the view: at rotation 0 east is toward
smaller x and north toward smaller y. That frame matched with rotation 92.6.

Five frames from real cameras confirm it (19.39 to 19.41 arcseconds a pixel,
none mirrored), and show two more things the camera does: stars are where
their proper motion has taken them on the day (Alpha Centauri has moved 5
pixels since 2000), and stars closer than it can separate are one point.
"""

from __future__ import annotations

import bisect
import gzip
import math
import struct
import threading
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

CATALOG_PATH = Path(__file__).resolve().parent / "data" / "hipparcos.bin.gz"
_RECORD = struct.Struct("<ffhIhh")

SENSOR_SIZE: Tuple[int, int] = (1280, 960)
SENSOR_CENTRE: Tuple[float, float] = (640.0, 480.0)
PIXEL_SCALE = 19.41  # arcseconds per pixel
MAG_LIMIT = 9.0      # the catalogue's faintest stars
MERGE_RADIUS = 3.0   # pixels: closer stars are one point to the camera
_MAX_MOTION = 0.1    # degrees a star may have moved since 2000 (at most 7"/year in this catalogue)


@dataclass(frozen=True)
class CatalogStar:
    ra: float    # degrees, J2000
    dec: float   # degrees, J2000
    mag: float   # V magnitude
    hip: int     # Hipparcos number
    pm_ra: float = 0.0   # proper motion in RA times cos(Dec), milliarcseconds a year
    pm_dec: float = 0.0  # proper motion in Dec, milliarcseconds a year

    def at(self, year: float) -> "CatalogStar":
        """The star where its proper motion has taken it in ``year`` (J2000 epoch, ICRS)."""
        years = year - 2000.0
        if not years or not (self.pm_ra or self.pm_dec):
            return self
        cos_dec = math.cos(math.radians(self.dec))
        ra = self.ra + (self.pm_ra * years / 3.6e6 / cos_dec if cos_dec > 1e-9 else 0.0)
        return CatalogStar(ra % 360, self.dec + self.pm_dec * years / 3.6e6, self.mag, self.hip,
                           self.pm_ra, self.pm_dec)


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
            _stars = [CatalogStar(ra, dec, mag / 100, hip, pm_ra, pm_dec)
                      for ra, dec, mag, hip, pm_ra, pm_dec in _RECORD.iter_unpack(raw)]
            _decs = [star.dec for star in _stars]
        return _stars


def current_year(when: Optional[datetime] = None) -> float:
    """``when`` (default now) as a decimal year."""
    when = when or datetime.now(timezone.utc)
    start = datetime(when.year, 1, 1, tzinfo=when.tzinfo)
    end = datetime(when.year + 1, 1, 1, tzinfo=when.tzinfo)
    return when.year + (when - start) / (end - start)


def near(ra: float, dec: float, radius: float, year: float = 2000.0) -> List[CatalogStar]:
    """Catalogue stars within ``radius`` degrees of RA/Dec (ICRS, degrees), as they are in ``year``."""
    catalog = stars()
    lo = bisect.bisect_left(_decs, dec - radius - _MAX_MOTION)
    hi = bisect.bisect_right(_decs, dec + radius + _MAX_MOTION)
    cos_radius = math.cos(math.radians(radius))
    sin_d0, cos_d0 = math.sin(math.radians(dec)), math.cos(math.radians(dec))
    found = []
    for star in catalog[lo:hi]:
        star = star.at(year)
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


def merge_close(seen: Sequence[SeenStar], radius: float = MERGE_RADIUS) -> List[SeenStar]:
    """Stars closer than ``radius`` pixels as the one point a camera sees: at their
    brightness-weighted centre, as bright as both together, brightest first."""
    points: List[List[float]] = []  # x, y, flux, hip
    for star in seen:  # brightest first, so each star joins a brighter point
        flux = 10 ** (-0.4 * star.mag)
        for point in points:
            if (point[0] - star.x) ** 2 + (point[1] - star.y) ** 2 < radius ** 2:
                total = point[2] + flux
                point[0] = (point[0] * point[2] + star.x * flux) / total
                point[1] = (point[1] * point[2] + star.y * flux) / total
                point[2] = total
                break
        else:
            points.append([star.x, star.y, flux, star.hip])
    merged = [SeenStar(x, y, -2.5 * math.log10(flux), int(hip)) for x, y, flux, hip in points]
    merged.sort(key=lambda star: star.mag)
    return merged


def in_view(ra: float, dec: float, rotation: float, pixel_scale: float = PIXEL_SCALE,
            year: Optional[float] = None) -> List[SeenStar]:
    """The points a camera sees: catalogue stars on the sensor in ``year`` (default now),
    with stars it cannot separate merged, brightest first."""
    year = current_year() if year is None else year
    candidates = near(ra, dec, field_radius(pixel_scale) + 0.01, year)
    return merge_close(project(ra, dec, rotation, candidates, pixel_scale))
