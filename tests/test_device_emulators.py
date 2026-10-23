import io
import math

import pytest

from celestron_aux.app import AppConfig, ScannerApp
from celestron_aux.bus import BusEvent, Direction
from celestron_aux.decoding import describe_packet
from celestron_aux.device_emulators import DewHeaterEmulator, FocuserEmulator
from celestron_aux.protocol import AuxPacket, FrameParser

from .fakes import FakeMount, wait_until


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def make(cls, **kwargs):
    sent, clock = [], Clock()
    emu = cls(sent.append, clock=clock, **kwargs)
    emu.enable()
    return emu, sent, clock


def ask(emu, sent, address, command, data=b""):
    before = len(sent)
    request = AuxPacket(0x20, address, command, data)
    emu(BusEvent(0.0, Direction.RX, request, request.encode()))
    if len(sent) == before:
        return None
    reply = sent[-1]
    assert (reply.source, reply.destination, reply.command) == (address, 0x20, command)
    return reply.data


def decoded(address, command, payload):
    """How the scanner itself decodes an emulator reply."""
    description = describe_packet(AuxPacket(address, 0x20, command, payload))
    assert not description.malformed
    return description.value


# --- focuser ---------------------------------------------------------------------------

def position(emu, sent):
    return int.from_bytes(ask(emu, sent, 0x12, 0x01), "big")


def test_focuser_reports_its_version_position_and_limits():
    emu, sent, _ = make(FocuserEmulator)
    assert decoded(0x12, 0xFE, ask(emu, sent, 0x12, 0xFE)) == "7.16.9300"
    assert position(emu, sent) == 30000
    low, high = FocuserEmulator.LIMITS
    assert ask(emu, sent, 0x12, 0x2C) == low.to_bytes(4, "big") + high.to_bytes(4, "big")
    assert ask(emu, sent, 0x12, 0x13) == b"\xff"  # not moving


def test_focuser_goto_moves_over_time_and_stops_on_target():
    emu, sent, clock = make(FocuserEmulator)
    speed = FocuserEmulator.SPEEDS[9]
    assert ask(emu, sent, 0x12, 0x02, (31500).to_bytes(3, "big")) == b"\x01"  # ACK, as motors answer
    clock.t = 1.0
    assert position(emu, sent) == 30000 + speed and ask(emu, sent, 0x12, 0x13) == b"\x00"
    clock.t = 5.0
    assert position(emu, sent) == 31500 and ask(emu, sent, 0x12, 0x13) == b"\xff"


def test_focuser_moves_at_a_rate_until_stopped_and_never_past_its_limits():
    emu, sent, clock = make(FocuserEmulator)
    ask(emu, sent, 0x12, 0x24, b"\x05")
    clock.t = 2.0
    assert position(emu, sent) == 30000 + 2 * FocuserEmulator.SPEEDS[5]
    ask(emu, sent, 0x12, 0x24, b"\x00")  # rate 0 stops
    clock.t = 10.0
    assert position(emu, sent) == 30000 + 2 * FocuserEmulator.SPEEDS[5]
    ask(emu, sent, 0x12, 0x25, b"\x09")
    clock.t = 1000.0
    assert position(emu, sent) == FocuserEmulator.LIMITS[0] and ask(emu, sent, 0x12, 0x13) == b"\xff"
    ask(emu, sent, 0x12, 0x02, (99999).to_bytes(3, "big"))  # a go-to past the limit stops at it
    clock.t = 2000.0
    assert position(emu, sent) == FocuserEmulator.LIMITS[1]


def test_focuser_calibration_steps_through_its_states():
    emu, sent, clock = make(FocuserEmulator)
    assert ask(emu, sent, 0x12, 0x2B) == b"\x01\x00"  # calibrated, idle
    ask(emu, sent, 0x12, 0x2A, b"\x01")               # start
    clock.t = FocuserEmulator.CALIBRATION_TIME / 2
    done, state = ask(emu, sent, 0x12, 0x2B)
    assert done == 0 and 1 <= state < FocuserEmulator.CALIBRATION_STEPS
    clock.t = FocuserEmulator.CALIBRATION_TIME
    assert ask(emu, sent, 0x12, 0x2B) == bytes((1, FocuserEmulator.CALIBRATION_STEPS))
    ask(emu, sent, 0x12, 0x2A, b"\x01")
    ask(emu, sent, 0x12, 0x2A, b"\x00")               # abort
    assert ask(emu, sent, 0x12, 0x2B) == b"\x00\x00"  # not calibrated, idle


