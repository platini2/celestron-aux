import shutil
import struct
import subprocess
import urllib.error
import urllib.request

import pytest

from celestron_aux import catalog, wireshark
from celestron_aux.app import AppConfig
from celestron_aux.cli import main
from celestron_aux.protocol import AuxPacket
from celestron_aux.protocol.packets import StarSenseFrame
from celestron_aux.web import WebServer, WebSession

REQUEST = AuxPacket(0x20, 0x10, 0x01).encode()
REPLY = AuxPacket(0x10, 0x20, 0x01, b"\x40\x00\x00").encode()


def read_pcap(data):
    magic, major, minor, _, _, _, linktype = struct.unpack_from("<IHHiIII", data)
    assert (magic, major, minor, linktype) == (0xA1B2C3D4, 2, 4, wireshark.LINKTYPE_USER0)
    records, offset = [], 24
    while offset < len(data):
        seconds, micros, captured, original = struct.unpack_from("<IIII", data, offset)
        assert captured == original
        offset += 16
        records.append((seconds + micros / 1e6, data[offset:offset + captured]))
        offset += captured
    return records


@pytest.fixture
def capture(tmp_path):
    both = REQUEST + REPLY
    bad = bytearray(AuxPacket(0x0D, 0xB0, 0x01).encode())
    bad[-1] ^= 0xFF
    star_list = StarSenseFrame(bytes(range(16)) + b"\x3b" * 8).encode()
    path = tmp_path / "raw.txt"
    path.write_text(
        f"1.250000 {both[:4].hex()}\n"
        f"1.500000 {both[4:].hex()}\n"
        "#MARK 2.0 pressed Align\n"
        f"{bytes(bad).hex()}\n"
        f"3.0 ffff{star_list.hex()}\n",
        encoding="ascii",
    )
    return path, bytes(bad), star_list


def test_capture_becomes_one_record_per_frame(capture, tmp_path):
    path, bad, star_list = capture
    count = wireshark.capture_to_pcap(path, tmp_path / "out.pcap")
    records = read_pcap((tmp_path / "out.pcap").read_bytes())
    assert count == len(records) == 5
    # A frame split over two chunks takes the time of the chunk that completed it.
    assert records[0] == (pytest.approx(1.5), REQUEST)
    assert records[1] == (pytest.approx(1.5), REPLY)
    assert records[2] == (pytest.approx(2.0), b"#MARK pressed Align")
    # No timestamp: the previous one. Corrupt frames are kept; noise bytes are not.
    assert records[3] == (pytest.approx(2.0), bad)
    assert records[4] == (pytest.approx(3.0), star_list)


def test_dissector_has_catalog_names_including_yours(isolated_user_catalog):
    isolated_user_catalog.set_device(0x99, 'My "odd" accessory')
    isolated_user_catalog.set_command(0x10, 0x77, "MTR_SOMETHING")
    lua = wireshark.dissector_lua()
    assert '[0x10] = "AZM MC",' in lua
    assert '[0x99] = "My \\"odd\\" accessory",' in lua
    assert '[0x77] = "MTR_SOMETHING",' in lua
    assert '[0xbd] = {' in lua and '"SS2_SET_ASPA"' in lua
    assert f"[0x{catalog.Addr.SCANNER:02x}]" not in lua.split("local CONTROLLERS")[0].split("local GENERIC")[1]
    assert lua.count("{") == lua.count("}")


def test_cli_exports(capture, tmp_path, capsys):
    path, _, _ = capture
    pcap, lua, docs = tmp_path / "out.pcap", tmp_path / "aux.lua", tmp_path / "docs" / "protocol.md"
    code = main(["--catalog", str(tmp_path / "names.json"), "--export-pcap", str(path), str(pcap),
                 "--export-dissector", str(lua), "--export-docs", str(docs)])
    assert code == 0
    assert len(read_pcap(pcap.read_bytes())) == 5
    assert "Proto(\"celestron_aux\"" in lua.read_text()
    assert docs.read_text().startswith("# Celestron AUX protocol reference")
    assert "5 records" in capsys.readouterr().out


def test_cli_export_reports_a_bad_capture(tmp_path):
    bad = tmp_path / "bad.txt"
    bad.write_text("not hex at all\n")
    assert main(["--export-pcap", str(bad), str(tmp_path / "out.pcap")]) == 2


