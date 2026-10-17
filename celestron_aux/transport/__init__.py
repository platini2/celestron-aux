"""Byte transports that connect the scanner to a mount (or a capture file)."""

from .base import Transport, TransportClosed

__all__ = ["Transport", "TransportClosed"]
