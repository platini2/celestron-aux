"""The emulated StarSense AutoAlign (V1) camera: a real sky, answered with a real camera's timing."""

import io

import pytest
import math
import random
import struct
from pathlib import Path

from celestron_aux import star_catalog
from celestron_aux.app import AppConfig, ScannerApp
from celestron_aux.bus import BusEvent, Direction
from celestron_aux.protocol import AuxPacket, FrameParser
from celestron_aux.starsense import STAR_RECORD, parse_star_list
from celestron_aux.starsense_v1_emulator import RECORDED_SKY, StarSenseV1Emulator, limiting_magnitude

from .fakes import FakeMount, wait_until

FIXTURE = Path(__file__).parent / "fixtures" / "starsense_frame.hex"
FAST = 0.01  # emulated seconds per real second


def recorded_points():
    frame = FrameParser().feed(bytes.fromhex(FIXTURE.read_text().strip()))[0].frame
    return parse_star_list(frame.payload)


def matched(points, others, tolerance=3.0):
    return sum(1 for p in points if any((p.x - q.x) ** 2 + (p.y - q.y) ** 2 < tolerance ** 2 for q in others))


class Camera:
    """An emulator with its replies and star lists collected, as a client would see them."""

    def __init__(self, **kwargs):
        self.replies, self.frames = [], []
        self.emu = StarSenseV1Emulator(self.replies.append, lambda frame, reply: self.frames.append(frame),
                                       rng=random.Random(1), time_scale=FAST, **kwargs)
        self.emu.enable()

    def ask(self, command, data=b""):
        request = AuxPacket(0x20, 0xB4, command, data)
        before = len(self.replies)
        self.emu(BusEvent(0.0, Direction.RX, request, request.encode()))
        return self.replies[before:]

    def wait_reply(self, command, count=1):
        assert wait_until(lambda: sum(r.command == command for r in self.replies) >= count, timeout=5)
        return [r for r in self.replies if r.command == command][count - 1].data


def test_the_catalogue_reproduces_a_real_frame():
    # The recorded frame from a real camera, solved by the reporter of these measurements:
    # its brightest points are the catalogue's, in the same order, to the pixel.
    real = recorded_points()
    seen = star_catalog.in_view(*RECORDED_SKY, year=2025.0)
    assert len(seen) == 63  # Hipparcos stars to magnitude 9 in the field, as the solver found
    assert matched(real, seen) >= 57
    for got, want in zip(seen[:3], real[:3]):
        assert abs(got.x - want.x) < 1.5 and abs(got.y - want.y) < 1.5
    assert [star.hip for star in seen[:2]] == [51233, 51658]


REAL_LISTS = Path(__file__).parent / "fixtures" / "starsense_real_lists.txt"
# Real points within 2 px of the catalogue as the camera sees it, per list (0.5 px rms or so).
EXPECTED_MATCHES = {"recorded-frame": 58, "sample-alpha-centauri": 64, "near-alpha-centauri": 56,
                    "lepus": 57, "cassiopeia": 80}


def real_lists():
    for line in REAL_LISTS.read_text().splitlines():
        if line and not line.startswith("#"):
            name, ra, dec, rotation, payload = line.split()
            yield name, (float(ra), float(dec), float(rotation)), bytes.fromhex(payload)


@pytest.mark.parametrize("name,sky,payload", list(real_lists()), ids=lambda value: value if isinstance(value, str) else "")
def test_five_real_skies_are_reproduced(name, sky, payload):
    # Each list from a real camera, at the plate a blind solve on Hipparcos found for it:
    # the emulator's sky holds its catalogue stars, and its frame has the same shape.
    real = parse_star_list(payload)
    seen = star_catalog.in_view(*sky, year=2025.0)
    assert matched(real, seen, 2.0) >= EXPECTED_MATCHES[name] - 2
    emulated = StarSenseV1Emulator(lambda packet: None, rng=random.Random(6))
    emulated.sky_source = lambda: sky
    frame = emulated.star_list()
    assert len(frame) == len(payload) == 800 and frame[-8:] == payload[-8:] == bytes(8)


