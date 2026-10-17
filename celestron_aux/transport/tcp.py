"""Wi-Fi connection through a SkyPortal / Evolution Wi-Fi module."""

from __future__ import annotations

import select
import socket
from typing import Optional

from .base import Transport, TransportClosed

DEFAULT_PORT = 2000
_RECEIVE_SIZE = 4096


class TcpTransport(Transport):
    echoes_tx = False
    keepalive_by_default = True  # the Wi-Fi module drops idle connections

    def __init__(self, host: str, port: int = DEFAULT_PORT, connect_timeout: float = 5.0,
                 poll_interval: float = 0.05) -> None:
        super().__init__(poll_interval)
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self._socket: Optional[socket.socket] = None

    @property
    def description(self) -> str:
        return f"wifi {self.host}:{self.port}"

    def open(self) -> None:
        self._socket = socket.create_connection((self.host, self.port), timeout=self.connect_timeout)

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def read(self) -> bytes:
        sock = self._require_socket()
        ready, _, _ = select.select([sock], [], [], self.poll_interval)
        if not ready:
            return b""
        data = sock.recv(_RECEIVE_SIZE)
        if not data:
            raise TransportClosed("connection closed by the Wi-Fi module")
        return data

    def write(self, data: bytes) -> None:
        self._require_socket().sendall(data)

    def _require_socket(self) -> socket.socket:
        if self._socket is None:
            raise TransportClosed("transport is not open")
        return self._socket
