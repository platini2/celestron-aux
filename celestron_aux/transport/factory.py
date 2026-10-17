"""Builds a transport from a connection mode and target."""

from __future__ import annotations

from pathlib import Path

from .base import Transport
from .tcp import DEFAULT_PORT

MODES = ("wifi", "serial", "hc", "playback")
LIVE_MODES = ("wifi", "serial", "hc")  # modes that talk to a real mount


def create_transport(mode: str, target: str, *, tcp_port: int = DEFAULT_PORT,
                     playback_speed: float = 1.0) -> Transport:
    """Create (but do not open) the transport for ``mode``.

    ``target`` is an IP address for wifi, a serial port for serial and hc, and
    a capture file for playback.
    """
    if not target:
        raise ValueError("a connection target is required")
    if mode == "wifi":
        from .tcp import TcpTransport
        return TcpTransport(target, tcp_port)
    if mode == "serial":
        from .serial_port import SerialBusTransport
        return SerialBusTransport(target)
    if mode == "hc":
        from .serial_port import HandControllerTransport
        return HandControllerTransport(target)
    if mode == "playback":
        from .playback import PlaybackTransport
        return PlaybackTransport(Path(target), speed=playback_speed)
    raise ValueError(f"unknown connection mode {mode!r}")
