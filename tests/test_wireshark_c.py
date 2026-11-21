"""The C dissector for Wireshark's source tree (wireshark/packet-celestron-aux.c).

The parity test needs a tshark built from Wireshark's source with this dissector
in it (see wireshark/README.md); point CELESTRON_AUX_TSHARK at it, e.g.

    CELESTRON_AUX_TSHARK=~/wireshark/build/run/tshark pytest tests/test_wireshark_c.py
"""

import os
import random
import struct
import subprocess
from pathlib import Path

import pytest

from celestron_aux import decoding, wireshark, wireshark_c
from celestron_aux.protocol import AuxPacket

from .test_wireshark import _big_integer, _parity_payloads

TSHARK = os.environ.get("CELESTRON_AUX_TSHARK")
needs_tshark = pytest.mark.skipif(not TSHARK, reason="set CELESTRON_AUX_TSHARK to a tshark built with the C dissector")
USER0 = 'uat:user_dlts:"User 0 (DLT=147)","celestron_aux","0","","0",""'


def test_generated_tables_are_up_to_date():
    source = wireshark_c.C_FILE.read_text(encoding="utf-8")
    assert wireshark_c.generated_source(source) == source, "run: python -m celestron_aux.wireshark_c"


def test_every_python_decoder_has_a_c_port():
    source = wireshark_c.C_FILE.read_text(encoding="utf-8")
    assert wireshark_c.unported_decoders(source) == []
    names = wireshark_c.c_decoder_names(source)
    assert len(names) == len(set(names))


def run_tshark(pcap: Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(Path(TSHARK).expanduser()), "-o", USER0, "-r", str(pcap), *arguments],
                          capture_output=True, timeout=300)


@needs_tshark
def test_c_decoders_match_python(tmp_path, monkeypatch):
    rng = random.Random(1234)
    packets = []
    for (device, command), decoder in sorted(decoding._DECODERS.items(), key=lambda i: (i[0][0] or -1, i[0][1])):
        for target in ([device] if device is not None else [0x01, 0xB3, 0x10]):
            for payload in _parity_payloads(rng):
                if _big_integer(decoder.__name__, payload):
                    continue
                packets.append(AuxPacket(0x20, target, command, payload))
                packets.append(AuxPacket(target, 0x20, command, payload))
    for slot in range(7):
        packets.append(AuxPacket(0x20, 0xBD, 0x08, bytes([slot])))
        for value in (0.0, 1.2, -0.7, 359.5, 1.7e9, float("nan")):
            packets.append(AuxPacket(0xBD, 0x20, 0x08, struct.pack(">f", value)))

    monkeypatch.setattr(decoding, "_last_calibration_slot", None)
    expected = [(d.value, d.malformed) for d in map(decoding.describe_packet, packets)]

    pcap = tmp_path / "parity.pcap"
    pcap.write_bytes(wireshark.pcap_bytes((i * 0.001, p.encode()) for i, p in enumerate(packets)))
    result = run_tshark(pcap, "-T", "fields", "-E", "occurrence=f", "-e", "celestron_aux.value",
                        "-e", "_ws.expert.message")
    assert result.returncode == 0, result.stderr.decode()
    lines = result.stdout.decode("utf-8").splitlines()
    assert len(lines) == len(packets)
    mismatches = []
    for packet, line, (value, malformed) in zip(packets, lines, expected):
        c_value, message = (line.split("\t") + [""])[:2]
        c_malformed = "known format" in message
        if (c_value, c_malformed) != (value, malformed):
            mismatches.append(f"{packet.source:02x}->{packet.destination:02x} cmd {packet.command:02x} "
                              f"data {packet.data.hex()}: python {value!r}/{malformed}, C {c_value!r}/{c_malformed}")
    assert not mismatches, f"{len(mismatches)} of {len(packets)} differ:\n" + "\n".join(mismatches[:40])


@needs_tshark
def test_c_dissector_reassembles_live_tcp_traffic(tmp_path):
    from .test_wireshark import REPLY, REQUEST, tcp_pcap
    from celestron_aux.protocol.packets import StarSenseFrame

    goto = AuxPacket(0x20, 0x10, 0x02, b"\x40\x00\x00").encode()
    ack = AuxPacket(0x10, 0x20, 0x02).encode()
    star_list = StarSenseFrame(b"\x3b" * 8 + bytes(range(8))).encode()
    segments = [(False, REQUEST + goto[:3]), (False, goto[3:]), (True, REPLY + ack + b"\x00\x11"),
                (True, star_list[:7]), (True, star_list[7:])]
    pcap = tmp_path / "live.pcap"
    pcap.write_bytes(tcp_pcap(segments))
    result = run_tshark(pcap, "-d", "tcp.port==2000,celestron_aux", "-Y", "celestron_aux",
                        "-T", "fields", "-e", "frame.number", "-e", "celestron_aux.cmd_name",
                        "-e", "celestron_aux.value", "-e", "celestron_aux.starsense.stars")
    assert result.returncode == 0, result.stderr.decode()
    rows = [line.split("\t") for line in result.stdout.decode("utf-8").splitlines()]
    assert rows == [
        ["1", "MTR_GET_POSITION", "", ""],
        ["2", "MTR_GOTO_FAST", "90°00'00.00\"", ""],
        ["3", "MTR_GET_POSITION,MTR_GOTO_FAST", "90°00'00.00\"", ""],
        ["5", "", "", "2"],
    ]


@needs_tshark
def test_c_dissector_heuristic_finds_aux_on_any_port(tmp_path):
    from .test_wireshark import REPLY, REQUEST, tcp_pcap

    pcap = tmp_path / "live.pcap"
    pcap.write_bytes(tcp_pcap([(False, REQUEST), (True, REPLY)]))
    plain = run_tshark(pcap, "-Y", "celestron_aux", "-T", "fields", "-e", "frame.number")
    assert plain.returncode == 0 and plain.stdout.strip() == b""   # off by default
    heuristic = run_tshark(pcap, "--enable-heuristic", "celestron_aux_tcp", "-Y", "celestron_aux",
                           "-T", "fields", "-e", "celestron_aux.cmd_name")
    assert heuristic.stdout.decode().split() == ["MTR_GET_POSITION", "MTR_GET_POSITION"]


@needs_tshark
def test_c_dissector_survives_random_bytes(tmp_path):
    rng = random.Random(99)
    records = [(i * 0.001, bytes(rng.getrandbits(8) for _ in range(rng.randint(1, 300)))) for i in range(3000)]
    records += [(3.0 + i * 0.001, b"\x3b" + bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 40))))
                for i in range(3000)]
    records += [(6.0 + i * 0.001, b"\x3c" + bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 40))))
                for i in range(3000)]
    pcap = tmp_path / "fuzz.pcap"
    pcap.write_bytes(wireshark.pcap_bytes(records))
    result = run_tshark(pcap, "-V")
    assert result.returncode == 0, result.stderr.decode()[-2000:]
