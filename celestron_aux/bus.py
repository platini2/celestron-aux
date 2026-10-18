"""The bus engine: moves bytes between a transport and the rest of the app.

Two background threads:

* the **receive** thread reads the transport, parses frames and queues events;
* the **dispatch** thread delivers events to listeners one at a time.

Listeners therefore run serially and never on the receive thread, so a slow
listener (writing an image, say) cannot make us drop bytes, and a listener may
safely call ``send`` (the GPS emulator does) without deadlocking.

When the transport does not echo our transmissions, ``send`` queues a TX
event for the sent frame, so listeners see both directions either way.
"""

from __future__ import annotations

import enum
import logging
import queue
import threading
import time
from dataclasses import dataclass
from typing import Callable, List, Union

from .clock import SessionClock
from .protocol import AuxPacket, Frame, FrameParser, StarSenseFrame
from .transport import Transport, TransportClosed

log = logging.getLogger(__name__)

DEFAULT_INTER_FRAME_DELAY = 0.15


class Direction(enum.Enum):
    RX = "rx"
    TX = "tx"


@dataclass(frozen=True)
class BusEvent:
    timestamp: float
    direction: Direction
    frame: Frame
    raw: bytes


Listener = Callable[[BusEvent], None]
RawTap = Callable[[Direction, bytes, float], None]
OutgoingFrame = Union[AuxPacket, StarSenseFrame]

_STOP = object()


class AuxBus:
    def __init__(
        self,
        transport: Transport,
        clock: SessionClock,
        *,
        inter_frame_delay: float = DEFAULT_INTER_FRAME_DELAY,
    ) -> None:
        self._transport = transport
        self._clock = clock
        self._inter_frame_delay = inter_frame_delay
        self._parser = FrameParser()
        self._listeners: List[Listener] = []
        self._raw_taps: List[RawTap] = []
        self._events: "queue.Queue[object]" = queue.Queue()
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._closed = threading.Event()
        self._last_tx = time.monotonic()
        #: When True nothing is ever transmitted: every send is dropped here, the
        #: single path all outgoing traffic takes (scans, keep-alive, GPS, commands).
        self.listen_only = False
        self._receive_thread = threading.Thread(target=self._receive_loop, name="aux-receive", daemon=True)
        self._dispatch_thread = threading.Thread(target=self._dispatch_loop, name="aux-dispatch", daemon=True)

    # --- setup ------------------------------------------------------------

    def subscribe(self, listener: Listener) -> None:
        """Register a listener. Call before ``start``."""
        self._listeners.append(listener)

    def add_raw_tap(self, tap: RawTap) -> None:
        """Register a callback for every raw chunk sent or received. Call before ``start``."""
        self._raw_taps.append(tap)

    # --- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        self._last_tx = time.monotonic()
        self._dispatch_thread.start()
        self._receive_thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Stop the threads and close the transport. Safe to call more than once."""
        self._stop.set()
        if self._receive_thread.is_alive():
            self._receive_thread.join(timeout)
        self._events.put(_STOP)
        if self._dispatch_thread.is_alive():
            self._dispatch_thread.join(timeout)
        with self._send_lock:
            self._transport.close()
        self._closed.set()

    @property
    def is_open(self) -> bool:
        return not self._closed.is_set() and not self._stop.is_set()

    @property
    def closed(self) -> threading.Event:
        """Set once the connection is gone (shutdown, error or end of playback)."""
        return self._closed

    # --- traffic ------------------------------------------------------------

    def send(self, frame: OutgoingFrame) -> bool:
        """Transmit a frame. Returns False if the bus is closed or listen-only."""
        raw = frame.encode()
        if self.listen_only:
            log.debug("listen-only; not sending %s", raw.hex())
            return False
        with self._send_lock:
            if not self.is_open:
                log.debug("bus closed; dropping %s", raw.hex())
                return False
            try:
                self._transport.write(raw)
            except OSError as exc:
                log.error("send failed: %s", exc)
                return False
            self._last_tx = time.monotonic()
            timestamp = self._clock.now()
            self._tap(Direction.TX, raw, timestamp)
            if not self._transport.echoes_tx:
                self._events.put(BusEvent(timestamp, Direction.TX, frame, raw))
            # Pace transmissions so devices have time to answer.
            time.sleep(self._inter_frame_delay)
        return True

    def seconds_since_last_tx(self) -> float:
        return time.monotonic() - self._last_tx

    @property
    def out_of_frame_bytes(self) -> int:
        return self._parser.out_of_frame_bytes

    # --- threads ------------------------------------------------------------

    def _receive_loop(self) -> None:
        while not self._stop.is_set():
            try:
                data = self._transport.read()
            except TransportClosed as exc:
                log.info("%s", exc)
                break
            except OSError as exc:
                log.error("receive failed: %s", exc)
                break
            if not data:
                continue
            recorded = self._transport.recorded_time()  # playback keeps the original times
            timestamp = self._clock.now() if recorded is None else recorded
            self._tap(Direction.RX, data, timestamp)
            for parsed in self._parser.feed(data):
                self._events.put(BusEvent(timestamp, Direction.RX, parsed.frame, parsed.raw))
        self._closed.set()

    def _dispatch_loop(self) -> None:
        while True:
            event = self._events.get()
            if event is _STOP:
                return
            for listener in self._listeners:
                try:
                    listener(event)  # type: ignore[arg-type]
                except Exception:  # a broken listener must not stop the bus
                    log.exception("listener %r failed", listener)

    def _tap(self, direction: Direction, raw: bytes, timestamp: float) -> None:
        for tap in self._raw_taps:
            try:
                tap(direction, raw, timestamp)
            except Exception:
                log.exception("raw tap %r failed", tap)