def test_stars_the_camera_cannot_separate_are_one_point():
    # Alpha Centauri A and B, about 1 px apart: one point in the real list, where they are now.
    real = parse_star_list(dict((name, payload) for name, _, payload in real_lists())["sample-alpha-centauri"])
    seen = star_catalog.in_view(220.686, -61.721, 344.86, year=2025.0)
    brightest = seen[0]
    assert brightest.mag < 0  # A and B together
    assert not any(other is not brightest and abs(other.x - brightest.x) < 3 and abs(other.y - brightest.y) < 3
                   for other in seen)
    assert abs(brightest.x - real[0].x) < 1 and abs(brightest.y - real[0].y) < 1


def test_stars_move_with_their_proper_motion():
    alpha_cen = next(star for star in star_catalog.stars() if star.hip == 71683)
    moved = alpha_cen.at(2025.0)
    assert alpha_cen.at(2000.0) == alpha_cen
    assert abs((moved.ra - alpha_cen.ra) * math.cos(math.radians(alpha_cen.dec)) * 3600 / 25 - alpha_cen.pm_ra / 1000) < 0.01
    assert abs((moved.dec - alpha_cen.dec) * 3600 / 25 - alpha_cen.pm_dec / 1000) < 0.01
    assert alpha_cen.pm_ra < -3000  # 3.6"/year west


def test_an_emulated_frame_looks_like_the_real_one():
    real = recorded_points()
    emulated = parse_star_list(StarSenseV1Emulator(lambda packet: None, rng=random.Random(2)).star_list())
    assert len(emulated) == 99 and matched(real, emulated) >= 55


def test_a_full_list_ends_with_a_counted_all_zero_record_like_the_real_one():
    payload = StarSenseV1Emulator(lambda packet: None, rng=random.Random(3)).star_list()
    assert len(payload) == 100 * STAR_RECORD.size and payload[-8:] == bytes(8)


def test_longer_exposures_reach_fainter_stars():
    assert limiting_magnitude(1000) == star_catalog.MAG_LIMIT
    assert limiting_magnitude(100) == star_catalog.MAG_LIMIT - 2.5
    emu = StarSenseV1Emulator(lambda packet: None, rng=random.Random(4))
    short = parse_star_list(emu.star_list(100))
    assert 0 < len(short) < 20  # catalogue stars only, to magnitude 6.5


def test_a_capture_answers_like_a_real_camera():
    camera = Camera()
    assert camera.ask(0x91)[0].data == b"\x01\x00"  # after power-on: idle
    assert camera.ask(0x3F)[0].data == struct.pack("<II", 640, 480)

    assert camera.ask(0x90, bytes.fromhex("03e800")) == []  # answered when the exposure is set
    assert camera.ask(0x91) == []                           # busy: no answer at all
    assert camera.wait_reply(0x90) == b""
    assert camera.ask(0x91)[0].data == b"\x03\x00"

    assert camera.ask(0x94, b"\x00") == []
    assert camera.ask(0xFE) == []
    assert camera.wait_reply(0x94) == bytes((0x09, 100))  # 99 points and the zero record
    assert camera.ask(0x92) == []
    assert camera.wait_reply(0x92) == bytes((0x09, 100))
    assert wait_until(lambda: len(camera.frames) == 1)
    first = camera.frames[0]

    assert camera.ask(0x9F)[0].data == bytes((0x09, 100))  # a guess: what CPWI waits for after the list

    camera.ask(0x92)  # asked again: the same list, no new exposure
    assert wait_until(lambda: len(camera.frames) == 2)
    assert camera.frames[1] == first and camera.emu.snapshot()["captures"] == 1


def test_no_sky_no_stars():
    camera = Camera()
    camera.emu.sky_source = lambda: None  # pointing below the horizon
    camera.ask(0x94, b"\x00")
    assert camera.wait_reply(0x94) == b"\x01\x00"
    camera.ask(0x92)
    assert camera.wait_reply(0x92) == b"\x01\x00"
    assert camera.frames == []


