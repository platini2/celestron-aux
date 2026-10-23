import pytest

from celestron_aux.decoding import describe_packet
from celestron_aux.protocol import AuxPacket


def value(source, destination, command, data):
    return describe_packet(AuxPacket(source, destination, command, bytes.fromhex(data))).value


def test_names_are_scoped_to_the_device_the_packet_is_about():
    request = describe_packet(AuxPacket(0x0D, 0x11, 0x01))
    assert (request.sender, request.receiver, request.command) == ("Nexstar+ HC", "ALT MC", "MTR_GET_POSITION")
    assert describe_packet(AuxPacket(0x2F, 0xB0, 0x01)).command == "GPS_GET_LAT"


@pytest.mark.parametrize("device,name", [
    (0x17, "GET_VERSION"),      # dew heater: no version entry of its own
    (0xB8, "GET_VERSION"),      # StarSense camera SW: no command table at all
    (0x99, "GET_VERSION"),      # even an unknown device
    (0x10, "MTR_GET_VERSION"),  # a device-specific name wins
    (0xB0, "GPS_GET_FW_VER"),
])
def test_version_requests_are_named_for_every_device(device, name):
    assert describe_packet(AuxPacket(0x2F, device, 0xFE)).command == name
    assert describe_packet(AuxPacket(device, 0x2F, 0xFE, b"\x01\x02")).value == "1.2"


def test_starsense_v2_has_the_autoguider_commands_except_guiding():
    from celestron_aux import catalog

    v2 = dict(catalog.commands_for(0xBD))
    autoguider = dict(catalog.commands_for(0xB9))
    assert set(v2) - {0x08, 0x4B} == {c for c in autoguider if c >> 4 != 0x2}  # all but 0x20/0x23, plus 0x08, 0x4b
    assert all(v2[c] == autoguider[c].replace("SSAG_", "SS2_") for c in v2 if c not in (0xFE, 0x07, 0x08, 0x4B))
    assert catalog.device_name(0xBD) == "Starsense AutoAlign V2"
    assert describe_packet(AuxPacket(0x20, 0xBD, 0x20)).command == "Unknown"  # no guiding on the V2


def test_dew_heater_command_names():
    names = [describe_packet(AuxPacket(0x0D, 0x17, c)).command for c in (0x02, 0x03, 0x14, 0x16, 0x17, 0x19, 0x1A, 0x20, 0x21)]
    assert names == ["DEWHEATER_GET_LED_BRIGHTNESS", "DEWHEATER_SET_INPUT_LIMIT", "DEWHEATER_ENABLE_PORT",
                     "DEWHEATER_SET_AUTO_AGGRESSION", "DEWHEATER_SET_MANUAL_POWER", "DEWHEATER_RECALIBRATE_SENSOR",
                     "DEWHEATER_IS_CALIBRATING", "DEWHEATER_SET_LED_BRIGHTNESS", "DEWHEATER_UNKNOWN_21"]
    assert describe_packet(AuxPacket(0x17, 0x0D, 0x21, b"\xff")).value == ""  # unknown: shown as hex


def test_every_exact_mount_model_agrees_with_its_family():
    from celestron_aux import catalog

    assert catalog.mount_table_problems() == []


def test_a_contradicting_model_or_a_missing_family_is_caught(monkeypatch):
    from celestron_aux import catalog

    monkeypatch.setattr(catalog, "MOUNT_MODELS", {**catalog.MOUNT_MODELS, 0x0D99: "AVX", 0x2203: "Imaginary"})
    assert catalog.mount_table_problems() == ["0x0d99 AVX: family 0x0d is CGE Pro", "0x2203 Imaginary: no family 0x22"]


@pytest.mark.parametrize("model,name", [
    (0x1189, "CPC Deluxe"),                         # exact match wins
    (0x0682, "Advanced GT"),
    (0x0001, "Nexstar GPS"),
    (0x0D12, "CGE Pro family (0x0d12)"),            # a known family
    (0x0687, "Advanced GT / CG-5 family (0x0687)"),
    (0x1891, "CGX-L family (0x1891)"),
    (0x1190, "CPC Deluxe family (0x1190)"),         # family inferred from an exact id
    (0x2203, "Unknown (0x2203)"),                   # no family known
    (0x0042, "Unknown (0x0042)"),                   # a single-byte reply has no family byte
])
def test_mount_names_with_families(model, name):
    from celestron_aux import catalog

    assert catalog.mount_name(model) == name


