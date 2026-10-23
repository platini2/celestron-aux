import io
import math
import random
import struct
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from celestron_aux.app import AppConfig, ScannerApp
from celestron_aux.bus import BusEvent, Direction
from celestron_aux.protocol import AuxPacket, FrameParser
from celestron_aux.starsense_emulator import (ALIGNMENT_POINTS, OFFSET_ANGLE, OFFSET_LENGTH, StarSenseEmulator,
                                              gmst_degrees)

from .fakes import FakeMount, wait_until

FIXTURE = Path(__file__).parent / "fixtures" / "cpwi_starsense_requests.txt"
NOW = datetime(2026, 10, 18, 23, 21, 0, tzinfo=timezone.utc)


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def emulator(**kwargs):
    sent = []
    clock = Clock()
    emu = StarSenseEmulator(sent.append, clock=clock, now=lambda: NOW + timedelta(seconds=clock.t), **kwargs)
    emu.enable()
    return emu, sent, clock


def ask(emu, sent, command, data=b""):
    """Send one request from CPWI (0x20) and return the emulator's reply payload, or None."""
    before = len(sent)
    request = AuxPacket(0x20, 0xBD, command, data)
    emu(BusEvent(0.0, Direction.RX, request, request.encode()))
    if len(sent) == before:
        return None
    reply = sent[-1]
    assert (reply.source, reply.destination, reply.command) == (0xBD, 0x20, command)
    return reply.data


def radec(payload):
    return tuple(math.degrees(v) for v in struct.unpack(">ff", payload[1:9]))


def separation(a, b):
    (r1, d1), (r2, d2) = (map(math.radians, a), map(math.radians, b))
    return math.degrees(math.acos(min(1, math.sin(d1) * math.sin(d2) + math.cos(d1) * math.cos(d2) * math.cos(r1 - r2))))


def test_it_answers_cpwi_with_the_same_reply_lengths_as_a_real_camera():
    """Replay CPWI's real requests (with their timing) and compare every reply's length."""
    emu, sent, clock = emulator()
    statuses = []
    for line in FIXTURE.read_text().splitlines():
        if line.startswith("#"):
            continue
        t, request_hex, real_length = line.split()
        clock.t = float(t)
        (parsed,) = FrameParser().feed(bytes.fromhex(request_hex))
        request = parsed.frame
        before = len(sent)
        emu(BusEvent(clock.t, Direction.RX, request, request.encode()))
        assert len(sent) == before + 1, f"no reply to 0x{request.command:02x} at {t}"
        assert len(sent[-1].data) == int(real_length), f"0x{request.command:02x} at {t}"
        if request.command == 0x02:
            statuses.append(sent[-1].data[0])
    assert set(statuses) == {0, 1} and statuses.count(1) >= 8  # in progress, then solved


def test_four_points_in_turn_with_ra_following_the_clock():
    emu, sent, clock = emulator(solve_time=0)
    latitude, longitude = 51.4779, -0.0015
    ask(emu, sent, 0x31, struct.pack(">ff", latitude, longitude))
    solves = []
    for _ in range(5):
        ask(emu, sent, 0x01)
        solves.append(radec(ask(emu, sent, 0x02)))
        clock.t += 60
    for n, (ra, dec) in enumerate(solves):
        point = ALIGNMENT_POINTS[n % 4]
        lst = gmst_degrees(NOW + timedelta(seconds=60 * n)) + longitude
        assert dec == pytest.approx(point.dec, abs=1e-4)
        assert ((lst - ra) / 15 + 12) % 24 - 12 == pytest.approx(point.hour_angle, abs=1e-4)  # same hour angle
    assert solves[4][1] == solves[0][1] and solves[4][0] != solves[0][0]  # point 1 again, a little later


def test_without_a_location_the_recorded_positions_are_used():
    emu, sent, _ = emulator(solve_time=0)
    ask(emu, sent, 0x01)
    ra, dec = radec(ask(emu, sent, 0x02))
    assert (ra, dec) == (pytest.approx(ALIGNMENT_POINTS[0].ra, abs=1e-4), pytest.approx(ALIGNMENT_POINTS[0].dec, abs=1e-4))