def test_web_downloads_pcap_and_dissector(capture, tmp_path):
    path, _, _ = capture
    session = WebSession(AppConfig(image_dir=tmp_path, raw_path=path, keepalive=False))
    server = WebServer(session, "127.0.0.1", 0)
    server.start_in_background()
    host, port = server.address
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/download/pcap", timeout=5) as response:
            assert 'filename="raw.pcap"' in response.headers["Content-Disposition"]
            assert len(read_pcap(response.read())) == 5
        with urllib.request.urlopen(f"http://{host}:{port}/api/download/dissector", timeout=5) as response:
            assert b"DissectorTable" in response.read()
        path.unlink()
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(f"http://{host}:{port}/api/download/pcap", timeout=5)
        assert missing.value.code == 404
    finally:
        server.close()
        session.close()


@pytest.mark.skipif(shutil.which("tshark") is None, reason="Wireshark's tshark is not installed")
def test_tshark_decodes_the_export(capture, tmp_path):
    path, _, _ = capture
    wireshark.capture_to_pcap(path, tmp_path / "out.pcap")
    wireshark.write_dissector(tmp_path / "aux.lua")
    result = subprocess.run(["tshark", "-X", f"lua_script:{tmp_path / 'aux.lua'}", "-r", str(tmp_path / "out.pcap")],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    assert "CPWI -> AZM MC  MTR_GET_POSITION (0x01)" in lines[0]
    assert "AZM MC -> CPWI  MTR_GET_POSITION (0x01) 90\u00b000'00.00\"" in lines[1]
    assert "MARK: pressed Align" in lines[2]
    assert "GPS_GET_LAT (0x01)  [bad checksum]" in lines[3]
    assert "StarSense star list, 3 stars" in lines[4]


def tcp_pcap(segments):
    """An Ethernet pcap of TCP segments: (from_mount, payload), mount at 10.0.0.1:2000."""
    out = bytearray(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, 65535, 1))
    sequence = {True: 1000, False: 5000}
    for index, (from_mount, payload) in enumerate(segments):
        mount, phone = bytes([10, 0, 0, 1]), bytes([10, 0, 0, 2])
        src, dst = (mount, phone) if from_mount else (phone, mount)
        sport, dport = (2000, 50000) if from_mount else (50000, 2000)
        ack = sequence[not from_mount]
        tcp = struct.pack(">HHIIBBHHH", sport, dport, sequence[from_mount], ack, 5 << 4, 0x18, 65535, 0, 0)
        sequence[from_mount] += len(payload)
        ip = struct.pack(">BBHHHBBH4s4s", 0x45, 0, 20 + len(tcp) + len(payload), index, 0, 64, 6, 0, src, dst)
        ethernet = b"\x02\x00\x00\x00\x00\x02" + b"\x02\x00\x00\x00\x00\x01" + b"\x08\x00"
        frame = ethernet + ip + tcp + payload
        out += struct.pack("<IIII", index, 0, len(frame), len(frame)) + frame
    return bytes(out)


