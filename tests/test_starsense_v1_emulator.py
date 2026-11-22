"""The emulated StarSense AutoAlign (V1) camera: a real sky, answered with a real camera's timing."""

import io
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
    seen = star_catalog.in_view(*RECORDED_SKY)
    assert len(seen) == 63  # Hipparcos stars to magnitude 9 in the field, as the solver found
    assert matched(real, seen) >= 57
    for got, want in zip(seen[:3], real[:3]):
        assert abs(got.x - want.x) < 1.5 and abs(got.y - want.y) < 1.5
    assert [star.hip for star in seen[:2]] == [51233, 51658]


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