def test_origin_and_advanced_gt_models():
    from celestron_aux import catalog

    assert catalog.mount_name(0x1D8A) == "Origin" and catalog.mount_name(0x0682) == "Advanced GT"
    assert describe_packet(AuxPacket(0x10, 0x2F, 0x05, bytes.fromhex("1d8a"))).value == "0x1d8a (Origin)"


@pytest.mark.parametrize("degrees,text", [
    (0.0, "0h00m00.00s"), (15.0, "1h00m00.00s"), (297.7, "19h50m48.00s"),
    (-15.0, "23h00m00.00s"), (360.0, "0h00m00.00s"), (359.99999999, "0h00m00.00s"),
])
def test_right_ascension_is_shown_in_hours(degrees, text):
    from celestron_aux.protocol.units import format_hms

    assert format_hms(degrees) == text


EXPECTED_CALIBRATION_LINES = ['Slot 0: offset position RA', 'Slot 0: offset position RA, 19h46m39.86s (296.6661°)', 'Slot 1: offset position Dec', 'Slot 1: offset position Dec, 9°14\'22.29"', 'Slot 2: calibration star RA', 'Slot 2: calibration star RA, 19h50m48.11s (297.7005°)', 'Slot 3: calibration star Dec', 'Slot 3: calibration star Dec, 8°52\'16.34"', 'Slot 4: image rotation', 'Slot 4: image rotation, 47.77°', 'Slot 5: calibration time', 'Slot 5: calibration time, 2026-10-20 00:06 UTC (1792454784)']


def test_calibration_slots_are_described_using_the_slot_just_requested():
    # The real exchange CPWI had with the camera after calibrating on Altair.
    exchange = [("00", "43945542"), ("01", "4113d518"), ("02", "4394d9a9"), ("03", "410df076"),
                ("04", "423f14d7"), ("05", "4ed5ad61")]
    lines = []
    for slot, reply in exchange:
        lines.append(describe_packet(AuxPacket(0x20, 0xBD, 0x08, bytes.fromhex(slot))).value)
        lines.append(describe_packet(AuxPacket(0xBD, 0x20, 0x08, bytes.fromhex(reply))).value)
    assert lines == EXPECTED_CALIBRATION_LINES


def test_an_empty_calibration_slot():
    describe_packet(AuxPacket(0x20, 0xBD, 0x08, b"\x03"))
    assert describe_packet(AuxPacket(0xBD, 0x20, 0x08, bytes(4))).value == "Slot 3: calibration star Dec, 0 (empty)"


def test_battery_and_wifi_and_gps_names():
    assert describe_packet(AuxPacket(0x0D, 0xB6, 0x18)).command == "BAT_GET_MAX_CURRENT"
    assert describe_packet(AuxPacket(0x0D, 0xB5, 0x10, b"\x01")).command == "WIFI_SET_ENABLE"
    assert describe_packet(AuxPacket(0x0D, 0xB0, 0x55)).command == "GPS_GET_HW_VER"


def test_autoguider_is_named_and_its_motor_traffic_uses_motor_names():
    request = describe_packet(AuxPacket(0x0D, 0xB9, 0x01))
    assert (request.receiver, request.command) == ("Starsense AutoGuider", "SSAG_START_PLATE_SOLVE")
    guide = describe_packet(AuxPacket(0xB9, 0x10, 0x26, b"\x05\x10"))  # a guiding correction it sends
    assert (guide.sender, guide.command) == ("Starsense AutoGuider", "MTR_AUX_GUIDE")
    assert describe_packet(AuxPacket(0x10, 0xB9, 0x26)).command == "MTR_AUX_GUIDE"  # the motor's reply
    assert describe_packet(AuxPacket(0xB9, 0x0D, 0x07, b"\x01")).command == "SSAG_UNKNOWN_07"
    assert describe_packet(AuxPacket(0x20, 0xBD, 0x45)).command == "SS2_GET_IMAGE_ROTATION"
    assert describe_packet(AuxPacket(0x20, 0xBD, 0x4B)).command == "SS2_GET_TELESCOPE_POSITION"
    assert describe_packet(AuxPacket(0x0D, 0xB9, 0x45)).command == "SSAG_GET_IMAGE_ROTATION"


