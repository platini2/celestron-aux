import io
import time
from datetime import datetime, timezone

import pytest

from celestron_aux._sample_data import SAMPLE_STAR_LIST
from celestron_aux.app import AppConfig, ScannerApp
from celestron_aux.bus import BusEvent, Direction
from celestron_aux.gps_emulator import GpsEmulator
from celestron_aux.output import CsvLogger
from celestron_aux.protocol import AuxPacket, StarSenseFrame
from celestron_aux.protocol.units import bam_to_degrees
from celestron_aux.registry import DeviceRegistry
from celestron_aux.starsense import parse_star_list, render_star_image

from .fakes import FakeMount, wait_until


def event(frame):
    return BusEvent(1.0, Direction.RX, frame, frame.encode())


def test_star_list_decodes_to_sensor_coordinates():
    stars = parse_star_list(SAMPLE_STAR_LIST)
    assert len(stars) == 99
    assert all(0 <= s.x < 1280 and 0 <= s.y < 960 for s in stars)


def test_star_image_has_sensor_size(tmp_path):
    image = render_star_image(parse_star_list(SAMPLE_STAR_LIST))
    assert image.size == (1280, 960)
    assert image.getbbox() is not None


def test_registry_learns_devices_and_mount():
    registry = DeviceRegistry()
    registry(event(AuxPacket(0x10, 0x2F, 0xFE, b"\x07\x0b")))
    registry(event(AuxPacket(0x10, 0x2F, 0x05, b"\x14\x85")))
    registry(event(AuxPacket(0x2F, 0x11, 0xFE)))  # request, no data: ignored
    assert registry.devices() == [(0x10, "7.11")]
    assert registry.mount_model == 0x1485


def test_gps_emulator_answers_only_when_enabled():
    sent = []
    fixed = datetime(2026, 10, 17, 21, 4, 5, tzinfo=timezone.utc)
    gps = GpsEmulator(sent.append, now=lambda: fixed)
    request = AuxPacket(0x0D, 0xB0, 0x33)
    gps(event(request))
    assert sent == []
    gps.enable(-34.6037, -58.3816)
    gps(event(request))
    gps(event(AuxPacket(0x0D, 0xB0, 0x04)))
    gps(event(AuxPacket(0x0D, 0xB0, 0x01)))
    gps(event(AuxPacket(0xB0, 0x0D, 0x01, b"\x00\x00\x00")))  # our own reply: ignored
    assert [p.data for p in sent[:2]] == [bytes((21, 4, 5)), (2026).to_bytes(2, "big")]
    assert all(p.source == 0xB0 and p.destination == 0x0D for p in sent)
    assert bam_to_degrees(sent[2].data) == pytest.approx(-34.6037, abs=1e-4)
    assert len(sent) == 3


def test_csv_rows_for_each_frame_type(tmp_path):
    logger = CsvLogger()
    path = tmp_path / "log.csv"
    logger.start(path)
    logger(event(AuxPacket(0x10, 0x2F, 0xFE, b"\x07\x0b")))
    logger(event(StarSenseFrame(SAMPLE_STAR_LIST)))
    logger.stop()
    lines = path.read_text().splitlines()
    assert lines[0].startswith("timestamp,sender")
    assert ",AZM MC,0x10,AUXBUS Scanner,0x2f,MTR_GET_VERSION,0xfe,7.11," in lines[1]
    assert "99 stars" in lines[2]


def test_end_to_end_scan_against_a_simulated_mount(tmp_path):
    mount = FakeMount({0x10: b"\x07\x0b", 0x11: b"\x07\x0b", 0x0D: bytes((5, 32, 0, 1))}, model=b"\x16\x87")
    out = io.StringIO()
    app = ScannerApp(mount, AppConfig(image_dir=tmp_path, raw_path=tmp_path / "raw.txt",
                                      record_raw=True), stream=out)
    app.bus._inter_frame_delay = 0  # speed the test up
    app.scanner._reply_wait = app.scanner._mount_wait = 0.1
    app.start(initial_scan=True)
    try:
        assert wait_until(lambda: "Finished AUXBUS scan" in out.getvalue()
                          and "Detected devices" in out.getvalue().split("Finished AUXBUS scan")[-1])
        assert {address for address, _ in app.devices()} == {0x0D, 0x10, 0x11}
        assert app.registry.mount_model == 0x1687
        text = out.getvalue()
        assert "Mount: Nexstar Evolution" in text
        assert "MTR_GET_VERSION" in text and "7.11" in text

        app.send_sample_star_list()
        assert wait_until(lambda: list(tmp_path.glob("starsense-*.png")))
    finally:
        app.close()
    raw_lines = (tmp_path / "raw.txt").read_text().splitlines()
    assert raw_lines and all(len(line.split()) == 2 for line in raw_lines)


def test_playback_replays_a_recording(tmp_path):
    from celestron_aux.transport.playback import PlaybackTransport

    capture = tmp_path / "capture.txt"
    reply = AuxPacket(0x10, 0x2F, 0xFE, b"\x07\x0b").encode()
    capture.write_text(f"0.0 {reply[:3].hex()}\n0.05 {reply[3:].hex()}\n")
    out = io.StringIO()
    app = ScannerApp(PlaybackTransport(capture), AppConfig(save_starsense_images=False), stream=out)
    app.start(initial_scan=False)
    try:
        assert wait_until(lambda: app.bus.closed.is_set())
        assert wait_until(lambda: app.devices() == [(0x10, "7.11")])
    finally:
        app.close()


def test_listen_only_blocks_every_transmission():
    mount = FakeMount({0x10: b"\x07\x0b"})
    app = ScannerApp(mount, AppConfig(listen_only=True, save_starsense_images=False), stream=io.StringIO())
    app.bus._inter_frame_delay = 0
    app.start(initial_scan=True)  # the initial scan is skipped
    try:
        app.gps.enable(-34.6, -58.4)
        mount.inject(AuxPacket(0x0D, 0xB0, 0x33).encode())  # a GPS question the emulator would answer
        assert app.send(AuxPacket(0x2F, 0x10, 0xFE)) is False
        assert app.send_sample_star_list() is False
        time.sleep(0.5)  # give the GPS emulator time to (not) answer
        assert mount.written == []
        app.listen_only = False
        assert app.send(AuxPacket(0x2F, 0x10, 0xFE)) is True
        assert len(mount.written) == 1
    finally:
        app.close()


def test_unknown_traffic_is_collected_by_the_app():
    mount = FakeMount({})
    app = ScannerApp(mount, AppConfig(save_starsense_images=False), stream=io.StringIO())
    app.start(initial_scan=False)
    try:
        mount.inject(AuxPacket(0x0D, 0x10, 0x77, b"\x01").encode())
        assert wait_until(lambda: len(app.discovery) == 1)
    finally:
        app.close()
