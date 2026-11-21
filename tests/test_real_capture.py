"""A real Wi-Fi capture: CPWI talking to a NexStar SLT through its SkyPortal module.

tests/fixtures/cpwi_wifi_nexstar_slt.pcap holds only the AUX traffic (TCP port
2000) of 58 seconds, with the hardware half of both MAC addresses blanked. On
the bus: two motors, a StarSense camera and its hand controller, a focuser, a
dew heater controller and a GPS. A TCP segment from the module was not
captured, so one stretch has to be resynchronised after a bad checksum, and the
StarSense hand controller twice sends a frame with a zero checksum.

The Python test runs everywhere. The Lua and C ones run when their tshark is
available, and require them to find the same frames, names and values.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from celestron_aux import decoding, wireshark
from celestron_aux.protocol import AuxPacket, CorruptFrame
from celestron_aux.protocol.framing import FrameParser

from .pcap_streams import tcp_streams

CAPTURE = Path(__file__).parent / "fixtures" / "cpwi_wifi_nexstar_slt.pcap"
MOUNT = ("192.168.2.96", 2000)


def python_frames(monkeypatch):
    """{(CPWI's port, "to"/"from" mount): ([(route, name, value)], corrupt frames)}."""
    monkeypatch.setattr(decoding, "_last_calibration_slot", None)
    result = {}
    for (source, destination), data in sorted(tcp_streams(CAPTURE).items()):
        key = (destination[1], "from") if source == MOUNT else (source[1], "to")
        frames, corrupt = [], []
        for parsed in FrameParser().feed(data):
            if isinstance(parsed.frame, AuxPacket):
                packet, d = parsed.frame, decoding.describe_packet(parsed.frame)
                assert not d.malformed, f"unexpected format: {parsed.raw.hex()}"
                frames.append((f"{packet.source:02x}>{packet.destination:02x} {packet.command:02x}", d.command, d.value))
            elif isinstance(parsed.frame, CorruptFrame):
                corrupt.append(parsed.raw.hex())
        result[key] = (frames, corrupt)
    return result


def test_python_decodes_the_capture(monkeypatch):
    frames = python_frames(monkeypatch)
    counts = {key: (len(f), len(c)) for key, (f, c) in frames.items()}
    assert counts == {
        (62846, "to"): (451, 0), (62846, "from"): (420, 3),
        (51610, "to"): (81, 0), (51610, "from"): (62, 0),
        (63926, "to"): (27, 0), (63926, "from"): (15, 0),
    }
    replies = [frame for (_, direction), (f, _) in frames.items() if direction == "from" for frame in f]
    versions = {route[:2]: value for route, name, value in replies if route.endswith(" fe") and value}
    # The dew heater controller answers version requests at its second address, 0xbb.
    assert versions == {"10": "5.18", "11": "5.18", "12": "7.16.9300", "bb": "1.1.1270",
                        "b0": "11.1", "b4": "1.2.13343"}
    values = {(route, value) for route, _, value in replies}
    assert ("10>20 05", "0x0783 (Nexstar SLT)") in values
    assert ("12>20 2c", "Limits 2368 to 44990") in values
    assert ("17>20 04", "2 A - 10 A") in values
    assert any(route == "17>20 18" and value.endswith("Hum: 53%") for route, value in values)
    # The StarSense hand controller's "stop" to the ALT motor, twice with checksum 00 instead of b9.
    corrupt = frames[(62846, "from")][1]
    assert corrupt.count("3b040e11240000") == 2


def dissector_frames(command):
    """The same structure as python_frames, from tshark's JSON."""
    result = subprocess.run([*command, "-r", str(CAPTURE), "-o", "tcp.reassemble_out_of_order:TRUE",
                             "-Y", "celestron_aux", "-T", "json", "--no-duplicate-keys",
                             "-j", "frame tcp celestron_aux"], capture_output=True, timeout=300)
    assert result.returncode == 0, result.stderr.decode()[-2000:]
    frames = {}
    for packet in json.loads(result.stdout):
        layers = packet["_source"]["layers"]
        tcp = layers["tcp"]
        key = (int(tcp["tcp.dstport"]), "from") if tcp["tcp.srcport"] == "2000" else (int(tcp["tcp.srcport"]), "to")
        items = layers["celestron_aux"]
        for item in items if isinstance(items, list) else [items]:
            if "celestron_aux.src" not in item:
                continue
            good, bad = frames.setdefault(key, ([], []))
            ok = (item["celestron_aux.checksum.status"] != "0") if "celestron_aux.checksum.status" in item \
                else item.get("celestron_aux.checksum_ok") in ("1", "True", "true")
            route = (f"{int(item['celestron_aux.src'], 16):02x}>{int(item['celestron_aux.dst'], 16):02x} "
                     f"{int(item['celestron_aux.cmd'], 16):02x}")
            if ok:
                good.append((route, item["celestron_aux.cmd_name"], item.get("celestron_aux.value", "")))
            else:
                bad.append(route)
    return frames


def assert_same_as_python(dissector, python):
    for key, (frames, corrupt) in python.items():
        got_frames, got_bad = dissector.get(key, ([], []))
        assert got_frames == frames, f"{key}: frames differ"
        assert len(got_bad) == len(corrupt), f"{key}: {len(got_bad)} bad checksums, Python has {len(corrupt)}"


@pytest.mark.skipif(shutil.which("tshark") is None, reason="Wireshark's tshark is not installed")
def test_lua_dissector_matches_python_on_the_capture(tmp_path, monkeypatch):
    wireshark.write_dissector(tmp_path / "aux.lua")
    lua = dissector_frames(["tshark", "-X", f"lua_script:{tmp_path / 'aux.lua'}"])
    assert_same_as_python(lua, python_frames(monkeypatch))


@pytest.mark.skipif(not os.environ.get("CELESTRON_AUX_TSHARK"),
                    reason="set CELESTRON_AUX_TSHARK to a tshark built with the C dissector")
def test_c_dissector_matches_python_on_the_capture(monkeypatch):
    tshark = str(Path(os.environ["CELESTRON_AUX_TSHARK"]).expanduser())
    c = dissector_frames([tshark, "-d", "tcp.port==2000,celestron_aux"])
    assert_same_as_python(c, python_frames(monkeypatch))