@pytest.mark.parametrize("command,name", [
    (0x02, "MTR_GOTO_FAST"), (0x17, "MTR_GOTO_SLOW"),
    (0x48, "MTR_SET_SWITCH_CALIBRATION"), (0x49, "MTR_GET_SWITCH_CALIBRATION"),
    (0x81, "MTR_PROGRAM_ENTER"), (0x82, "MTR_PROGRAM_INIT"), (0x83, "MTR_PROGRAM_DATA"), (0x84, "MTR_PROGRAM_END"),
])
def test_motor_command_names(command, name):
    for motor in (0x10, 0x11):
        assert describe_packet(AuxPacket(0x0D, motor, command)).command == name


def test_undecoded_payloads_have_no_value_but_keep_their_hex():
    description = describe_packet(AuxPacket(0x99, 0x2F, 0x42, b"\x01\x02"))
    assert (description.sender, description.command) == ("Unknown", "Unknown")
    assert (description.value, description.payload, description.text) == ("", "0102", "0102")


def test_decoded_payloads_keep_their_hex_too():
    description = describe_packet(AuxPacket(0x10, 0x2F, 0xFE, bytes.fromhex("070b1405")))
    assert (description.value, description.payload, description.text) == ("7.11.5125", "070b1405", "7.11.5125")


