import math
from pathlib import Path

import pytest

from celestron_aux import catalog
from celestron_aux.bus import BusEvent, Direction
from celestron_aux.mount_emulator import (MOUNTS, MOUNTS_BY_KEY, NO_WEDGE, OPTIONAL_WEDGE, POLAR, SIDEREAL,
                                          SLEW_FACTOR, SLOW_APPROACH, Frame, MountEmulator, MountSetup, _dot, _vector)
from celestron_aux.protocol import AuxPacket, FrameParser
from celestron_aux.protocol.units import format_version, parse_version

FIXTURE = Path(__file__).parent / "fixtures" / "cpwi_slt_motor_requests.txt"
TURN = 1 << 24


class Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


def make(**setup):
    sent, clock = [], Clock()
    emu = MountEmulator(sent.append, clock=clock)
    emu.configure(MountSetup(**setup))
    emu.enable()
    return emu, sent, clock


def ask(emu, sent, address, command, data=b""):
    before = len(sent)
    request = AuxPacket(0x20, address, command, data)
    emu(BusEvent(0.0, Direction.RX, request, request.encode()))
    if len(sent) == before:
        return None
    return sent[-1].data


def steps(degrees):
    return int(round(degrees / 360 * TURN)).to_bytes(3, "big")


def degrees(position):
    return int.from_bytes(position, "big") / TURN * 360


# --- like a real SLT -------------------------------------------------------------------------

def test_it_answers_cpwi_exactly_like_a_real_slt():
    emu, sent, _ = make(model="slt")
    for line in FIXTURE.read_text().splitlines():
        if line.startswith("#") or not line.strip():
            continue
        request_hex, reply_hex = line.split()
        (parsed,) = FrameParser().feed(bytes.fromhex(request_hex))
        request = parsed.frame
        before = len(sent)
        emu(BusEvent(0.0, Direction.RX, request, request.encode()))
        assert len(sent) == before + 1, request_hex
        assert sent[-1].data.hex() == ("" if reply_hex == "-" else reply_hex), request_hex


def test_every_mount_reports_its_model_and_firmware():
    for spec in MOUNTS:
        emu, sent, _ = make(model=spec.key)
        assert int.from_bytes(ask(emu, sent, 0x10, 0x05), "big") == spec.model_id
        for motor in (0x10, 0x11):
            assert format_version(ask(emu, sent, motor, 0xFE)) == spec.firmware


def test_the_mount_table_agrees_with_the_catalog():
    for spec in MOUNTS:
        assert catalog.MOUNT_MODELS[spec.model_id].lower().replace("nexstar ", "") in spec.name.lower(), spec.key
        assert (spec.kind == "gem") == (spec.wedge == POLAR)
    assert MOUNTS_BY_KEY["slt"].wedge == NO_WEDGE and MOUNTS_BY_KEY["gt"].wedge == NO_WEDGE
    assert {s.key for s in MOUNTS if s.wedge == OPTIONAL_WEDGE} == {"se45", "se68", "cpc", "nexstar-gps",
                                                                    "evolution", "origin"}
    assert MOUNTS_BY_KEY["slt"].rate9 == 3.0  # CPWI's Rate9Default for a real SLT


# --- motion ----------------------------------------------------------------------------------

def test_goto_fast_moves_at_twice_rate_9_and_reports_when_it_is_over():
    emu, sent, clock = make(model="slt")
    speed = MOUNTS_BY_KEY["slt"].rate9 * SLEW_FACTOR  # 3 deg/s, doubled
    assert ask(emu, sent, 0x10, 0x02, steps(30)) == b"\x01"
    clock.t = 2.0
    assert degrees(ask(emu, sent, 0x10, 0x01)) == pytest.approx(2 * speed, abs=1e-3)
    assert ask(emu, sent, 0x10, 0x13) == b"\x00"
    clock.t = 30 / speed + 0.5
    assert degrees(ask(emu, sent, 0x10, 0x01)) == pytest.approx(30.0, abs=1e-3)
    assert ask(emu, sent, 0x10, 0x13) == b"\xff"


def test_goto_takes_the_short_way_round_and_understands_16_bit_positions():
    emu, sent, clock = make(model="evolution")
    ask(emu, sent, 0x10, 0x02, (0xF000).to_bytes(2, "big"))  # 16-bit: 337.5 degrees, i.e. 22.5 the other way
    clock.t = 22.5 / (MOUNTS_BY_KEY["evolution"].rate9 * SLEW_FACTOR) + 0.1  # only the short way is quick enough
    assert degrees(ask(emu, sent, 0x10, 0x01)) == pytest.approx(337.5, abs=1e-3)


