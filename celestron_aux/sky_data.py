"""Data for the page's Earth and sky spheres: bright stars and the continents."""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any, Dict, List

from . import star_catalog

LAND_PATH = Path(__file__).resolve().parent / "data" / "land.json"
SPHERE_MAG_LIMIT = 5.5  # about 2,800 stars: what the eye sees on a dark night


@functools.lru_cache(maxsize=1)
def bright_stars() -> List[List[float]]:
    """Hipparcos stars to magnitude 5.5 as [RA, Dec (J2000, degrees), V magnitude, HIP number]."""
    return [[round(star.ra, 3), round(star.dec, 3), round(star.mag, 2), star.hip]
            for star in star_catalog.stars() if star.mag <= SPHERE_MAG_LIMIT]


@functools.lru_cache(maxsize=1)
def land() -> Dict[str, Any]:
    """The continents' outlines as lists of [longitude, latitude] (Natural Earth 1:110m)."""
    return json.loads(LAND_PATH.read_text())
