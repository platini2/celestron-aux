"""Common interface for every way of reaching the AUX bus."""

from __future__ import annotations

from abc import ABC, abstractmethod
from types import TracebackType
from typing import Optional, Type


class TransportClosed(ConnectionError):
    """The other end closed the connection, or a capture file ran out."""


class Transport(ABC):
    """A bidirectional byte channel to the AUX bus.

    ``read`` must return within roughly ``poll_interval`` seconds (with ``b""``
    if nothing arrived) so the receive loop can notice shutdown requests.
    """

    #: True when frames we send are also received back (a shared bus does
    #: this). Otherwise the bus layer reports sent frames itself.
    echoes_tx: bool = False

    #: Whether keep-alive traffic should be on by default for this transport.
    keepalive_by_default: bool = False

    def __init__(self, poll_interval: float = 0.05) -> None:
        self.poll_interval = poll_interval

    @property
    @abstractmethod
    def description(self) -> str:
        """Human-readable description, e.g. ``"wifi 1.2.3.4:2000"``."""

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def read(self) -> bytes: ...

    @abstractmethod
    def write(self, data: bytes) -> None: ...

    def recorded_time(self) -> Optional[float]:
        """When the chunk just read was originally recorded, in seconds from the start
        of the recording, or None for live data (the session clock is used then)."""
        return None

    def __enter__(self) -> "Transport":
        self.open()
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc: Optional[BaseException],
        traceback: Optional[TracebackType],
    ) -> None:
        self.close()