def test_goto_slow_is_a_slow_final_approach():
    emu, sent, clock = make(model="evolution")
    ask(emu, sent, 0x10, 0x17, steps(5.0))
    clock.t = 1.0
    assert degrees(ask(emu, sent, 0x10, 0x01)) == pytest.approx(SLOW_APPROACH, abs=1e-3)


@pytest.mark.parametrize("rate,per_second", [(1, 2 * SIDEREAL / 3600), (5, 32 * SIDEREAL / 3600), (7, 1.0),
                                             (9, 3.0 * SLEW_FACTOR)])
def test_direction_buttons_move_at_the_hand_controller_rates(rate, per_second):
    emu, sent, clock = make(model="slt")
    assert ask(emu, sent, 0x11, 0x24, bytes((rate,))) == b"\x01"
    clock.t = 2.0
    assert degrees(ask(emu, sent, 0x11, 0x01)) == pytest.approx(2 * per_second, abs=1e-4)
    assert ask(emu, sent, 0x11, 0x13) == b"\x00"  # slewing while held
    ask(emu, sent, 0x11, 0x24, b"\x00")           # released
    clock.t = 5.0
    assert degrees(ask(emu, sent, 0x11, 0x01)) == pytest.approx(2 * per_second, abs=1e-4)
    assert ask(emu, sent, 0x11, 0x13) == b"\xff"


def test_negative_buttons_tracking_and_sync():
    emu, sent, clock = make(model="cpc")
    ask(emu, sent, 0x10, 0x25, b"\x07")  # 1 deg/s, negative
    clock.t = 3.0
    assert degrees(ask(emu, sent, 0x10, 0x01)) == pytest.approx(357.0, abs=1e-3)
    ask(emu, sent, 0x10, 0x24, b"\x00")
    assert ask(emu, sent, 0x10, 0x06, (0xFFFF).to_bytes(2, "big")) == b"\x01"  # sidereal tracking
    clock.t = 3.0 + 3600
    assert degrees(ask(emu, sent, 0x10, 0x01)) == pytest.approx((357.0 + SIDEREAL) % 360, abs=1e-3)  # wraps past 360
    assert ask(emu, sent, 0x10, 0x13) == b"\xff"  # tracking is not slewing
    assert ask(emu, sent, 0x10, 0x04, steps(100)) == b"\x01"  # sync
    assert degrees(ask(emu, sent, 0x10, 0x01)) == pytest.approx(100.0, abs=1e-3)


def test_settings_cpwi_reads_and_writes():
    emu, sent, _ = make(model="slt")
    ask(emu, sent, 0x10, 0x10, b"\x05")
    assert ask(emu, sent, 0x10, 0x40) == b"\x05" and ask(emu, sent, 0x10, 0x41) == b"\x00"
    ask(emu, sent, 0x11, 0xFD, b"\x01")
    assert ask(emu, sent, 0x11, 0xFC) == b"\x01" and ask(emu, sent, 0x10, 0xFC) == b"\x00"
    assert ask(emu, sent, 0x10, 0x3B) == b"\x00"
    ask(emu, sent, 0x10, 0x38)
    assert ask(emu, sent, 0x10, 0x3B) == b"\xff"
    assert ask(emu, sent, 0x10, 0x99) is None  # unknown: no answer, so it shows as unknown traffic


def test_it_answers_cpwi_like_a_real_cpc():
    """The custom rate 9 and autoguide rate CPWI asks every mount but the SLT, with a real CPC's replies."""
    emu, sent, _ = make(model="cpc")
    exchange = [(0x10, 0xFE, "0613"), (0x11, 0xFE, "0613"), (0x10, 0x05, "1189"), (0x10, 0x23, "00"),
                (0x10, 0x21, "0e74125c"), (0x10, 0x47, "80"), (0x11, 0x47, "80"), (0x10, 0x41, "00"),
                (0x11, 0x41, "00"), (0x10, 0xFC, "00"), (0x11, 0xFC, "00"), (0x10, 0x01, "000000")]
    assert [ask(emu, sent, device, command).hex() for device, command, _ in exchange] == [r for _, _, r in exchange]


def bam16(degrees):
    return int(round(degrees % 360 / 360 * 65536)) % 65536


def test_without_user_limits_the_telescope_moves_freely():
    emu, sent, clock = make(model="cpc", initial_alt=10)
    assert [ask(emu, sent, 0x11, c).hex() for c in (0x1C, 0x1D, 0x1E)] == ["0000", "0000", "00"]  # off, as real MCs
    ask(emu, sent, 0x11, 0x25, b"\x09")  # down, past the horizon: nothing stops it
    clock.t = 5
    assert emu.snapshot()["alt"] < -20 and ask(emu, sent, 0x11, 0x13) == b"\x00"