def test_a_solve_is_in_progress_first():
    emu, sent, clock = emulator(solve_time=8)
    ask(emu, sent, 0x01)
    assert ask(emu, sent, 0x02) == bytes(9)
    assert ask(emu, sent, 0x4B) == bytes(9)  # no telescope position before the solve is done
    clock.t = 8
    assert ask(emu, sent, 0x02)[0] == 1
    assert ask(emu, sent, 0x4B)[0] == 1


def test_telescope_position_and_rotation_match_the_real_camera_geometry():
    emu, sent, _ = emulator(solve_time=0)
    ask(emu, sent, 0x01)
    solve = radec(ask(emu, sent, 0x02))
    telescope = radec(ask(emu, sent, 0x4B))
    rotation = struct.unpack(">f", ask(emu, sent, 0x45))[0]
    assert rotation == pytest.approx(ALIGNMENT_POINTS[0].rotation, abs=1e-3)
    assert separation(solve, telescope) == pytest.approx(OFFSET_LENGTH, abs=1e-4)
    # the real camera's 0x4b for this point was RA 276.6048, Dec -3.2768; the emulator is within 10"
    assert separation(telescope, (276.6048, -3.2768)) * 3600 < 10
    assert OFFSET_ANGLE == 117.6


def test_calibration_is_stored_like_a_real_camera_does():
    emu, sent, clock = emulator(calibrate_time=7)
    assert ask(emu, sent, 0x07) == b"\x00"  # not calibrated
    assert [ask(emu, sent, 0x08, bytes((slot,))) for slot in range(6)] == [bytes(4)] * 6
    altair = (297.70046, 8.87121)
    assert ask(emu, sent, 0x03, struct.pack(">ff", *map(math.radians, altair))) == b""
    assert ask(emu, sent, 0x04) == bytes(5)  # in progress
    clock.t = 7
    assert ask(emu, sent, 0x04) == bytes.fromhex("0100e6007a")  # solved, x 230, y 122
    assert ask(emu, sent, 0x07) == b"\x01"
    slots = [struct.unpack(">f", ask(emu, sent, 0x08, bytes((slot,))))[0] for slot in range(6)]
    assert (slots[2], slots[3]) == (pytest.approx(altair[0], abs=1e-3), pytest.approx(altair[1], abs=1e-4))
    assert separation((slots[0], slots[1]), altair) == pytest.approx(OFFSET_LENGTH, abs=1e-3)
    assert slots[5] == pytest.approx((NOW + timedelta(seconds=7, days=1)).timestamp(), abs=256)


def test_it_stays_silent_when_off_and_ignores_other_devices_and_its_own_replies():
    emu, sent, _ = emulator()
    emu.disable()
    assert ask(emu, sent, 0xFE) is None
    emu.enable()
    for packet in (AuxPacket(0x20, 0xB4, 0xFE), AuxPacket(0xBD, 0x20, 0xFE, b"\x01")):
        emu(BusEvent(0.0, Direction.RX, packet, packet.encode()))
    assert sent == []
    assert ask(emu, sent, 0xFE) == bytes.fromhex("230c0005")
    assert ask(emu, sent, 0x99) is None  # unknown command: no guess


def start(**config):
    mount = FakeMount({})
    app = ScannerApp(mount, AppConfig(save_starsense_images=False, **config), stream=io.StringIO())
    app.bus._inter_frame_delay = 0
    app.start(initial_scan=False)
    return app, mount


def test_the_app_refuses_to_emulate_next_to_a_real_camera_and_lists_the_emulated_one():
    app, mount = start()
    try:
        mount.inject(AuxPacket(0xBD, 0x2F, 0xFE, bytes.fromhex("230c0005")).encode())  # a real camera answers
        assert wait_until(lambda: app.registry.contains(0xBD))
        with pytest.raises(ValueError, match="already answered"):
            app.enable_starsense_emulation()
        app.registry.remove(0xBD)  # the camera is gone
        app.enable_starsense_emulation()
        assert (0xBD, "35.12.5 (emulated)") in app.devices()
        mount.inject(AuxPacket(0x20, 0xBD, 0xFE).encode())  # CPWI asks the camera for its version
        replies = lambda: [p.frame for chunk in mount.written for p in FrameParser().feed(chunk)]
        assert wait_until(lambda: any(r.source == 0xBD and r.destination == 0x20 for r in replies()))
        app.disable_starsense_emulation()
        assert not app.registry.contains(0xBD)
    finally:
        app.close()


