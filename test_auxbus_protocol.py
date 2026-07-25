"""Unit tests for the hardware-independent logic in auxbus_protocol.py.

No serial port or socket is needed to run these.
"""

import auxbus_protocol as protocol


# ---- pure math -----------------------------------------------------------

def test_twos_comp_zero():
    assert protocol.twos_comp(0, 8) == 0


def test_twos_comp_max_positive_8bit():
    assert protocol.twos_comp(127, 8) == 127


def test_twos_comp_negative_wraparound_8bit():
    assert protocol.twos_comp(128, 8) == -128
    assert protocol.twos_comp(255, 8) == -1


def test_tosigned24_positive():
    assert protocol.tosigned24("000001") == 1
    assert protocol.tosigned24("7fffff") == 8388607


def test_tosigned24_negative():
    assert protocol.tosigned24("800000") == -8388608
    assert protocol.tosigned24("ffffff") == -1


def test_hextoposition_matches_tosigned24_scaling():
    assert protocol.hextoposition("000000") == 0
    expected = protocol.tosigned24("7fffff") / pow(2, 24) * 360
    assert protocol.hextoposition("7fffff") == expected


def test_decdeg2dms_zero():
    assert protocol.decdeg2dms(0) == (0, 0, 0.0)


def test_decdeg2dms_positive():
    assert protocol.decdeg2dms(10.5) == (10, 30, 0.0)


def test_decdeg2dms_negative():
    assert protocol.decdeg2dms(-10.5) == (-10, 30, 0.0)


def test_checksum_simple_values():
    assert protocol.checksum([0]) == 0
    assert protocol.checksum([1]) == 255


# ---- frame build/parse round-trip ----------------------------------------

def test_build_then_parse_round_trip():
    frame_hex = protocol.build_frame(sender=0x2f, receiver=0x10, command=0xfe, value_hex='')
    parsed = protocol.parse_frame(frame_hex)
    assert parsed.checksum_ok is True
    assert parsed.sender == 0x2f
    assert parsed.receiver == 0x10
    assert parsed.command == 0xfe
    assert parsed.commandvalue == []


def test_build_then_parse_round_trip_with_value():
    frame_hex = protocol.build_frame(sender=0x2f, receiver=0x10, command=0x02, value_hex='0102ff')
    parsed = protocol.parse_frame(frame_hex)
    assert parsed.checksum_ok is True
    assert parsed.commandvalue == [hex(0x01), hex(0x02), hex(0xff)]


def test_parse_frame_detects_corrupted_checksum():
    frame_hex = protocol.build_frame(sender=0x2f, receiver=0x10, command=0xfe, value_hex='')
    good_checksum = int(frame_hex[-2:], 16)
    bad_checksum = (good_checksum + 1) & 0xff
    corrupted = frame_hex[:-2] + "{:02x}".format(bad_checksum)

    parsed = protocol.parse_frame(corrupted)

    assert parsed.checksum_ok is False
    # Header fields are still recovered even though the checksum is bad.
    assert parsed.sender == 0x2f
    assert parsed.receiver == 0x10
    assert parsed.command == 0xfe


def test_parse_frame_truncated_does_not_raise():
    assert protocol.parse_frame("").checksum_ok is False
    assert protocol.parse_frame("3b02").checksum_ok is False

    frame_hex = protocol.build_frame(sender=0x2f, receiver=0x10, command=0x02, value_hex='0102ff')
    truncated = frame_hex[:-4]
    assert protocol.parse_frame(truncated).checksum_ok is False


# ---- decode_command_value --------------------------------------------------

def test_decode_command_value_fw_version_short_form():
    result = protocol.decode_command_value(0x10, 0x10, 0xfe, ['0x1', '0x2', '0x3'])
    assert result == "1.2.3"


def test_decode_command_value_dewheater_power():
    # 0x2ee0 -> 12000 / 1000 = 12.0 V ; 0x01f4 -> 500 / 1000 = 0.5 A
    result = protocol.decode_command_value(0x17, 0x17, 0x0, ['0x2e', '0xe0', '0x1', '0xf4'])
    assert result == "12.0 V - 0.5 A "


def test_decode_command_value_short_payload_falls_back_to_raw_hex():
    # 0xb0/0x33 (GPS_GET_TIME) normally expects 3 bytes (h/m/s); only giving 1
    # would raise IndexError in the naive implementation. It must not raise,
    # and should fall back to a raw hex dump instead.
    result = protocol.decode_command_value(0xb0, 0xb0, 0x33, ['0x5'])
    assert result == "05"


# ---- Starsense geometry -----------------------------------------------------

def _wire_msg_from_reversed(rev):
    """Undo decode_starsense_star's/starsense_pixel's byte-pair reversal, so
    tests can express the intuitive (already de-reversed) byte layout and
    have the wire-format input derived from it automatically."""
    return "".join(reversed([rev[i:i + 2] for i in range(0, len(rev), 2)]))


def test_decode_starsense_star_and_pixel_for_centered_star():
    # bx=0x40-0x40=0, px raw=0, by=0x40-0x40=0, py raw=0 -> dead center.
    rev = "40" + "000000" + "40" + "000000"
    msg = _wire_msg_from_reversed(rev)

    star_text = protocol.decode_starsense_star(msg)
    assert star_text.startswith("0 - ")
    assert " - 0 - " in star_text

    pixelx, pixely, bx, by = protocol.starsense_pixel(msg)
    assert (bx, by) == (0, 0)
    assert pixelx == protocol.STARSENSE_XRES // 2
    assert pixely == protocol.STARSENSE_YRES // 2


def test_starsense_pixel_offsets_in_expected_direction():
    # A positive raw X offset should move the pixel right of center. Needs to
    # be large enough to cross a pixel boundary at 24-bit resolution over
    # STARSENSE_XRES pixels (a raw value of 0x10 alone is sub-pixel).
    rev = "40" + "100000" + "40" + "000000"
    msg = _wire_msg_from_reversed(rev)

    pixelx, pixely, bx, by = protocol.starsense_pixel(msg)
    assert pixelx > protocol.STARSENSE_XRES // 2