def test_the_motor_stops_at_the_user_limits_the_app_sets():
    emu, sent, clock = make(model="cpc", initial_alt=10)
    assert ask(emu, sent, 0x11, 0x1A, bam16(-10).to_bytes(2, "big")) == b"\x01"  # minimum: the horizon
    assert ask(emu, sent, 0x11, 0x1B, bam16(75).to_bytes(2, "big")) == b"\x01"   # maximum: 85 degrees up
    assert ask(emu, sent, 0x11, 0x1F, b"\x01") == b"\x01"
    assert ask(emu, sent, 0x11, 0x1E) == b"\x01"
    assert ask(emu, sent, 0x11, 0x1C) == bam16(-10).to_bytes(2, "big")  # read back as set
    ask(emu, sent, 0x11, 0x24, b"\x09")                                  # up, fast
    clock.t = 30
    assert emu.snapshot()["alt"] == pytest.approx(85.0, abs=0.01) and ask(emu, sent, 0x11, 0x13) == b"\xff"
    ask(emu, sent, 0x11, 0x02, steps(300))                                # a go-to far below the minimum (-60)
    clock.t = 60
    assert emu.snapshot()["alt"] == pytest.approx(0.0, abs=0.01) and ask(emu, sent, 0x11, 0x13) == b"\xff"
    ask(emu, sent, 0x11, 0x1F, b"\x00")                                  # limits off again: free
    ask(emu, sent, 0x11, 0x25, b"\x09")
    clock.t = 62
    assert emu.snapshot()["alt"] < -10


def test_user_limits_are_per_motor():
    emu, sent, clock = make(model="cgx", inclination=35, rotation=180, initial_alt=35, initial_az=180)
    ask(emu, sent, 0x10, 0x1A, bam16(-20).to_bytes(2, "big"))
    ask(emu, sent, 0x10, 0x1B, bam16(20).to_bytes(2, "big"))
    ask(emu, sent, 0x10, 0x1F, b"\x01")  # RA limits, as CPWI keeps for equatorial mounts
    ask(emu, sent, 0x10, 0x24, b"\x09")
    ask(emu, sent, 0x11, 0x24, b"\x09")
    clock.t = 30
    axes = emu.snapshot()["axes"]
    assert axes["azm"] == pytest.approx(20.0, abs=0.01)                  # stopped at its limit
    assert ask(emu, sent, 0x11, 0x13) == b"\x00"                         # the other motor carries on


# --- where it points -------------------------------------------------------------------------

def test_alt_az_pointing_starts_at_the_initial_position_and_follows_the_motors():
    emu, sent, clock = make(model="slt", initial_alt=10, initial_az=90)
    assert (emu.snapshot()["alt"], emu.snapshot()["az"]) == (10.0, 90.0)
    ask(emu, sent, 0x10, 0x02, steps(30))
    ask(emu, sent, 0x11, 0x02, steps(20))
    assert emu.snapshot()["target"] == {"alt": 30.0, "az": 120.0} and emu.snapshot()["moving"] is True
    clock.t = 20
    assert (emu.snapshot()["alt"], emu.snapshot()["az"]) == (30.0, 120.0) and emu.snapshot()["target"] is None


def test_on_a_wedge_the_first_axis_turns_around_the_pole():
    emu, sent, clock = make(model="cpc", wedge=True, inclination=35, rotation=180, initial_alt=20, initial_az=150, polar_error_alt=0, polar_error_az=0)
    pole = _vector(35, 180)
    start = emu.snapshot()
    distance = lambda s: math.degrees(math.acos(_dot(_vector(s["alt"], s["az"]), pole)))
    ask(emu, sent, 0x10, 0x24, b"\x09")  # turn the polar axis only
    clock.t = 20
    moved = emu.snapshot()
    assert (moved["alt"], moved["az"]) != (start["alt"], start["az"])
    assert distance(moved) == pytest.approx(distance(start), abs=1e-3)  # a circle around the pole (status: 0.0001 deg)


def test_an_equatorial_mount_is_always_polar_and_a_slt_never_takes_a_wedge():
    assert MountSetup(model="cgx").validated().wedge is True
    with pytest.raises(ValueError, match="cannot be used on a wedge"):
        MountSetup(model="slt", wedge=True).validated()
    with pytest.raises(ValueError, match="unknown mount"):
        MountSetup(model="dobsonian").validated()
    with pytest.raises(ValueError, match="inclination"):
        MountSetup(model="cpc", wedge=True, inclination=95).validated()


def test_frames_round_trip():
    for inclination, rotation in ((90, 0), (35, 180), (52, 0), (0, 90)):
        frame = Frame.for_axis(inclination, rotation)
        for first, second in ((0, 0), (123, -10), (300, 75)):
            alt, az = frame.pointing(first, second)
            back = frame.angles(alt, az)
            assert back[0] == pytest.approx(first % 360, abs=1e-9) and back[1] == pytest.approx(second, abs=1e-9)


