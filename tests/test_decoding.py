import pytest

from celestron_aux.decoding import describe_packet
from celestron_aux.protocol import AuxPacket


def value(source, destination, command, data):
    return describe_packet(AuxPacket(source, destination, command, bytes.fromhex(data))).value


def test_names_are_scoped_to_the_device_the_packet_is_about():
    request = describe_packet(AuxPacket(0x0D, 0x11, 0x01))
    assert (request.sender, request.receiver, request.command) == ("Nexstar+ HC", "ALT MC", "MTR_GET_POSITION")
    assert describe_packet(AuxPacket(0x2F, 0xB0, 0x01)).command == "GPS_GET_LAT"


def test_unknown_names_and_payloads_fall_back_gracefully():
    description = describe_packet(AuxPacket(0x99, 0x2F, 0x42, b"\x01\x02"))
    assert (description.sender, description.command, description.value) == ("Unknown", "Unknown", "0102")


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
])
def test_payload_decoders(source, destination, command, data, expected):
    assert value(source, destination, command, data) == expected


def test_truncated_payload_is_reported_not_raised():
    assert value(0x17, 0x0D, 0x18, "0000") == "0000 (unexpected format)"
