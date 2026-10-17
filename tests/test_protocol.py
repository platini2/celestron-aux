from pathlib import Path

import pytest

from celestron_aux.protocol import AuxPacket, CorruptFrame, FrameParser, StarSenseFrame, checksum
from celestron_aux.protocol.units import (
    bam_to_degrees, degrees_to_bam, format_dms, format_version, to_signed,
)

CAPTURED_FRAME = bytes.fromhex((Path(__file__).parent / "fixtures" / "starsense_frame.hex").read_text().strip())


def test_version_request_encoding_matches_the_wire_format():
    assert AuxPacket(0x2F, 0x10, 0xFE).encode() == bytes.fromhex("3b032f10fec0")


def test_encoding_includes_payload_in_length_and_checksum():
    raw = AuxPacket(0xB0, 0x0D, 0x33, bytes((1, 2, 3))).encode()
    assert raw[1] == 6
    assert checksum(raw[1:-1]) == raw[-1]


def test_packet_rejects_out_of_range_fields():
    with pytest.raises(ValueError):
        AuxPacket(0x100, 0x10, 0xFE)
    with pytest.raises(ValueError):
        AuxPacket(0x2F, 0x10, 0xFE, bytes(253))


def test_parser_handles_frames_split_across_reads_and_noise():
    parser = FrameParser()
    packet = AuxPacket(0x10, 0x2F, 0xFE, bytes((7, 11, 0, 5)))
    stream = b"\x00\xff" + packet.encode() + packet.encode()
    parsed = []
    for byte in stream:
        parsed += parser.feed(bytes((byte,)))
    assert [p.frame for p in parsed] == [packet, packet]
    assert parsed[0].raw == packet.encode()
    assert parser.out_of_frame_bytes == 2


def test_parser_reports_bad_checksum_and_resynchronises():
    good = AuxPacket(0x10, 0x2F, 0xFE, b"\x07\x0b").encode()
    bad = bytearray(good)
    bad[-1] ^= 0xFF
    parsed = FrameParser().feed(bytes(bad) + good)
    assert isinstance(parsed[0].frame, CorruptFrame)
    assert parsed[-1].frame == AuxPacket(0x10, 0x2F, 0xFE, b"\x07\x0b")


def test_parser_skips_impossible_length_byte():
    parser = FrameParser()
    good = AuxPacket(0x10, 0x2F, 0xFE).encode()
    assert [p.frame for p in parser.feed(b"\x3b\x01" + good)] == [AuxPacket(0x10, 0x2F, 0xFE)]
    assert parser.out_of_frame_bytes == 2


def test_captured_starsense_frame_parses_with_escapes():
    parsed = FrameParser().feed(CAPTURED_FRAME)
    assert len(parsed) == 1
    frame = parsed[0].frame
    assert isinstance(frame, StarSenseFrame)
    assert len(frame.payload) == 800
    assert b"\x3b\x02\x02" in CAPTURED_FRAME  # the fixture really exercises escaping


def test_starsense_frame_round_trips_through_encoder_and_parser():
    payload = bytes(range(0x30, 0x48)) * 3  # contains 0x3b bytes
    encoded = StarSenseFrame(payload).encode()
    assert [p.frame for p in FrameParser().feed(encoded)] == [StarSenseFrame(payload)]


def test_starsense_frame_split_inside_an_escape_sequence():
    encoded = StarSenseFrame(b"\x3b\x3b\x01").encode()
    parser = FrameParser()
    split = encoded.index(b"\x3b\x02\x02") + 1
    assert parser.feed(encoded[:split]) == []
    assert [p.frame for p in parser.feed(encoded[split:])] == [StarSenseFrame(b"\x3b\x3b\x01")]


def test_angle_conversions():
    assert to_signed(0xFFFFFF, 24) == -1
    assert bam_to_degrees(bytes.fromhex("400000")) == pytest.approx(90.0)
    assert bam_to_degrees(bytes.fromhex("c000")) == pytest.approx(-90.0)
    assert bam_to_degrees(degrees_to_bam(-34.6037)) == pytest.approx(-34.6037, abs=1e-4)


def test_dms_formatting_keeps_sign_and_carries_rounding():
    assert format_dms(-0.5) == "-0\N{DEGREE SIGN}30'00.00\""
    assert format_dms(1.9999999999) == "2\N{DEGREE SIGN}00'00.00\""


def test_version_formatting():
    assert format_version(b"\x07\x0b") == "7.11"
    assert format_version(bytes((7, 11, 0x14, 0x05))) == "7.11.5125"