@pytest.mark.parametrize("source,destination,command,data,expected", [
    (0x10, 0x2F, 0xFE, "070b1405", "7.11.5125"),
    (0x11, 0x0D, 0x01, "400000", "90\N{DEGREE SIGN}00'00.00\""),
    (0x10, 0x2F, 0x05, "1687", "0x1687 (Nexstar Evolution)"),
    (0x10, 0x0D, 0x13, "ff", "255 - Done"),
    (0x0D, 0x10, 0x06, "003c2a", "15.0410 arcsec/s"),
    (0x10, 0x0D, 0x06, "01", "1 - ACK"),
    (0xB0, 0x0D, 0x33, "170509", "23:05:09"),
    (0xB0, 0x0D, 0x37, "01", "1 - Yes"),
    (0xB4, 0x0E, 0x91, "0312", "3 - Ready - Stars: 18"),
    (0x17, 0x0D, 0x00, "2ee00bb8", "12 V - 3 A"),
    (0x17, 0x0D, 0x18, "00004e20000027100a", "Temp: 20\N{DEGREE SIGN}C Dew: 10\N{DEGREE SIGN}C Hum: 10%"),
    (0x0D, 0x17, 0x11, "01", "1 - Get dew heater ports"),
    (0x17, 0x0D, 0x02, "80", "Brightness 128"),
    (0x0D, 0x17, 0x20, "40", "Brightness 64"),
    (0x0D, 0x17, 0x03, "1388", "5 A"),
    (0x0D, 0x17, 0x14, "0201", "Port 2 on"),
    (0x0D, 0x17, 0x16, "0007", "Heater 1, aggression 7"),
    (0x0D, 0x17, 0x17, "0180", "Heater 2, power 50%"),
    (0x0D, 0x17, 0x19, "01", "Start"),
    (0x17, 0x0D, 0x1A, "00", "Not calibrating"),
    (0xB6, 0x0D, 0x10, "010200bc614e", "12.346 V, status 2 (high), charging"),
    (0xB6, 0x0D, 0x18, "09a4", "2468 mA"),
    (0x0D, 0xB7, 0x10, "01", "Set charging on"),
    (0xB7, 0x0D, 0x10, "00", "Charging off"),
    (0x0D, 0xBF, 0x10, "0280", "Set Wi-Fi light to 128"),
    (0x0D, 0xBF, 0x10, "00", "Tray light"),
    (0xBF, 0x0D, 0x10, "40", "Level 64"),
    (0x12, 0x0D, 0x01, "0186a0", "100000"),
    (0x0D, 0x12, 0x02, "0186a0", "To 100000"),
    (0x12, 0x0D, 0x13, "ff", "255 - Done"),
    (0x12, 0x0D, 0x2C, "000003e8000f4240", "Limits 1000 to 1000000"),
    (0xB0, 0x0D, 0x55, "ab", "0xab (Celestron GPS)"),
    (0xB0, 0x0D, 0x55, "eb", "0xeb"),  # an unknown hardware version: shown as is
    (0x0D, 0xB5, 0x10, "00", "Off"),
    # StarSense AutoGuider, using published examples
    (0xB9, 0x0D, 0x02, "000000000000000000", "In progress"),
    (0xB9, 0x0D, 0x02, "01402d57e13f9a7ee0", "Solved, RA 10h20m44.39s, Dec 69\N{DEGREE SIGN}09'20.99\""),
    (0x0D, 0xB9, 0x03, "406ef02e3eab4c8e", "RA 14h15m38.06s, Dec 19\N{DEGREE SIGN}10'09.69\""),
    (0xB9, 0x0D, 0x04, "01fe6efac0", "Solved, x 65134, y 64192"),
    (0x0D, 0xB9, 0x20, "69", "Enabled, flags 0x69"),
    (0x0D, 0xB9, 0x20, "00", "Disabled"),
    (0x0D, 0xB9, 0x30, "040128630000", "Model 0x04, mount mode 1, AZM firmware 40.99"),
    (0x0D, 0xB9, 0x31, "4222cb4bc295cc2d", "Lat 40.6985, Lon -74.8988"),
    (0x0D, 0xB9, 0x32, "2c892d13", "2023-09-05 00:04:35 UTC"),
    # StarSense Camera V2, captured from CPWI on a CPC mount
    (0x20, 0xBD, 0x30, "110106130000", "Model 0x11 (CPC Deluxe family), mount mode 1, AZM firmware 6.19"),
    (0x20, 0xBD, 0x30, "0d0007020000", "Model 0x0d (CGE Pro family), mount mode 0, AZM firmware 7.2"),
    (0x20, 0xBD, 0x31, "c2080001c2680000", "Lat -34.0000, Lon -58.0000"),
    (0x20, 0xBD, 0x32, "3267c1ef", "2026-10-18 17:44:15 UTC"),
    (0xBD, 0x20, 0x02, "01402d57e13f9a7ee0", "Solved, RA 10h20m44.39s, Dec 69\N{DEGREE SIGN}09'20.99\""),
    # a real StarSense AutoAlign V2 session: CPWI listed these as RA 18h30m, 20h03m, 0h13m, 22h54m
    (0xBD, 0x20, 0x02, "01409b127fbd82e78f", "Solved, RA 18h30m37.39s, Dec -3\N{DEGREE SIGN}39'44.08\""),
    (0xBD, 0x20, 0x02, "0140bfea033c513bf0", "Solved, RA 22h54m29.01s, Dec 0\N{DEGREE SIGN}43'54.13\""),
    (0xBD, 0x20, 0x4B, "01409a7c39bd6a407f", "Solved, RA 18h26m25.14s, Dec -3\N{DEGREE SIGN}16'36.37\""),
    (0xBD, 0x20, 0x02, "040000000000000000", "Failed (status 4)"),  # a real failed solve
    (0xB9, 0x0D, 0x04, "0400000000", "Failed (status 4)"),
    (0xBD, 0x20, 0x02, "070000000000000000", "Status 7, RA 0h00m00.00s, Dec 0\N{DEGREE SIGN}00'00.00\""),  # unknown: shown as is
    (0xBD, 0x20, 0x07, "00", "No"),   # real replies: before any calibration, and after
    (0xBD, 0x20, 0x07, "01", "Yes"),
    (0xBD, 0x20, 0x45, "4249ac41", "Rotation 50.42\N{DEGREE SIGN}"),   # real V2 replies
    (0xBD, 0x20, 0x45, "423e1c24", "Rotation 47.53\N{DEGREE SIGN}"),
    (0xB9, 0x0D, 0x45, "42e1790e", "Rotation 112.74\N{DEGREE SIGN}"),  # an AutoGuider example
    (0x20, 0xBD, 0x03, "40a6446e3e1e8c37", "RA 19h50m48.11s, Dec 8\N{DEGREE SIGN}52'16.34\""),  # Altair
    (0xBD, 0x20, 0x04, "01fe6efac0", "Solved, x 65134, y 64192"),
    (0xB9, 0x0D, 0xFE, "230a1e", "35.10.30"),
])
def test_payload_decoders(source, destination, command, data, expected):
    assert value(source, destination, command, data) == expected


def test_truncated_payload_is_flagged_not_raised():
    for packet in (AuxPacket(0x17, 0x0D, 0x18, b"\x00\x00"), AuxPacket(0xB6, 0x0D, 0x10, b"\x01")):
        description = describe_packet(packet)
        assert description.value == "" and description.malformed
        assert description.text == f"{packet.data.hex()} (unexpected format)"
