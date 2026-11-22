"""Plate-solve a StarSense star list on the Hipparcos catalogue, with no hint of where it is.

It relies on what five frames from real cameras showed: the scale (19.4 arcseconds a
pixel) and that the image is not mirrored. So a pair of the list's brightest points,
their distance in degrees known, can be looked up in an index of catalogue star pairs
sorted by separation; each candidate fixes the rotation, and the other bright points
must then land on catalogue stars. The best candidate is refined with every point by
least squares (scale, rotation and centre).

Pure Python: building the index takes a few seconds the first time; a solve then
takes well under a second or so.
"""

from __future__ import annotations

import bisect
import hashlib
import queue
from collections import OrderedDict
import logging
import math
import threading
import time
from array import array
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple

from . import star_catalog
from .bus import BusEvent
from .protocol import StarSenseFrame
from .star_names import STAR_NAMES
from .starsense import Star, parse_star_list

log = logging.getLogger(__name__)

INDEX_MAGS = (6.5, 7.5)  # pair indexes: fast first; the deeper one for sparse fields, built when needed
MAX_SEPARATION = 8.7     # degrees: the sensor's diagonal
MIN_SEPARATION = 0.4
BIN = 0.02               # degrees per separation bin
SCALE_TOLERANCE = 0.02   # a real camera's scale is within 1 % of 19.4"/px
BRIGHT_POINTS = 8        # the list's brightest points used to find candidates
CHECK_POINTS = 14        # ... and checked against the catalogue
MATCH_RADIUS = 60 / 3600 # degrees: a checked point must be this close to a catalogue star
MIN_CHECKS = 4           # catalogue stars the other bright points must land on
TIME_LIMIT = 8.0         # seconds per index before giving up on a list

Vector = Tuple[float, float, float]


def _vector(ra: float, dec: float) -> Vector:
    r, d = math.radians(ra), math.radians(dec)
    return (math.cos(d) * math.cos(r), math.cos(d) * math.sin(r), math.sin(d))


def _radec(v: Vector) -> Tuple[float, float]:
    x, y, z = v
    return math.degrees(math.atan2(y, x)) % 360, math.degrees(math.asin(max(-1.0, min(1.0, z))))


@dataclass
class _Index:
    vectors: List[Vector]
    bins: Dict[int, array]                         # separation bin -> pairs (i, j) flattened
    grid: Dict[Tuple[int, int], List[Vector]]      # 1-degree cells -> catalogue stars to magnitude 9 (shared)


_indexes: Dict[float, _Index] = {}
_index_lock = threading.Lock()
_grid: Optional[Dict[Tuple[int, int], List[Vector]]] = None


def _cell(v: Vector) -> Tuple[int, int]:
    ra, dec = _radec(v)
    return int(dec + 90), int(ra) if abs(dec) < 89 else 0


def _build_index(year: float, limit: float) -> _Index:
    global _grid
    stars = [star.at(year) for star in star_catalog.stars()]
    bright = sorted((s for s in stars if s.mag <= limit), key=lambda s: s.dec)
    vectors = [_vector(s.ra, s.dec) for s in bright]
    decs = [s.dec for s in bright]
    cos_max, cos_min = math.cos(math.radians(MAX_SEPARATION)), math.cos(math.radians(MIN_SEPARATION))
    bins: Dict[int, array] = {}
    for i, (vi, si) in enumerate(zip(vectors, bright)):
        hi = bisect.bisect_right(decs, si.dec + MAX_SEPARATION)
        xi, yi, zi = vi
        for j in range(i + 1, hi):
            xj, yj, zj = vectors[j]
            dot = xi * xj + yi * yj + zi * zj
            if cos_max <= dot <= cos_min:
                key = int(math.degrees(math.acos(dot)) / BIN)
                pairs = bins.get(key)
                if pairs is None:
                    pairs = bins[key] = array("I")
                pairs.append(i)
                pairs.append(j)
    if _grid is None:
        _grid = {}
        for star in stars:
            v = _vector(star.ra, star.dec)
            _grid.setdefault(_cell(v), []).append(v)
    return _Index(vectors, bins, _grid)


def index(year: Optional[float] = None, limit: float = INDEX_MAGS[0]) -> _Index:
    """The pair index of the stars to magnitude ``limit``, built once (where they are this year)."""
    with _index_lock:
        if limit not in _indexes:
            _indexes[limit] = _build_index(star_catalog.current_year() if year is None else year, limit)
        return _indexes[limit]


