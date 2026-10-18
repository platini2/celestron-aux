import io

import pytest

from celestron_aux.app import AppConfig, ScannerApp
from celestron_aux.protocol import AuxPacket, FrameParser
from celestron_aux.protocol.units import degrees_to_bam

from .fakes import FakeMount, wait_until

GPS_REPLIES = {
    (0xB0, 0x37): b"\x01",                      # linked
    (0xB0, 0x36): b"\x01",                      # time valid
    (0xB0, 0x01): degrees_to_bam(-34.6037),
    (0xB0, 0x02): degrees_to_bam(-58.3816),
    (0xB0, 0x04): (2026).to_bytes(2, "big"),
    (0xB0, 0x03): bytes((10, 17)),
    (0xB0, 0x33): bytes((21, 4, 5)),
    (0xB0, 0x07): bytes((9, 7)),
}
FOCUSER_REPLIES = {
    (0x12, 0x01): (100000).to_bytes(3, "big"),
    (0x12, 0x13): b"\xff",
    (0x12, 0x2C): (1000).to_bytes(4, "big") + (60000).to_bytes(4, "big"),
}


def start(**config):
    mount = FakeMount({0xB0: b"\x0b\x01", 0x12: b"\x07\x03"}, replies={**GPS_REPLIES, **FOCUSER_REPLIES})
    app = ScannerApp(mount, AppConfig(save_starsense_images=False, **config), stream=io.StringIO())
    app.bus._inter_frame_delay = 0
    app.start(initial_scan=False)
    return app, mount


def commands_sent(mount, device):
    parser = FrameParser()
    return [p.frame.command for chunk in mount.written for p in parser.feed(chunk) if p.frame.destination == device]


def test_monitors_poll_nothing_while_off():
    app, mount = start()
    try:
        assert not wait_until(lambda: mount.written, timeout=1.5)
        assert app.gps_monitor.snapshot()["enabled"] is False
    finally:
        app.close()


def test_gps_monitor_polls_and_decodes_its_status():
    app, mount = start(gps_polling=True)
    try:
        assert wait_until(lambda: app.gps_monitor.snapshot()["satellites"] == [9, 7])
        status = app.gps_monitor.snapshot()
        assert status["linked"] is True and status["time_valid"] is True
        assert status["latitude"]["degrees"] == pytest.approx(-34.6037, abs=1e-4)
        assert status["longitude"]["degrees"] == pytest.approx(-58.3816, abs=1e-4)
        assert status["utc"] == "2026-10-17 21:04:05"
        assert sorted(commands_sent(mount, 0xB0)) == sorted([0x37, 0x36, 0x01, 0x02, 0x04, 0x03, 0x33, 0x07])
        assert commands_sent(mount, 0x12) == []  # the focuser stays quiet
    finally:
        app.close()


def test_focuser_monitor_polls_position_and_reads_limits_once():
    app, mount = start(focuser_polling=True)
    try:
        assert wait_until(lambda: app.focuser.snapshot()["limits"] == [1000, 60000])
        assert wait_until(lambda: commands_sent(mount, 0x12).count(0x01) >= 2, timeout=4)
        status = app.focuser.snapshot()
        assert status["position"] == 100000 and status["moving"] is False
        assert commands_sent(mount, 0x12).count(0x2C) == 1
    finally:
        app.close()


def test_replies_to_the_hand_controller_are_picked_up_while_off():
    app, mount = start(listen_only=True)
    try:
        mount.inject(AuxPacket(0x12, 0x0D, 0x01, (4242).to_bytes(3, "big")).encode())
        mount.inject(AuxPacket(0xB0, 0x0D, 0x37, b"\x00").encode())
        assert wait_until(lambda: app.focuser.snapshot()["position"] == 4242)
        assert wait_until(lambda: app.gps_monitor.snapshot()["linked"] is False)
        assert mount.written == []
    finally:
        app.close()
