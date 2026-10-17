"""Frame types carried on the AUX bus and their binary encoding.

Two frame formats share the bus:

* **AUX packet** (preamble ``0x3B``)::

      3B | len | src | dst | cmd | data... | checksum

  ``len`` counts ``src``, ``dst``, ``cmd`` and the data bytes, so it is
  ``3 + len(data)``. The checksum is the two's complement of the sum of every
  byte from ``len`` through the last data byte.

* **StarSense bulk frame** (preamble ``0x3C``), used by the StarSense hand
  controller to move star lists::

      3C | len (4 bytes, big-endian) | payload... | checksum

  Every ``0x3B`` byte inside the length and payload is escaped as
  ``3B 02 02`` so it cannot be mistaken for an AUX preamble. The checksum is
  computed over the *unescaped* length and payload and is sent unescaped.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

AUX_PREAMBLE = 0x3B
STARSENSE_PREAMBLE = 0x3C

AUX_HEADER_LENGTH = 3  # src, dst, cmd: the minimum value of the length byte
AUX_MAX_DATA = 0xFF - AUX_HEADER_LENGTH

ESCAPED_PREAMBLE = bytes((AUX_PREAMBLE, 0x02, 0x02))


def checksum(body: bytes) -> int:
    """Return the AUX checksum of ``body``: the negated byte sum, modulo 256."""
    return -sum(body) & 0xFF


def escape(body: bytes) -> bytes:
    """Escape AUX preamble bytes inside a StarSense frame body."""
    return body.replace(bytes((AUX_PREAMBLE,)), ESCAPED_PREAMBLE)


def _check_byte(name: str, value: int) -> None:
    if not 0 <= value <= 0xFF:
        raise ValueError(f"{name} must be a byte (0-255), got {value!r}")


@dataclass(frozen=True)
class AuxPacket:
    """A standard AUX bus packet."""

    source: int
    destination: int
    command: int
    data: bytes = b""

    def __post_init__(self) -> None:
        _check_byte("source", self.source)
        _check_byte("destination", self.destination)
        _check_byte("command", self.command)
        if len(self.data) > AUX_MAX_DATA:
            raise ValueError(f"data is limited to {AUX_MAX_DATA} bytes, got {len(self.data)}")

    def encode(self) -> bytes:
        body = bytes((AUX_HEADER_LENGTH + len(self.data), self.source, self.destination, self.command))
        body += self.data
        return bytes((AUX_PREAMBLE,)) + body + bytes((checksum(body),))


@dataclass(frozen=True)
class StarSenseFrame:
    """A StarSense bulk-data frame; the payload is a list of 8-byte star records."""

    payload: bytes

    def encode(self) -> bytes:
        body = len(self.payload).to_bytes(4, "big") + self.payload
        return bytes((STARSENSE_PREAMBLE,)) + escape(body) + bytes((checksum(body),))


@dataclass(frozen=True)
class CorruptFrame:
    """Bytes that looked like a frame but failed validation."""

    raw: bytes
    reason: str


Frame = Union[AuxPacket, StarSenseFrame, CorruptFrame]