def _near_catalogue(idx: _Index, v: Vector) -> bool:
    dec_cell, ra_cell = _cell(v)
    cos_r = math.cos(math.radians(MATCH_RADIUS))
    near_pole = dec_cell <= 4 or dec_cell >= 175  # there a small circle spans many 1-degree RA cells
    for dd in (-1, 0, 1):
        for dr in (range(360) if near_pole else (-1, 0, 1)):
            for w in idx.grid.get((dec_cell + dd, (ra_cell + dr) % 360), ()):
                if v[0] * w[0] + v[1] * w[1] + v[2] * w[2] >= cos_r:
                    return True
    return False


def _from_tangent(centre: Vector, xi: float, eta: float) -> Vector:
    """The direction at standard coordinates (xi east, eta north, radians) around ``centre``."""
    ra0, dec0 = (math.radians(a) for a in _radec(centre))
    east = (-math.sin(ra0), math.cos(ra0), 0.0)
    north = (-math.sin(dec0) * math.cos(ra0), -math.sin(dec0) * math.sin(ra0), math.cos(dec0))
    v = [centre[k] + xi * east[k] + eta * north[k] for k in range(3)]
    n = math.sqrt(sum(c * c for c in v))
    return (v[0] / n, v[1] / n, v[2] / n)


def _to_tangent(centre: Vector, v: Vector) -> Optional[Tuple[float, float]]:
    ra0, dec0 = (math.radians(a) for a in _radec(centre))
    east = (-math.sin(ra0), math.cos(ra0), 0.0)
    north = (-math.sin(dec0) * math.cos(ra0), -math.sin(dec0) * math.sin(ra0), math.cos(dec0))
    c = sum(centre[k] * v[k] for k in range(3))
    if c <= 0:
        return None
    return (sum(east[k] * v[k] for k in range(3)) / c, sum(north[k] * v[k] for k in range(3)) / c)


@dataclass
class Solution:
    ra: float                 # degrees, at the sensor centre (ICRS)
    dec: float
    rotation: float           # degrees, the star_catalog convention
    scale: float              # arcseconds a pixel
    matched: int              # list points within 2 px of a catalogue star
    points: int               # list points
    rms: float                # pixels, over the matched points
    stars: List[star_catalog.SeenStar] = field(default_factory=list)  # the catalogue as the camera sees it
    on_catalogue: List[bool] = field(default_factory=list)            # per list point


def _candidate(idx: _Index, pts: Sequence[Star], a: int, b: int, i: int, j: int,
               scale_rad: float) -> Tuple[int, Optional[Tuple[Vector, float, float]]]:
    """How many bright points land on catalogue stars if points a, b are stars i, j."""
    centre = idx.vectors[i]
    tangent = _to_tangent(centre, idx.vectors[j])
    if tangent is None:
        return 0, None
    want = (-tangent[0], -tangent[1])           # (-xi, -eta): the pixel axes at rotation 0
    got = (pts[b].x - pts[a].x, pts[b].y - pts[a].y)
    ratio = math.hypot(*got) * scale_rad / math.hypot(*want)
    if abs(ratio - 1) > SCALE_TOLERANCE:
        return 0, None
    theta = math.atan2(got[1], got[0]) - math.atan2(want[1], want[0])
    cos_t, sin_t = math.cos(theta), math.sin(theta)
    k = scale_rad / ratio
    hits = 0
    for c, p in enumerate(pts[:CHECK_POINTS]):
        if c in (a, b):
            continue
        dx, dy = p.x - pts[a].x, p.y - pts[a].y
        u, v = (cos_t * dx + sin_t * dy) * k, (-sin_t * dx + cos_t * dy) * k
        if _near_catalogue(idx, _from_tangent(centre, -u, -v)):
            hits += 1
    return hits, (centre, theta, k)


