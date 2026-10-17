"""Incremental parser that turns a raw byte stream into frames.

Bytes arrive in arbitrary chunks (TCP segments, serial reads), so the parser
keeps a buffer and only emits frames once they are complete. Bytes that are not
part of any frame are counted in ``out_of_frame_bytes`` and discarded.

When a candidate frame fails its checksum the parser reports it and then skips
only the preamble byte, so it can resynchronise on a real frame that may start
inside the bad one.
"""

from __future__ import annotations

from typing import List, NamedTuple, Optional, Tuple

from .packets import (
    AUX_HEADER_LENGTH,
    AUX_PREAMBLE,
    ESCAPED_PREAMBLE,
    STARSENSE_PREAMBLE,
    AuxPacket,
    CorruptFrame,
    Frame,
    StarSenseFrame,
    checksum,
)

_PREAMBLES = (AUX_PREAMBLE, STARSENSE_PREAMBLE)
DEFAULT_MAX_STARSENSE_PAYLOAD = 0xFFFF


class Parsed(NamedTuple):
    """A frame together with the exact bytes it was parsed from."""

    frame: Frame
    raw: bytes


# (bytes consumed, parsed item or None for noise); None overall means "need more data".
_Step = Optional[Tuple[int, Optional[Parsed]]]


class FrameParser:
    """Stateful stream parser. Not thread-safe: feed it from a single thread."""

    def __init__(self, max_starsense_payload: int = DEFAULT_MAX_STARSENSE_PAYLOAD) -> None:
        self._buffer = bytearray()
        self._max_starsense_payload = max_starsense_payload
        self.out_of_frame_bytes = 0

    def feed(self, data: bytes) -> List[Parsed]:
        """Add received bytes and return every frame that is now complete."""
        self._buffer.extend(data)
        parsed: List[Parsed] = []
        while self._buffer:
            first = self._buffer[0]
            if first == AUX_PREAMBLE:
                step = self._parse_aux()
            elif first == STARSENSE_PREAMBLE:
                step = self._parse_starsense()
            else:
                self._discard_noise()
                continue
            if step is None:
                break
            consumed, item = step
            del self._buffer[:consumed]
            if item is None:
                self.out_of_frame_bytes += consumed
            else:
                parsed.append(item)
        return parsed

    def _discard_noise(self) -> None:
        buffer = self._buffer
        end = next((i for i, byte in enumerate(buffer) if byte in _PREAMBLES), len(buffer))
        self.out_of_frame_bytes += end
        del buffer[:end]

    def _parse_aux(self) -> _Step:
        buffer = self._buffer
        if len(buffer) < 2:
            return None
        length = buffer[1]
        if length < AUX_HEADER_LENGTH:
            return 1, None
        total = length + 3  # preamble + length byte + checksum
        if len(buffer) < total:
            return None
        raw = bytes(buffer[:total])
        body, received = raw[1:-1], raw[-1]
        expected = checksum(body)
        if received != expected:
            return 1, Parsed(CorruptFrame(raw, _checksum_reason(received, expected)), raw)
        packet = AuxPacket(source=body[1], destination=body[2], command=body[3], data=body[4:])
        return total, Parsed(packet, raw)

    def _parse_starsense(self) -> _Step:
        buffer = self._buffer
        header = _read_unescaped(buffer, 1, 4)
        if header is None:
            return None
        length_bytes, position = header
        length = int.from_bytes(length_bytes, "big")
        if length > self._max_starsense_payload:
            return 1, None
        body = _read_unescaped(buffer, position, length)
        if body is None:
            return None
        payload, position = body
        if position >= len(buffer):
            return None
        received = buffer[position]
        total = position + 1
        raw = bytes(buffer[:total])
        expected = checksum(length_bytes + payload)
        if received != expected:
            return 1, Parsed(CorruptFrame(raw, _checksum_reason(received, expected)), raw)
        return total, Parsed(StarSenseFrame(payload), raw)


def _checksum_reason(received: int, expected: int) -> str:
    return f"checksum 0x{received:02x}, expected 0x{expected:02x}"


def _read_unescaped(buffer: bytearray, start: int, count: int) -> Optional[Tuple[bytes, int]]:
    """Read ``count`` logical bytes from ``start``, undoing ``3B 02 02`` escapes.

    Returns the bytes and the index just past them, or None if the buffer does
    not hold enough data yet.
    """
    out = bytearray()
    index = start
    while len(out) < count:
        if index >= len(buffer):
            return None
        byte = buffer[index]
        if byte == AUX_PREAMBLE:
            if index + len(ESCAPED_PREAMBLE) > len(buffer):
                return None
            escaped = buffer[index:index + len(ESCAPED_PREAMBLE)] == ESCAPED_PREAMBLE
            index += len(ESCAPED_PREAMBLE) if escaped else 1
        else:
            index += 1
        out.append(byte)
    return bytes(out), index