@pytest.mark.skipif(shutil.which("tshark") is None, reason="Wireshark's tshark is not installed")
def test_tshark_decodes_live_wifi_traffic_on_port_2000(tmp_path):
    goto = AuxPacket(0x20, 0x10, 0x02, b"\x40\x00\x00").encode()
    ack = AuxPacket(0x10, 0x20, 0x02).encode()
    star_list = StarSenseFrame(b"\x3b" * 8 + bytes(range(8))).encode()
    segments = [
        (False, REQUEST + goto[:3]),        # one whole frame and the start of the next
        (False, goto[3:]),                  # the rest of it
        (True, REPLY + ack + b"\x00\x11"),  # two frames in one segment, then noise
        (True, star_list[:7]),              # a StarSense frame cut inside an escape
        (True, star_list[7:]),
    ]
    (tmp_path / "live.pcap").write_bytes(tcp_pcap(segments))
    wireshark.write_dissector(tmp_path / "aux.lua")
    result = subprocess.run(
        ["tshark", "-X", f"lua_script:{tmp_path / 'aux.lua'}", "-r", str(tmp_path / "live.pcap"),
         "-T", "fields", "-e", "frame.number", "-e", "celestron_aux.cmd_name", "-e", "celestron_aux.starsense.stars",
         "-Y", "celestron_aux"],
        capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    rows = [line.split("\t") for line in result.stdout.splitlines()]
    assert rows == [
        ["1", "MTR_GET_POSITION", ""],
        ["2", "MTR_GOTO_FAST", ""],
        ["3", "MTR_GET_POSITION,MTR_GOTO_FAST", ""],
        ["5", "", "2"],
    ]


# --- payload decoding: the Lua port must match decoding.py --------------------

def test_every_python_decoder_has_a_lua_port():
    assert wireshark.unported_decoders() == []
    assert len(wireshark.lua_decoder_names()) == len(set(wireshark.lua_decoder_names()))


def _parity_payloads(rng):
    """Payloads that exercise every branch: random bytes, zeros, status bytes, real floats."""
    payloads = [bytes([b]) for b in (0, 1, 2, 3, 4, 5, 9, 0x7F, 0x80, 0xAB, 0xFF)]
    payloads += [bytes(n) for n in range(2, 13)] + [b"\xff" * n for n in range(2, 13)]
    payloads += [bytes(rng.getrandbits(8) for _ in range(rng.randint(1, 12))) for _ in range(40)]
    for status in (0, 1, 2, 4):
        for _ in range(4):
            ra, dec = rng.uniform(-7, 7), rng.uniform(-1.6, 1.6)
            payloads.append(bytes([status]) + struct.pack(">ff", ra, dec) + bytes(rng.getrandbits(8) for _ in range(3)))
            payloads.append(bytes([status, rng.randint(0, 100)]) + struct.pack(">f", rng.uniform(-90, 90)) + b"\x00\x00\x00")
    for value in (0.0, -0.0, 1.5, -1.5, 359.99, 1.7e9, 1.7e9 + 1e6, 2.0e9, 1e20, -5.0, 0.004):
        payloads.append(struct.pack(">f", value))
        payloads.append(struct.pack(">ff", value, -value))
    payloads += [bytes.fromhex(h) for h in ("7f800000", "ff800000", "7fc00000", "00000001", "0613", "1189", "05241842")]
    return payloads


def _big_integer(name, payload):
    # Python prints integers of any size; Lua's stop at 2^63. Real replies are short.
    return name in ("_motor_model", "_gps_year") and len(payload) > 6


@pytest.mark.skipif(shutil.which("tshark") is None, reason="Wireshark's tshark is not installed")
def test_lua_decoders_match_python(tmp_path, monkeypatch):
    import random

    from celestron_aux import decoding

    rng = random.Random(1234)
    packets = []
    for (device, command), decoder in sorted(decoding._DECODERS.items(), key=lambda i: (i[0][0] or -1, i[0][1])):
        targets = [device] if device is not None else [0x01, 0xB3, 0x10]
        for target in targets:
            for payload in _parity_payloads(rng):
                if _big_integer(decoder.__name__, payload):
                    continue
                packets.append(AuxPacket(0x20, target, command, payload))   # request
                packets.append(AuxPacket(target, 0x20, command, payload))   # reply
    # The calibration data decoder remembers the requested slot: walk all the slots.
    for slot in range(7):
        packets.append(AuxPacket(0x20, 0xBD, 0x08, bytes([slot])))
        for value in (0.0, 1.2, -0.7, 359.5, 1.7e9, float("nan")):
            packets.append(AuxPacket(0xBD, 0x20, 0x08, struct.pack(">f", value)))

    monkeypatch.setattr(decoding, "_last_calibration_slot", None)
    expected = []
    for packet in packets:
        description = decoding.describe_packet(packet)
        expected.append((description.value, description.malformed))

    (tmp_path / "parity.pcap").write_bytes(wireshark.pcap_bytes((i * 0.001, p.encode()) for i, p in enumerate(packets)))
    wireshark.write_dissector(tmp_path / "aux.lua")
    result = subprocess.run(
        ["tshark", "-X", f"lua_script:{tmp_path / 'aux.lua'}", "-r", str(tmp_path / "parity.pcap"),
         "-T", "fields", "-E", "occurrence=f", "-e", "celestron_aux.value", "-e", "celestron_aux.unexpected_format"],
        capture_output=True, timeout=300)
    assert result.returncode == 0, result.stderr.decode()
    lines = result.stdout.decode("utf-8").splitlines()
    assert len(lines) == len(packets)
    mismatches = []
    for packet, line, (value, malformed) in zip(packets, lines, expected):
        lua_value, lua_flag = (line.split("\t") + [""])[:2]
        lua_malformed = lua_flag in ("True", "1")
        if (lua_value, lua_malformed) != (value, malformed):
            mismatches.append(f"{packet.source:02x}->{packet.destination:02x} cmd {packet.command:02x} "
                              f"data {packet.data.hex()}: python {value!r}/{malformed}, lua {lua_value!r}/{lua_malformed}")
    assert not mismatches, f"{len(mismatches)} of {len(packets)} differ:\n" + "\n".join(mismatches[:40])