def start_app(**config):
    mount = FakeMount({})
    app = ScannerApp(mount, AppConfig(save_starsense_images=False, **config), stream=io.StringIO())
    app.bus._inter_frame_delay = 0
    app.start(initial_scan=False)
    return app, mount


def test_the_focuser_panel_controls_the_emulated_focuser():
    app, _ = start_app(focuser_polling=True)
    try:
        app.enable_emulation("focuser")
        assert (0x12, "7.16.9300 (emulated)") in app.devices()
        assert wait_until(lambda: app.focuser.snapshot()["position"] == 30000)
        assert wait_until(lambda: app.focuser.snapshot()["limits"] == list(FocuserEmulator.LIMITS))
        app.focuser.goto(30500)
        assert wait_until(lambda: app.focuser.snapshot()["position"] == 30500, timeout=5)
        assert app.focuser.snapshot()["moving"] is False
    finally:
        app.close()


# --- dew heater controller ---------------------------------------------------------------

def test_dew_controller_answers_at_both_addresses():
    emu, sent, _ = make(DewHeaterEmulator)
    assert decoded(0x17, 0xFE, ask(emu, sent, 0x17, 0xFE)) == "1.0"
    assert decoded(0xBB, 0xFE, ask(emu, sent, 0xBB, 0xFE)) == "1.1.1270"
    assert ask(emu, sent, 0xBB, 0x05) == b"\x01\x07"  # the model


def test_dew_controller_replies_have_the_expected_layouts_and_decode():
    emu, sent, _ = make(DewHeaterEmulator)
    power = ask(emu, sent, 0x17, 0x00)
    assert len(power) == 6 and decoded(0x17, 0x00, power).startswith("12.7 V - ")  # the default input
    assert decoded(0x17, 0x04, ask(emu, sent, 0x17, 0x04)) == "5 A - 10 A"
    assert ask(emu, sent, 0x17, 0x10) == b"\x03"  # two heaters and one 12 V port
    heater = ask(emu, sent, 0x17, 0x12, b"\x00")
    assert len(heater) == 10 and decoded(0x17, 0x12, heater).startswith("Port 1 - Auto - Power: ")
    env = ask(emu, sent, 0x17, 0x18)
    temperature, dew_point = (int.from_bytes(env[i:i + 4], "big", signed=True) / 1000 for i in (0, 4))
    a, b = 17.62, 243.12  # Magnus formula, restated, for the defaults: 5 C and 75 %
    gamma = math.log(0.75) + a * 5.0 / (b + 5.0)
    assert temperature == 5.0 and dew_point == pytest.approx(b * gamma / (a - gamma), abs=0.001) and env[8] == 75
    assert ask(emu, sent, 0x17, 0x21) == b"\xff"


def test_dew_controller_settings_change_what_it_reports():
    emu, sent, clock = make(DewHeaterEmulator)
    ask(emu, sent, 0x17, 0x17, b"\x01\x80")  # heater 2 manual at 0x80/255
    heater = ask(emu, sent, 0x17, 0x12, b"\x01")
    percent = int(0x80 / 2.55 + 0.5)                         # manual power is kept as a percentage
    assert (heater[1], heater[2], heater[5]) == (0, int(percent * 2.55 + 0.5), 0)  # manual, power byte, aggression 0
    assert decoded(0x17, 0x12, heater).startswith("Port 2 - Manual - Power: On ")
    ask(emu, sent, 0x17, 0x16, b"\x00\x07")  # heater 1 automatic, aggression 7
    assert ask(emu, sent, 0x17, 0x12, b"\x00")[5] == 7
    ask(emu, sent, 0x17, 0x20, b"\x40")
    assert ask(emu, sent, 0x17, 0x02) == b"\x40"
    ask(emu, sent, 0x17, 0x03, (3500).to_bytes(2, "big"))
    assert decoded(0x17, 0x04, ask(emu, sent, 0x17, 0x04)) == "3.5 A - 10 A"
    ask(emu, sent, 0x17, 0x19, b"\x01")      # recalibrate the sensor
    assert ask(emu, sent, 0x17, 0x1A) == b"\x01"
    clock.t = DewHeaterEmulator.RECALIBRATION_TIME
    assert ask(emu, sent, 0x17, 0x1A) == b"\x00"