def _plate_centre(pts: Sequence[Star], a: int, centre: Vector, theta: float, k: float) -> Tuple[float, float, float]:
    """The sky at the sensor centre and the rotation there. The rotation found at the bright
    star differs from the one at the centre where meridians converge (near a celestial pole),
    so it is measured again at the centre: from where a point 100 px to its right lands."""
    cos_t, sin_t = math.cos(theta), math.sin(theta)

    def sky(x: float, y: float) -> Vector:
        dx, dy = x - pts[a].x, y - pts[a].y
        u, v = (cos_t * dx + sin_t * dy) * k, (-sin_t * dx + cos_t * dy) * k
        return _from_tangent(centre, -u, -v)

    cx, cy = star_catalog.SENSOR_CENTRE
    middle = sky(cx, cy)
    tangent = _to_tangent(middle, sky(cx + 100, cy))
    rotation = -math.degrees(math.atan2(-tangent[1], -tangent[0])) if tangent else math.degrees(theta)
    ra, dec = _radec(middle)
    return ra, dec, rotation % 360


def _refine(pts: Sequence[Star], ra: float, dec: float, rotation: float, year: float) -> Solution:
    """Least-squares fit of scale, rotation and centre to every point, then the camera's view there."""
    centre = _vector(ra, dec)
    stars = star_catalog.near(ra, dec, star_catalog.field_radius() + 0.5, year)
    tangent = [t for t in (_to_tangent(centre, _vector(s.ra, s.dec)) for s in stars) if t is not None]
    T = [(-xi, -eta) for xi, eta in tangent]
    k = 1 / math.radians(star_catalog.PIXEL_SCALE / 3600)
    a, b = k * math.cos(math.radians(rotation)), k * math.sin(math.radians(rotation))
    tx, ty = star_catalog.SENSOR_CENTRE
    tolerance = 25.0
    pairs: List[Tuple[Tuple[float, float], Star]] = []
    for _ in range(12):
        pairs = []
        for p in pts:
            best, best_d = None, tolerance
            for u, v in T:
                x, y = a * u - b * v + tx, b * u + a * v + ty
                d = math.hypot(x - p.x, y - p.y)
                if d < best_d:
                    best, best_d = (u, v), d
            if best is not None:
                pairs.append((best, p))
        if len(pairs) < 3:
            break
        n = len(pairs)
        mu = (sum(q[0][0] for q in pairs) / n, sum(q[0][1] for q in pairs) / n)
        mp = (sum(q[1].x for q in pairs) / n, sum(q[1].y for q in pairs) / n)
        den = sum((q[0][0] - mu[0]) ** 2 + (q[0][1] - mu[1]) ** 2 for q in pairs)
        a = sum((q[0][0] - mu[0]) * (q[1].x - mp[0]) + (q[0][1] - mu[1]) * (q[1].y - mp[1]) for q in pairs) / den
        b = sum((q[0][0] - mu[0]) * (q[1].y - mp[1]) - (q[0][1] - mu[1]) * (q[1].x - mp[0]) for q in pairs) / den
        tx, ty = mp[0] - a * mu[0] + b * mu[1], mp[1] - b * mu[0] - a * mu[1]
        tolerance = max(2.0, tolerance * 0.6)
    scale_rad = 1 / math.hypot(a, b)
    theta = math.atan2(b, a)
    det = a * a + b * b
    cx, cy = star_catalog.SENSOR_CENTRE
    u = (a * (cx - tx) + b * (cy - ty)) / det
    v = (-b * (cx - tx) + a * (cy - ty)) / det
    ra, dec = _radec(_from_tangent(centre, -u, -v))
    scale = 3600 * math.degrees(scale_rad)
    rotation = math.degrees(theta) % 360
    seen = star_catalog.in_view(ra, dec, rotation, scale, year)
    on, residuals = [], []
    for p in pts:
        d = min((math.hypot(s.x - p.x, s.y - p.y) for s in seen), default=99.0)
        on.append(d < 2.0)
        if d < 2.0:
            residuals.append(d)
    rms = math.sqrt(sum(r * r for r in residuals) / len(residuals)) if residuals else 0.0
    return Solution(ra, dec, rotation, scale, len(residuals), len(pts), rms, seen, on)


def solve(stars: Sequence[Star], year: Optional[float] = None, time_limit: float = TIME_LIMIT) -> Optional[Solution]:
    """The plate of a star list (brightest first), or None if it does not solve within
    ``time_limit`` seconds per index (a list that solves usually takes under a second)."""
    pts = [s for s in stars if s.x or s.y]
    if len(pts) < 6:
        return None
    year = star_catalog.current_year() if year is None else year
    for limit in INDEX_MAGS:  # a field with few bright stars needs the deeper index
        solution = _solve_with(pts, index(year, limit), year, time_limit)
        if solution is not None:
            return solution
    return None


