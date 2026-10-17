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
