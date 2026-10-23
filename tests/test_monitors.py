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


# --- focuser control ----------------------------------------------------------------

def focus_moves(mount):
    parser = FrameParser()
    return [(p.frame.command, p.frame.data) for chunk in mount.written for p in parser.feed(chunk)
            if p.frame.destination == 0x12 and p.frame.command in (0x02, 0x24, 0x25)]


def test_focuser_moves_once_per_hold_and_stops():
    app, mount = start(focuser_polling=True)
    try:
        app.focuser.move(1, 5)
        app.focuser.move(1, 5)   # held: only the deadline is refreshed
        app.focuser.move(-1, 3)  # other way: sent
        assert focus_moves(mount) == [(0x24, b"\x05"), (0x25, b"\x03")]
        assert app.focuser.snapshot()["direction"] == -1
        app.focuser.stop()
        assert focus_moves(mount)[-1] == (0x24, b"\x00") and not app.focuser.moving_by_hand
    finally:
        app.close()


def test_focuser_dead_man_stops_an_unconfirmed_move():
    app, mount = start(focuser_polling=True)
    app.focuser.deadman = 0.3
    try:
        app.focuser.move(1, 9)
        assert wait_until(lambda: (0x24, b"\x00") in focus_moves(mount), timeout=2)
    finally:
        app.close()


def test_focuser_stops_when_switched_off_on_listen_only_and_on_close():
    for finish in ("switch off", "listen only", "close"):
        app, mount = start(focuser_polling=True)
        try:
            app.focuser.move(1, 4)
            if finish == "switch off":
                app.focuser.enabled = False
            elif finish == "listen only":
                app.listen_only = True
            else:
                app.close()
            assert focus_moves(mount)[-1] == (0x24, b"\x00"), finish
        finally:
            app.close()


def test_focuser_control_is_refused_while_off():
    from celestron_aux.motion import MotionRefused

    app, mount = start()
    try:
        with pytest.raises(MotionRefused):
            app.focuser.move(1, 5)
        with pytest.raises(MotionRefused):
            app.focuser.goto(30000)
        assert focus_moves(mount) == []
    finally:
        app.close()


def test_focuser_goto_respects_the_travel_limits():
    app, mount = start(focuser_polling=True)
    try:
        assert wait_until(lambda: app.focuser.snapshot()["limits"] == [1000, 60000])
        with pytest.raises(ValueError, match="1000 to 60000"):
            app.focuser.goto(70000)
        app.focuser.goto(30000)
        assert focus_moves(mount) == [(0x02, (30000).to_bytes(3, "big"))]
    finally:
        app.close()


# --- dew heater box and plate solves, against the emulators on a local bus ------------------

def local_app(**config):
    from celestron_aux.transport.local import LocalBusTransport

    app = ScannerApp(LocalBusTransport(), AppConfig(save_starsense_images=False, **config), stream=io.StringIO())
    app.bus._inter_frame_delay = 0
    app.start(initial_scan=False)
    return app


def test_dew_box_reads_the_controller_and_switches_its_outputs():
    app = local_app(dew_polling=True)
    try:
        app.enable_emulation("dew")
        assert wait_until(lambda: app.dew.snapshot()["ambient"] == 5.0 and len(app.dew.snapshot()["heaters"][1]) > 1
                          and app.dew.snapshot()["port_12v"] is not None and app.dew.snapshot()["led"] is not None)
        reading = app.dew.snapshot()
        assert (reading["voltage"], reading["humidity"], reading["port_12v"]) == (12.7, 75, True)
        assert reading["heaters"][0]["temperature"] == 7.0 and reading["heaters"][0]["automatic"] is True
        app.dew.set_output(1, False)
        app.dew.set_output(2, False)
        assert wait_until(lambda: app.dew_emulator.ports_enabled == [1, 0, 0])
        assert wait_until(lambda: app.dew.snapshot()["heaters"][1]["on"] is False and app.dew.snapshot()["port_12v"] is False)
        app.dew.set_manual(0, 60)
        assert wait_until(lambda: app.dew.snapshot()["heaters"][0]["automatic"] is False
                          and app.dew.snapshot()["heaters"][0]["power"] == 60)
        app.dew.set_led(32)
        assert wait_until(lambda: app.dew.snapshot()["led"] == 32 and app.dew_emulator.led_brightness == 32)
        with pytest.raises(ValueError):
            app.dew.set_automatic(0, 11)
    finally:
        app.close()


def test_a_plate_solve_from_the_box():
    app = local_app()
    app.starsense_v2.solve_time = 0.3
    try:
        app.enable_emulation("starsense")
        app.plate_solver.solve(0xBD)
        assert app.plate_solver.snapshot()["solving"] is True
        assert wait_until(lambda: "rotation" in app.plate_solver.snapshot() and "telescope_ra" in app.plate_solver.snapshot(),
                          timeout=5)
        result = app.plate_solver.snapshot()
        assert result["status"] == "Solved" and result["solving"] is False and result["calibrated"] is False
        assert result["dec"] == pytest.approx(-3.6622, abs=1e-3)  # the first recorded point, without a mount emulator
        with pytest.raises(ValueError):
            app.plate_solver.solve(0x10)
    finally:
        app.close()
