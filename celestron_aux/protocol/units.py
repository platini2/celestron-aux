"""Numeric conversions used by AUX payloads."""

from __future__ import annotations


def to_signed(value: int, bits: int) -> int:
    """Interpret ``value`` as a two's-complement integer of ``bits`` bits."""
    if value & (1 << (bits - 1)):
        return value - (1 << bits)
    return value


def bam_to_degrees(raw: bytes) -> float:
    """Convert a signed big-endian binary angle (BAM) of any width to degrees."""
    if not raw:
        raise ValueError("an angle needs at least one byte")
    bits = 8 * len(raw)
    return to_signed(int.from_bytes(raw, "big"), bits) * 360.0 / (1 << bits)


def degrees_to_bam(degrees: float, size: int = 3) -> bytes:
    """Convert degrees to a big-endian binary angle of ``size`` bytes."""
    full_circle = 1 << (8 * size)
    return (round(degrees / 360.0 * full_circle) % full_circle).to_bytes(size, "big")


def format_dms(degrees: float) -> str:
    """Format an angle as signed degrees, minutes and seconds (0.01" resolution)."""
    sign = "-" if degrees < 0 else ""
    hundredths = round(abs(degrees) * 360_000)
    whole_degrees, remainder = divmod(hundredths, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    return f"{sign}{whole_degrees}\N{DEGREE SIGN}{minutes:02d}'{remainder / 100:05.2f}\""


def format_hms(degrees: float) -> str:
    """Format a right ascension given in degrees as hours, minutes and seconds (0.01 s resolution)."""
    hundredths = round((degrees % 360) / 15 * 360_000) % (24 * 360_000)  # 24h00m00s wraps to 0h
    hours, remainder = divmod(hundredths, 360_000)
    minutes, remainder = divmod(remainder, 6_000)
    return f"{hours}h{minutes:02d}m{remainder / 100:05.2f}s"


def parse_version(text: str) -> bytes:
    """The reverse of format_version: "11.1" -> 2 bytes, "7.16.9300" -> 4 bytes."""
    parts = text.strip().split(".")
    try:
        numbers = [int(part) for part in parts]
    except ValueError:
        numbers = []
    if len(numbers) == 2 and all(0 <= n <= 255 for n in numbers):
        return bytes(numbers)
    if len(numbers) == 3 and all(0 <= n <= 255 for n in numbers[:2]) and 0 <= numbers[2] <= 0xFFFF:
        return bytes(numbers[:2]) + numbers[2].to_bytes(2, "big")
    raise ValueError("a version is major.minor or major.minor.build, e.g. 11.1 or 7.16.9300 "
                     "(major and minor 0-255, build 0-65535)")


def format_version(data: bytes) -> str:
    """Format a firmware version reply: ``major.minor`` or ``major.minor.build``."""
    if len(data) < 4:
        return ".".join(str(byte) for byte in data)
    return f"{data[0]}.{data[1]}.{int.from_bytes(data[2:4], 'big')}"