def test_listen_only_keeps_the_emulator_quiet():
    app, mount = start(listen_only=True)
    try:
        app.enable_starsense_emulation()
        mount.inject(AuxPacket(0x20, 0xBD, 0xFE).encode())
        assert not wait_until(lambda: mount.written, timeout=0.5)
    finally:
        app.close()


# --- solves that follow an emulated telescope -----------------------------------------------

def coupled(**setup):
    from celestron_aux.mount_emulator import MountEmulator, MountSetup

    clock = Clock()
    mount = MountEmulator(lambda packet: None, clock=clock)
    mount.configure(MountSetup(**setup))
    mount.enable()
    sent = []
    camera = StarSenseEmulator(sent.append, clock=clock, now=lambda: NOW + timedelta(seconds=clock.t),
                               solve_time=0, rng=random.Random(7))
    camera.pointing_source = mount.sky_pointing
    camera.enable()
    ask(camera, sent, 0x31, struct.pack(">ff", 51.4779, -0.0015))  # what CPWI sends the camera
    ask(camera, sent, 0x32, int((NOW - datetime(2000, 1, 1, tzinfo=timezone.utc)).total_seconds()).to_bytes(4, "big"))
    return mount, camera, sent, clock


def solve(camera, sent):
    ask(camera, sent, 0x01)
    return radec(ask(camera, sent, 0x02))


def test_solves_report_where_the_emulated_telescope_points():
    pytest.importorskip("astropy")
    import warnings
    import astropy.units as u
    from astropy.coordinates import AltAz, EarthLocation, FK5, SkyCoord
    from astropy.time import Time

    warnings.filterwarnings("ignore")
    mount, camera, sent, clock = coupled(model="cpc", initial_alt=40, initial_az=120)
    site = EarthLocation(lat=51.4779 * u.deg, lon=-0.0015 * u.deg)
    errors = []
    for azm_goto, alt_goto in ((0, 0), (60, 10), (200, -25)):
        mount.answer(0x10, 0x02, int(azm_goto % 360 / 360 * (1 << 24)).to_bytes(3, "big"))
        mount.answer(0x11, 0x02, int(alt_goto % 360 / 360 * (1 << 24)).to_bytes(3, "big"))
        clock.t += 120  # long enough to arrive
        alt, az, _ = mount.sky_pointing()
        ra, dec = solve(camera, sent)
        frame = AltAz(obstime=Time(NOW + timedelta(seconds=clock.t)), location=site, pressure=0)
        seen = SkyCoord(ra=ra * u.deg, dec=dec * u.deg, frame=FK5(equinox="J2000")).transform_to(frame)
        errors.append(SkyCoord(alt=alt * u.deg, az=az * u.deg, frame=frame).separation(seen).arcsec)
    assert all(0 < e < 90 for e in errors), errors  # small (a solve error plus nutation and aberration), never zero


def test_solves_move_with_the_telescope_without_any_outside_library():
    mount, camera, sent, clock = coupled(model="evolution", initial_alt=30, initial_az=180)
    first = solve(camera, sent)
    again = solve(camera, sent)
    assert 0 < separation(first, again) * 3600 < 120  # the same place: only the small solve error differs
    mount.answer(0x10, 0x04, int(20 / 360 * (1 << 24)).to_bytes(3, "big"))  # 20 degrees round, instantly (a sync)
    moved = solve(camera, sent)                                                # so the sky has not turned meanwhile
    expected = math.degrees(math.acos(math.cos(math.radians(30)) ** 2 * math.cos(math.radians(20))
                                      + math.sin(math.radians(30)) ** 2))  # the angle between the two pointings
    assert separation(first, moved) == pytest.approx(expected, abs=1 / 60)  # within the solve error


def test_without_an_emulated_telescope_the_recorded_points_are_used():
    mount, camera, sent, _ = coupled(model="slt")
    mount.disable()
    ra, dec = solve(camera, sent)
    assert dec == pytest.approx(ALIGNMENT_POINTS[0].dec, abs=1e-4)
