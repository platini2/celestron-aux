"""Rebuilds the TCP byte streams of a classic pcap file (Ethernet, IPv4).

Just enough for test captures: each direction of each connection becomes one
byte string, in sequence order. Retransmitted bytes are kept once, as
Wireshark's "Follow TCP Stream" does, and a segment missing from the capture
leaves a gap that is simply skipped.
"""

import struct
from pathlib import Path
from typing import Dict, List, Tuple

Endpoint = Tuple[str, int]
Direction = Tuple[Endpoint, Endpoint]  # (source, destination)


def _records(data: bytes):
    magic = struct.unpack_from("<I", data)[0]
    if magic not in (0xA1B2C3D4, 0xA1B23C4D):
        raise ValueError("not a little-endian classic pcap file")
    if struct.unpack_from("<I", data, 20)[0] != 1:
        raise ValueError("only Ethernet captures are supported")
    offset = 24
    while offset + 16 <= len(data):
        captured = struct.unpack_from("<I", data, offset + 8)[0]
        yield data[offset + 16:offset + 16 + captured]
        offset += 16 + captured


def tcp_streams(path: Path) -> Dict[Direction, bytes]:
    segments: Dict[Direction, List[Tuple[int, bytes]]] = {}
    for frame in _records(path.read_bytes()):
        if len(frame) < 14 + 20 or frame[12:14] != b"\x08\x00":
            continue
        ip = frame[14:]
        header, total = (ip[0] & 0x0F) * 4, struct.unpack_from(">H", ip, 2)[0]
        if ip[9] != 6:
            continue
        tcp = ip[header:total]
        source = (".".join(map(str, ip[12:16])), struct.unpack_from(">H", tcp, 0)[0])
        destination = (".".join(map(str, ip[16:20])), struct.unpack_from(">H", tcp, 2)[0])
        sequence = struct.unpack_from(">I", tcp, 4)[0]
        payload = tcp[(tcp[12] >> 4) * 4:]
        if payload:
            segments.setdefault((source, destination), []).append((sequence, payload))
    streams = {}
    for direction, parts in segments.items():
        parts.sort(key=lambda part: part[0])
        out, next_sequence = bytearray(), None
        for sequence, payload in parts:
            if next_sequence is None or sequence > next_sequence:  # start, or a gap
                out += payload
                next_sequence = sequence + len(payload)
            elif sequence + len(payload) > next_sequence:          # partly new
                out += payload[next_sequence - sequence:]
                next_sequence = sequence + len(payload)
        streams[direction] = bytes(out)
    return streams