def test_switching_off_cancels_a_capture_in_progress():
    camera = Camera()
    camera.ask(0x94, b"\x00")
    camera.emu.disable()
    camera.emu.enable()
    assert camera.ask(0x91)[0].data == b"\x01\x00"
    assert not wait_until(lambda: any(r.command == 0x94 for r in camera.replies), timeout=0.3)


def start_app():
    app = ScannerApp(FakeMount({}), AppConfig(save_starsense_images=False), stream=io.StringIO())
    app.bus._inter_frame_delay = 0
    app.start(initial_scan=False)
    return app


def test_the_scanners_own_capture_gets_a_list_from_the_emulated_camera():
    app = start_app()
    try:
        app.starsense_v1.time_scale = FAST
        app.enable_emulation("starsense1")
        assert app.starsense_capture.run() == 100
        assert wait_until(lambda: app.starsense_v1.snapshot()["captures"] == 1)
    finally:
        app.close()


def test_the_camera_follows_the_emulated_mount_once_a_location_is_known():
    app = start_app()
    try:
        assert app._starsense_v1_sky() == RECORDED_SKY  # no mount, no location: the recorded sky
        app.enable_emulation("mount")
        app.gps.enable(40.0, -3.0)
        sky = app._starsense_v1_sky()
        assert sky is not None and sky != RECORDED_SKY
        alt, az, _ = app.mount_emulator.sky_pointing()
        assert alt >= 0
    finally:
        app.close()


def test_the_recorded_list_can_answer_captures_unchanged():
    from celestron_aux._sample_data import SAMPLE_STAR_LIST

    camera = Camera()
    camera.emu.recorded_list = True
    camera.ask(0x94, b"\x00")
    assert camera.wait_reply(0x94) == bytes((0x09, len(SAMPLE_STAR_LIST) // 8))
    camera.ask(0x92)
    assert wait_until(lambda: len(camera.frames) == 1)
    assert camera.frames[0].payload == SAMPLE_STAR_LIST
    assert camera.emu.snapshot()["sky"] is None


def test_the_snapshot_says_which_sky_was_photographed():
    camera = Camera()
    camera.ask(0x94, b"\x00")
    camera.wait_reply(0x94)
    sky = camera.emu.snapshot()["sky"]
    assert (sky["ra"], sky["dec"], sky["rotation"]) == (round(RECORDED_SKY[0], 3), round(RECORDED_SKY[1], 3),
                                                        round(RECORDED_SKY[2], 1))


def test_the_last_four_captures_are_kept_with_where_their_sky_came_from():
    camera = Camera()
    count = 0

    def sky():  # as the app's: it says where the sky came from
        camera.emu.sky_details = {"source": "telescope", "alt": float(count)}
        return RECORDED_SKY

    camera.emu.sky_source = sky
    for count in range(1, 6):
        camera.ask(0x94, b"\x00")
        camera.wait_reply(0x94, count)
    kept = camera.emu.captures()
    assert [capture["number"] for capture in kept] == [5, 4, 3, 2]
    newest = kept[0]
    assert newest["sky"]["ra"] == round(RECORDED_SKY[0], 4) and newest["details"]["alt"] == 5.0
    assert len(newest["points"]) == 99
    hipparcos = [point for point in newest["points"] if point[3]]
    assert hipparcos[0][3] == 51233 and 50 < len(hipparcos) < 70


def test_captures_say_when_the_camera_cannot_follow_the_telescope():
    app = start_app()
    try:
        app.enable_emulation("mount")
        app._starsense_v1_sky()
        assert "GPS emulator" in app.starsense_v1.sky_details["reason"]
        app.gps.enable(40.0, -3.0)
        app._starsense_v1_sky()
        details = app.starsense_v1.sky_details
        assert details["source"] == "telescope" and details["location_from"] == "GPS emulator"
    finally:
        app.close()