def _solve_with(pts: Sequence[Star], idx: _Index, year: float, time_limit: float) -> Optional[Solution]:
    scale_rad = math.radians(star_catalog.PIXEL_SCALE / 3600)
    deadline = time.monotonic() + time_limit
    best: Tuple[int, Optional[Tuple[int, Vector, float, float]]] = (0, None)
    bright = list(range(min(BRIGHT_POINTS, len(pts))))
    for a in bright:
        for b in bright:
            if b <= a:
                continue
            separation = math.hypot(pts[b].x - pts[a].x, pts[b].y - pts[a].y) * star_catalog.PIXEL_SCALE / 3600
            if not MIN_SEPARATION <= separation <= MAX_SEPARATION:
                continue
            if time.monotonic() > deadline:
                break
            spread = separation * SCALE_TOLERANCE
            for key in range(int((separation - spread) / BIN), int((separation + spread) / BIN) + 1):
                pairs = idx.bins.get(key)
                if pairs is None:
                    continue
                for n in range(0, len(pairs), 2):
                    for i, j in ((pairs[n], pairs[n + 1]), (pairs[n + 1], pairs[n])):
                        hits, plate = _candidate(idx, pts, a, b, i, j, scale_rad)
                        if hits > best[0] and plate is not None:
                            best = (hits, (a,) + plate)
            if best[0] >= CHECK_POINTS - 4:
                break
        if best[0] >= CHECK_POINTS - 4:
            break
    if best[0] < MIN_CHECKS or best[1] is None:
        return None
    a, centre, theta, k = best[1]
    ra, dec, rotation = _plate_centre(pts, a, centre, theta, k)
    solution = _refine(pts, ra, dec, rotation, year)
    return solution if solution.matched >= 10 else None


def list_id(payload: bytes) -> str:
    """A short name for a star list, the same wherever it is seen."""
    return hashlib.sha1(payload).hexdigest()[:12]


def solution_json(solution: Solution) -> Dict[str, object]:
    return {
        "status": "solved", "ra": round(solution.ra, 4), "dec": round(solution.dec, 4),
        "rotation": round(solution.rotation, 2), "scale": round(solution.scale, 3),
        "matched": solution.matched, "points": solution.points, "rms": round(solution.rms, 2),
        "stars": [[round(s.x, 1), round(s.y, 1), round(s.mag, 2), s.hip] for s in solution.stars],
        "on_catalogue": solution.on_catalogue,
        "names": {str(s.hip): STAR_NAMES[s.hip] for s in solution.stars if s.hip in STAR_NAMES},
    }


class StarListSolver:
    """Bus listener that plate-solves every StarSense star list it sees, one at a time in the
    background (traffic never waits), keeping the last few results by list id."""

    KEEP = 16

    def __init__(self, enabled: bool = True) -> None:
        self.enabled = enabled
        self._results: "OrderedDict[str, Dict[str, object]]" = OrderedDict()
        self._lock = threading.Lock()
        self._queue: "queue.Queue[Tuple[str, bytes]]" = queue.Queue()
        self._worker: Optional[threading.Thread] = None

    def __call__(self, event: BusEvent) -> None:
        frame = event.frame
        if not self.enabled or not isinstance(frame, StarSenseFrame):
            return
        key = list_id(frame.payload)
        with self._lock:
            if key in self._results:
                return  # the same list again (a repeated request): already solved or solving
            self._results[key] = {"status": "solving"}
            while len(self._results) > self.KEEP:
                self._results.popitem(last=False)
            if self._worker is None:
                self._worker = threading.Thread(target=self._run, name="aux-star-solver", daemon=True)
                self._worker.start()
        self._queue.put((key, frame.payload))

    def result(self, key: str) -> Optional[Dict[str, object]]:
        with self._lock:
            result = self._results.get(key)
            return None if result is None else dict(result)

    def _run(self) -> None:
        while True:
            key, payload = self._queue.get()
            started = time.monotonic()
            result: Dict[str, object]
            try:
                solution = solve(parse_star_list(payload))
                result = solution_json(solution) if solution else {"status": "failed"}
            except Exception:  # never let a bad list stop the solver
                log.exception("plate solving a star list failed")
                result = {"status": "failed"}
            result["seconds"] = round(time.monotonic() - started, 2)
            with self._lock:
                if key in self._results:
                    self._results[key] = result
