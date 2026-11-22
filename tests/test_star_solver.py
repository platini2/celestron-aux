"""Plate solving StarSense star lists on Hipparcos, with no hint of where they are."""

import math
import random
import time
from pathlib import Path

import pytest

from celestron_aux import star_solver
from celestron_aux.bus import BusEvent, Direction
from celestron_aux.protocol import StarSenseFrame
from celestron_aux.starsense import Star, parse_star_list
from celestron_aux.starsense_v1_emulator import StarSenseV1Emulator

from .fakes import wait_until

REAL_LISTS = Path(__file__).parent / "fixtures" / "starsense_real_lists.txt"


def real_lists():
    for line in REAL_LISTS.read_text().splitlines():
        if line and not line.startswith("#"):
            name, ra, dec, rotation, payload = line.split()
            yield name, (float(ra), float(dec), float(rotation)), bytes.fromhex(payload)


def separation(ra1, dec1, ra2, dec2):
    r1, d1, r2, d2 = map(math.radians, (ra1, dec1, ra2, dec2))
    return math.degrees(math.acos(min(1.0, math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(r1 - r2))))


@pytest.mark.parametrize("name,sky,payload", list(real_lists()), ids=lambda value: value if isinstance(value, str) else "")
def test_real_camera_lists_solve_blind(name, sky, payload):
    solution = star_solver.solve(parse_star_list(payload), year=2025.0)
    assert solution is not None
    assert separation(solution.ra, solution.dec, sky[0], sky[1]) < 0.01
    assert abs((solution.rotation - sky[2] + 180) % 360 - 180) < 0.2
    assert 19.35 < solution.scale < 19.45 and solution.rms < 1.0 and solution.matched >= 55
    assert len(solution.on_catalogue) == 99 and sum(solution.on_catalogue) == solution.matched


def test_emulated_lists_from_anywhere_in_the_sky_solve_in_the_right_place():
    rng = random.Random(3)
    for n in range(12):
        ra, dec, rotation = rng.uniform(0, 360), math.degrees(math.asin(rng.uniform(-1, 1))), rng.uniform(0, 360)
        emulator = StarSenseV1Emulator(lambda packet: None, rng=random.Random(n))
        emulator.sky_source = lambda: (ra, dec, rotation)
        solution = star_solver.solve(parse_star_list(emulator.star_list()))
        assert solution is not None and separation(solution.ra, solution.dec, ra, dec) < 0.01, (ra, dec)


def test_near_the_celestial_pole_too():
    # Meridians converge there: the rotation must be measured at the field's centre.
    emulator = StarSenseV1Emulator(lambda packet: None, rng=random.Random(1))
    emulator.sky_source = lambda: (291.39, -80.75, 290.1)
    solution = star_solver.solve(parse_star_list(emulator.star_list()))
    assert solution is not None and separation(solution.ra, solution.dec, 291.39, -80.75) < 0.01


def test_random_points_do_not_solve():
    noise = random.Random(9)
    points = [Star(noise.uniform(0, 1280), noise.uniform(0, 960)) for _ in range(99)]
    assert star_solver.solve(points, time_limit=1.5) is None


def test_every_list_seen_is_solved_in_the_background_once():
    solver = star_solver.StarListSolver()
    payload = next(payload for name, _, payload in real_lists() if name == "lepus")
    frame = StarSenseFrame(payload)
    key = star_solver.list_id(payload)
    started = time.monotonic()
    solver(BusEvent(0.0, Direction.RX, frame, frame.encode()))
    assert time.monotonic() - started < 0.5  # the bus never waits for it
    assert wait_until(lambda: solver.result(key)["status"] != "solving", timeout=30)
    result = solver.result(key)
    assert result["status"] == "solved" and abs(result["ra"] - 88.514) < 0.01
    solver(BusEvent(1.0, Direction.RX, frame, frame.encode()))  # the same list again: not solved twice
    assert solver.result(key)["status"] == "solved"
