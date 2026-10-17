"""Wire-level AUX protocol: frame layout, checksums, escaping and stream parsing.

This package knows nothing about device names or what payloads mean; see
``celestron_aux.catalog`` and ``celestron_aux.decoding`` for that.
"""

from .framing import FrameParser, Parsed
from .packets import (
    AUX_PREAMBLE,
    STARSENSE_PREAMBLE,
    AuxPacket,
    CorruptFrame,
    Frame,
    StarSenseFrame,
    checksum,
)

__all__ = [
    "AUX_PREAMBLE",
    "STARSENSE_PREAMBLE",
    "AuxPacket",
    "CorruptFrame",
    "Frame",
    "FrameParser",
    "Parsed",
    "StarSenseFrame",
    "checksum",
]
