"""Test doubles."""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Dict, List, Optional

from celestron_aux.protocol import AuxPacket, FrameParser
from celestron_aux.transport import Transport


class FakeMount(Transport):
    """In-memory transport that answers like a small mount would."""

    echoes_tx = False

    def __init__(self, versions: Dict[int, bytes], model: Optional[bytes] = None) -> None:
        super().__init__(poll_interval=0.01)
        self.versions = versions
        self.model = model
        self.written: List[bytes] = []
        self._incoming: "queue.Queue[bytes]" = queue.Queue()
        self._parser = FrameParser()
        self._lock = threading.Lock()

    @property
    def description(self) -> str:
        return "fake mount"

    def open(self) -> None:
        pass

    def close(self) -> None:
        pass

    def inject(self, data: bytes) -> None:
        self._incoming.put(data)

    def read(self) -> bytes:
        try:
            return self._incoming.get(timeout=self.poll_interval)
        except queue.Empty:
            return b""

    def write(self, data: bytes) -> None:
        with self._lock:
            self.written.append(data)
            frames = self._parser.feed(data)
        for parsed in frames:
            request = parsed.frame
            if not isinstance(request, AuxPacket):
                continue
            reply = self._reply_data(request)
            if reply is not None:
                self.inject(AuxPacket(request.destination, request.source, request.command, reply).encode())

    def _reply_data(self, request: AuxPacket) -> Optional[bytes]:
        if request.command == 0xFE:
            return self.versions.get(request.destination)
        if request.command == 0x05 and request.destination == 0x10:
            return self.model
        return None


def wait_until(condition: Callable[[], bool], timeout: float = 5.0, step: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(step)
    return condition()


class FakeTcpMount:
    """A mount reachable over TCP, like a SkyPortal Wi-Fi module, for end-to-end tests."""

    def __init__(self, versions: Dict[int, bytes], model: Optional[bytes] = None) -> None:
        import socketserver

        self.versions = versions
        self.model = model
        mount = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                parser = FrameParser()
                while True:
                    try:
                        data = self.request.recv(4096)
                    except OSError:
                        return
                    if not data:
                        return
                    for parsed in parser.feed(data):
                        request = parsed.frame
                        if isinstance(request, AuxPacket):
                            reply = mount.reply_data(request)
                            if reply is not None:
                                packet = AuxPacket(request.destination, request.source, request.command, reply)
                                self.request.sendall(packet.encode())

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._server = Server(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def reply_data(self, request: AuxPacket) -> Optional[bytes]:
        if request.command == 0xFE:
            return self.versions.get(request.destination)
        if request.command == 0x05 and request.destination == 0x10:
            return self.model
        return None

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