def test_firmware_versions_can_be_set_and_the_defaults_parse():
    for spec in MOUNTS:
        parse_version(spec.firmware)
    emu, sent, _ = make(model="cpc")
    emu.configure(MountSetup(model="cpc"), azm_version="6.20", alt_version="6.21")
    assert format_version(ask(emu, sent, 0x10, 0xFE)) == "6.20" and format_version(ask(emu, sent, 0x11, 0xFE)) == "6.21"


def test_a_rate_0_move_stops_button_motion_but_not_a_go_to():
    """CPWI sends 'move at rate 0' (stop the buttons) in its normal traffic; a real motor keeps
    a go-to running. When this cancelled go-tos, StarSense Auto solved the same spot again and again."""
    emu, sent, clock = make(model="cpc")
    ask(emu, sent, 0x10, 0x02, steps(40))  # a go-to...
    ask(emu, sent, 0x10, 0x24, b"\x00")     # ...then 'stop the buttons', as CPWI sends
    ask(emu, sent, 0x10, 0x25, b"\x00")
    clock.t = 20
    assert degrees(ask(emu, sent, 0x10, 0x01)) == pytest.approx(40.0, abs=1e-3)  # it still arrived
    assert ask(emu, sent, 0x10, 0x13) == b"\xff"
    ask(emu, sent, 0x10, 0x24, b"\x09")     # a button move...
    clock.t = 21
    ask(emu, sent, 0x10, 0x24, b"\x00")     # ...is stopped by rate 0
    stopped_at = degrees(ask(emu, sent, 0x10, 0x01))
    clock.t = 25
    assert degrees(ask(emu, sent, 0x10, 0x01)) == pytest.approx(stopped_at, abs=1e-6)



def test_a_sync_changes_the_motor_reading_but_does_not_move_the_telescope():
    """CPWI starts ASPA by setting both motors to 90 degrees; on a real mount nothing moves.
    When this moved the emulated telescope, it ended up pointing below the horizon."""
    emu, sent, clock = make(model="cgx-l", inclination=35, rotation=180, initial_alt=35, initial_az=180, polar_error_alt=0, polar_error_az=0)
    before = (emu.snapshot()["alt"], emu.snapshot()["az"])
    for motor in (0x10, 0x11):
        assert ask(emu, sent, motor, 0x04, bytes.fromhex("400000")) == b"\x01"  # set position: 90 degrees
        assert degrees(ask(emu, sent, motor, 0x01)) == pytest.approx(90.0)     # it now reads 90...
    assert (emu.snapshot()["alt"], emu.snapshot()["az"]) == before              # ...and points where it did
    ask(emu, sent, 0x10, 0x02, steps(100))                                      # go-tos use the new readings
    clock.t = 30
    assert degrees(ask(emu, sent, 0x10, 0x01)) == pytest.approx(100.0, abs=1e-3)
    # the polar axis turned 10 degrees: still the same distance from the pole
    pole = _vector(35, 180)
    distance = lambda s: math.degrees(math.acos(_dot(_vector(s["alt"], s["az"]), pole)))
    assert distance(emu.snapshot()) == pytest.approx(distance({"alt": before[0], "az": before[1]}), abs=1e-3)



def test_a_placement_error_is_measured_and_turning_the_knobs_removes_it():
    """The polar axis is off by the placement error; adjusting inclination and rotation while the
    telescope runs moves it (and the telescope with it), as turning a real wedge's knobs."""
    emu, sent, clock = make(model="cgx-l", inclination=35, rotation=180, initial_alt=10, initial_az=90,
                            polar_error_alt=0.5, polar_error_az=-1.0)
    assert emu.polar_axis() == pytest.approx((35.5, 179.0))              # off by the error
    before = emu.snapshot()
    ask(emu, sent, 0x10, 0x02, steps(30))                                  # a go-to, then the knobs move
    clock.t = 10
    reading = degrees(ask(emu, sent, 0x10, 0x01))
    emu.adjust(34.5, 181.0)                                                 # compensate: the axis is now exact
    assert emu.polar_axis() == pytest.approx((35.0, 180.0))
    assert degrees(ask(emu, sent, 0x10, 0x01)) == pytest.approx(reading)   # the motors did not move...
    assert (emu.snapshot()["alt"], emu.snapshot()["az"]) != (before["alt"], before["az"])  # ...the mount tilted
    with pytest.raises(ValueError):
        emu.adjust(95, 180)
    altaz, sent2, _ = make(model="cpc")
    with pytest.raises(ValueError, match="alt-az"):
        altaz.adjust(35, 180)