def test_dew_controller_port_numbering():
    emu, sent, _ = make(DewHeaterEmulator)
    assert ask(emu, sent, 0x17, 0x11, b"\x00") == b"\x01"  # one 12 V port
    assert ask(emu, sent, 0x17, 0x11, b"\x01") == b"\x02"  # two heaters
    assert ask(emu, sent, 0x17, 0x11, b"\xff") == b"\x00"  # USB hub
    on = bytes((0x11, 1, 0)) + (0).to_bytes(2, "big") + (12700).to_bytes(2, "big")  # on, passing 12.7 V through
    assert ask(emu, sent, 0x17, 0x11, b"\x02") == on                                # on by default
    ask(emu, sent, 0x17, 0x14, b"\x02\x00")                                         # 12 V output off
    assert ask(emu, sent, 0x17, 0x11, b"\x02") == bytes((0x11, 0, 0)) + bytes(4)
    ask(emu, sent, 0x17, 0x14, b"\x02\x01")                                         # and on again
    assert ask(emu, sent, 0x17, 0x11, b"\x02") == on


def test_dew_heaters_are_on_by_default_and_remember_being_switched_off():
    emu, sent, _ = make(DewHeaterEmulator)
    assert [h["on"] for h in emu.snapshot()["heaters"]] == [True, True] and emu.snapshot()["port_12v"] is True
    ask(emu, sent, 0x17, 0x17, b"\x01\x80")       # heater 2: manual, about 50 %
    assert ask(emu, sent, 0x17, 0x12, b"\x01")[2] > 0
    ask(emu, sent, 0x17, 0x14, b"\x01\x00")       # heater 2 off
    heater = ask(emu, sent, 0x17, 0x12, b"\x01")
    assert heater[2] == 0 and heater[3:5] == bytes(2)  # no power, no current
    assert [h["on"] for h in emu.snapshot()["heaters"]] == [True, False]
    ask(emu, sent, 0x17, 0x14, b"\x01\x01")       # back on: its manual setting returns
    assert ask(emu, sent, 0x17, 0x12, b"\x01")[2] > 0


def test_dew_conditions_can_be_set_and_are_reported():
    emu, sent, _ = make(DewHeaterEmulator)
    emu.set_conditions(ambient=-2.5, humidity=90, voltage=11.4, heater_temperatures=[3.0, 12.5])
    env = ask(emu, sent, 0x17, 0x18)
    assert int.from_bytes(env[0:4], "big", signed=True) == -2500 and env[8] == 90  # negative temperatures too
    assert decoded(0x17, 0x00, ask(emu, sent, 0x17, 0x00)).startswith("11.4 V - ")
    temps = [int.from_bytes(ask(emu, sent, 0x17, 0x12, bytes((c,)))[6:10], "big", signed=True) / 1000 for c in (0, 1)]
    assert temps == [3.0, 12.5]
    assert emu.snapshot()["dew_point"] < -2.5 + 0.1  # at 90 % the dew point is just under the air temperature
    for bad in ({"voltage": 10.9}, {"voltage": 13.1}, {"humidity": 0}, {"ambient": 60}, {"heater_temperatures": [1.0]}):
        with pytest.raises(ValueError):
            emu.set_conditions(**bad)


def test_the_led_brightness_set_by_the_controller_is_kept():
    emu, sent, _ = make(DewHeaterEmulator)
    assert emu.snapshot()["led_brightness"] == 128
    ask(emu, sent, 0x17, 0x20, b"\x20")
    assert emu.snapshot()["led_brightness"] == 0x20 and ask(emu, sent, 0x17, 0x02) == b"\x20"


def test_emulated_devices_stay_labelled_after_a_scan():
    app, _ = start_app()
    app.scanner._reply_wait = app.scanner._mount_wait = 0.05
    try:
        app.enable_emulation("dew")
        app.start_scan()
        assert wait_until(lambda: not app.scanning and app.registry.contains(0x17), timeout=15)
        assert (0x17, "1.0 (emulated)") in app.devices() and (0xBB, "1.1.1270 (emulated)") in app.devices()
    finally:
        app.close()


