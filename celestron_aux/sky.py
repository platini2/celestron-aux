"""Sky coordinates for the emulators: from where a telescope points to RA/Dec.

Accurate to well under an arcminute, which is what the emulators need: no
refraction, nutation or aberration (each at most about half an arcminute), but
precession is included, because CPWI works in J2000 and 26 years of precession
already moves the sky by a third of a degree.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import List, Tuple

Matrix = List[List[float]]


def julian_date(when: datetime) -> float:
    return when.timestamp() / 86400 + 2440587.5


def gmst_degrees(when: datetime) -> float:
    """Greenwich mean sidereal time in degrees (accurate to about a second)."""
    days = julian_date(when) - 2451545.0
    return (280.46061837 + 360.98564736629 * days) % 360


def altaz_to_radec(alt: float, az: float, latitude: float, longitude: float, when: datetime) -> Tuple[float, float]:
    """RA/Dec of date (degrees) of the direction ``alt``/``az`` (azimuth from north through east)."""
    a, z, phi = map(math.radians, (alt, az, latitude))
    # Rounding can leave this a hair beyond +-1 exactly at the poles (a mount pointing at the pole
    # reads as such on some platforms' math libraries): clamp it, or asin raises "math domain error".
    sine_dec = math.sin(phi) * math.sin(a) + math.cos(phi) * math.cos(a) * math.cos(z)
    dec = math.asin(max(-1.0, min(1.0, sine_dec)))
    hour_angle = math.atan2(-math.sin(z) * math.cos(a), math.cos(phi) * math.sin(a) - math.sin(phi) * math.cos(a) * math.cos(z))
    lst = gmst_degrees(when) + longitude
    return (lst - math.degrees(hour_angle)) % 360, math.degrees(dec)


def _precession_matrix(when: datetime) -> Matrix:
    """J2000 to mean equator and equinox of date (IAU 1976 angles)."""
    t = (julian_date(when) - 2451545.0) / 36525
    arcsec = math.pi / 180 / 3600
    zeta = (2306.2181 * t + 0.30188 * t ** 2 + 0.017998 * t ** 3) * arcsec
    z = (2306.2181 * t + 1.09468 * t ** 2 + 0.018203 * t ** 3) * arcsec
    theta = (2004.3109 * t - 0.42665 * t ** 2 - 0.041833 * t ** 3) * arcsec
    cz, sz, cZ, sZ, ct, st = math.cos(zeta), math.sin(zeta), math.cos(z), math.sin(z), math.cos(theta), math.sin(theta)
    return [[cZ * ct * cz - sZ * sz, -cZ * ct * sz - sZ * cz, -cZ * st],
            [sZ * ct * cz + cZ * sz, -sZ * ct * sz + cZ * cz, -sZ * st],
            [st * cz, -st * sz, ct]]


def of_date_to_j2000(ra: float, dec: float, when: datetime) -> Tuple[float, float]:
    """Undo precession: RA/Dec of date to J2000 (degrees)."""
    r, d = math.radians(ra), math.radians(dec)
    v = (math.cos(d) * math.cos(r), math.cos(d) * math.sin(r), math.sin(d))
    p = _precession_matrix(when)
    w = [sum(p[row][col] * v[row] for row in range(3)) for col in range(3)]  # the transpose: date to J2000
    return math.degrees(math.atan2(w[1], w[0])) % 360, math.degrees(math.asin(max(-1.0, min(1.0, w[2]))))


def altaz_to_j2000(alt: float, az: float, latitude: float, longitude: float, when: datetime) -> Tuple[float, float]:
    return of_date_to_j2000(*altaz_to_radec(alt, az, latitude, longitude, when), when)


def parallactic_angle(ra: float, dec: float, latitude: float, longitude: float, when: datetime) -> float:
    """How far an alt-az view is turned from north, in degrees."""
    hour_angle = math.radians((gmst_degrees(when) + longitude - ra) % 360)
    d, phi = math.radians(dec), math.radians(latitude)
    return math.degrees(math.atan2(math.sin(hour_angle),
                                   math.tan(phi) * math.cos(d) - math.sin(d) * math.cos(hour_angle)))
