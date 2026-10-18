import io

import pytest

from celestron_aux.app import AppConfig, ScannerApp
from celestron_aux.motion import Axis, MotionRefused
from celestron_aux.protocol import AuxPacket, FrameParser

from .fakes import FakeMount, wait_until

AZM_90 = bytes.fromhex("400000")   # a quarter turn
ALT_M45 = bytes.fromhex("e00000")  # -45 degrees


def start(**config):
    config.setdefault("motion_enabled", True)
    mount = FakeMount({0x10: b"\x07\x0b", 0x11: b"\x07\x0b"}, positions={0x10: AZM_90, 0x11: ALT_M45})
    app = ScannerApp(mount, AppConfig(save_starsense_images=False, **config), stream=io.StringIO())
    app.bus._inter_frame_delay = 0
    app.start(initial_scan=False)
    return app, mount


def sent(mount):
    """Every packet the app transmitted, decoded."""
    parser = FrameParser()
    return [p.frame for chunk in mount.written for p in parser.feed(chunk)]


def slews(mount):
    return [(p.destination, p.command, p.data) for p in sent(mount) if p.command in (0x24, 0x25)]


def test_positions_are_polled_and_decoded():
    app, mount = start()
    try:
        assert wait_until(lambda: app.motion.snapshot()["axes"]["alt"]["dms"] is not None)
        axes = app.motion.snapshot()["axes"]
        assert axes["azm"]["degrees"] == pytest.approx(90.0) and axes["alt"]["degrees"] == pytest.approx(-45.0)
        assert axes["azm"]["dms"] == "90\N{DEGREE SIGN}00'00.00\""
    finally:
        app.close()


def test_positions_asked_for_by_the_hand_controller_are_picked_up_while_off():
    app, mount = start(motion_enabled=False, listen_only=True)
    try:
        mount.inject(AuxPacket(0x10, 0x0D, 0x01, AZM_90).encode())  # the HC's own question, answered
        assert wait_until(lambda: app.motion.snapshot()["axes"]["azm"]["degrees"] is not None)
        assert mount.written == []  # nothing was transmitted
    finally:
        app.close()


def test_move_sends_once_then_only_refreshes_and_resends_on_change():
    app, mount = start()
    try:
        app.motion.move(Axis.AZM, 1, 5)
        app.motion.move(Axis.AZM, 1, 5)  # held: only the deadline is refreshed
        app.motion.move(Axis.AZM, 1, 7)  # faster: sent again
        app.motion.move(Axis.ALT, -1, 3)
        assert slews(mount) == [(0x10, 0x24, b"\x05"), (0x10, 0x24, b"\x07"), (0x11, 0x25, b"\x03")]
        app.motion.stop()
        assert slews(mount)[-2:] == [(0x10, 0x24, b"\x00"), (0x11, 0x24, b"\x00")]
        assert not app.motion.moving
    finally:
        app.close()


def test_a_move_that_is_not_confirmed_is_stopped_by_the_dead_man_check():
    app, mount = start()
    app.motion.deadman = 0.3
    try:
        app.motion.move(Axis.ALT, 1, 9)
        assert wait_until(lambda: (0x11, 0x24, b"\x00") in slews(mount), timeout=2)
        assert not app.motion.moving
    finally:
        app.close()


def test_closing_the_app_stops_a_moving_axis():
    app, mount = start()
    app.motion.move(Axis.AZM, -1, 4)
    app.close()
    assert slews(mount)[-2:] == [(0x10, 0x24, b"\x00"), (0x11, 0x24, b"\x00")]


def test_turning_on_listen_only_stops_motion_first():
    app, mount = start()
    try:
        app.motion.move(Axis.AZM, 1, 5)
        app.listen_only = True
        assert slews(mount)[-2:] == [(0x10, 0x24, b"\x00"), (0x11, 0x24, b"\x00")]
        with pytest.raises(MotionRefused):
            app.motion.move(Axis.AZM, 1, 5)
    finally:
        app.close()


def test_invalid_moves_are_rejected():
    app, _ = start()
    try:
        with pytest.raises(ValueError):
            app.motion.move(Axis.AZM, 0, 5)
        with pytest.raises(ValueError):
            app.motion.move(Axis.AZM, 1, 10)
    finally:
        app.close()


def test_mount_control_off_means_no_polling_and_no_moves():
    app, mount = start(motion_enabled=False)
    try:
        with pytest.raises(MotionRefused):
            app.motion.move(Axis.AZM, 1, 5)
        assert not wait_until(lambda: mount.written, timeout=1.5)  # nothing polled, nothing sent
        app.motion.enabled = True
        assert wait_until(lambda: app.motion.snapshot()["axes"]["azm"]["degrees"] is not None)
    finally:
        app.close()


def test_switching_mount_control_off_stops_motion_first():
    app, mount = start()
    try:
        app.motion.move(Axis.ALT, 1, 6)
        app.motion.enabled = False
        assert slews(mount)[-2:] == [(0x10, 0x24, b"\x00"), (0x11, 0x24, b"\x00")]
        assert not app.motion.moving
    finally:
        app.close()