def test_the_emulated_gps_stays_labelled_after_a_scan_and_leaves_when_switched_off():
    app, _ = start_app()
    app.scanner._reply_wait = app.scanner._mount_wait = 0.05
    try:
        app.enable_gps_emulation(51.4779, -0.0015)
        app.start_scan()
        assert wait_until(lambda: not app.scanning and app.registry.contains(0xB0), timeout=15)
        assert (0xB0, "11.1 (emulated)") in app.devices()
        app.disable_gps_emulation()
        assert not app.registry.contains(0xB0)
    finally:
        app.close()


def test_emulators_refuse_to_start_next_to_a_real_device_and_stay_silent_when_off():
    app, mount = start_app()
    try:
        mount.inject(AuxPacket(0xBB, 0x2F, 0xFE, bytes.fromhex("010104f6")).encode())  # a real dew controller
        assert wait_until(lambda: app.registry.contains(0xBB))
        with pytest.raises(ValueError, match=r"\(0xbb\) already answered"):
            app.enable_emulation("dew")
        app.enable_emulation("focuser")
        app.disable_emulation("focuser")
        assert not app.registry.contains(0x12)
        mount.written.clear()
        mount.inject(AuxPacket(0x20, 0x12, 0xFE).encode())
        assert not wait_until(lambda: mount.written, timeout=0.5)
    finally:
        app.close()


# --- changing an emulator's firmware version ------------------------------------------------

@pytest.mark.parametrize("text,data", [
    ("11.1", bytes((11, 1))), ("7.16.9300", bytes((7, 16)) + (9300).to_bytes(2, "big")),
    (" 35.12.5 ", bytes((35, 12)) + (5).to_bytes(2, "big")), ("1.1.1270", bytes((1, 1)) + (1270).to_bytes(2, "big")),
])
def test_versions_are_parsed_back_into_reply_bytes(text, data):
    from celestron_aux.protocol.units import format_version, parse_version

    assert parse_version(text) == data and format_version(data) == text.strip()


@pytest.mark.parametrize("text", ["", "7", "7.x", "256.1", "1.2.70000", "1.2.3.4", "-1.2"])
def test_invalid_versions_are_refused(text):
    from celestron_aux.protocol.units import parse_version

    with pytest.raises(ValueError):
        parse_version(text)


def test_an_emulators_version_can_be_changed_while_it_runs():
    app, mount = start_app()
    try:
        app.enable_emulation("focuser")
        assert app.set_emulated_version("focuser", 0x12, "7.20.100") == "7.20.100"
        assert (0x12, "7.20.100 (emulated)") in app.devices()  # the device list follows
        mount.inject(AuxPacket(0x20, 0x12, 0xFE).encode())  # CPWI asks
        replies = lambda: [p.frame for chunk in mount.written for p in FrameParser().feed(chunk)]
        assert wait_until(lambda: any(r.source == 0x12 and r.data == bytes((7, 20)) + (100).to_bytes(2, "big")
                                      for r in replies()))
        app.enable_gps_emulation(51.4779, -0.0015)
        app.set_emulated_version("gps", 0xB0, "12.3")
        assert (0xB0, "12.3 (emulated)") in app.devices()
        with pytest.raises(ValueError):
            app.set_emulated_version("focuser", 0x17, "1.0")  # not the focuser's address
    finally:
        app.close()


def test_starsense_v1_answers_discovery_only():
    from celestron_aux.device_emulators import StarSenseV1Emulator

    emu, sent, _ = make(StarSenseV1Emulator)
    assert decoded(0xB4, 0xFE, ask(emu, sent, 0xB4, 0xFE)) == "1.2.13343"  # the version a real camera reported
    assert ask(emu, sent, 0xB4, 0x90, bytes.fromhex("03e800")) is None    # a capture: not emulated


def test_starsense_v1_is_found_by_a_scan_and_labelled():
    app, _ = start_app()
    app.scanner._reply_wait = app.scanner._mount_wait = 0.05
    try:
        app.enable_emulation("starsense1")
        app.start_scan()
        assert wait_until(lambda: not app.scanning and app.registry.contains(0xB4), timeout=15)
        assert (0xB4, "1.2.13343 (emulated)") in app.devices()
    finally:
        app.close()
