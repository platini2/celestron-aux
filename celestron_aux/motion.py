"""Mount position readout and manual motion of the two motor axes.

Positions are the motors' own axis angles (azimuth/RA and altitude/Dec), not
sky coordinates. They are learned from every MTR_GET_POSITION reply on the bus,
so positions requested by a hand controller (or found in a replay) show up too,
and they can also be polled once a second.

Nothing is polled and no move is accepted until the panel is enabled; turning
it off stops any motion first.

Manual motion uses the variable-rate slew commands. A slew keeps going until a
stop (rate 0) is sent, so every move is held by a dead-man deadline: the
controlling page must keep re-confirming the move, and if it stops doing so
(tab closed, network gone, laptop asleep) the axis is stopped automatically.
"""

from __future__ import annotations

import enum
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from .bus import AuxBus, BusEvent
from .catalog import Addr
from .protocol import AuxPacket
from .protocol.units import bam_to_degrees, format_dms

log = logging.getLogger(__name__)

MTR_GET_POSITION = 0x01
MTR_SLEW_POSITIVE = 0x24  # MTR_PMSLEW_RATE, one byte: rate 1-9, 0 stops
MTR_SLEW_NEGATIVE = 0x25  # MTR_NMSLEW_RATE

MIN_RATE, MAX_RATE = 1, 9
DEFAULT_POLL_INTERVAL = 1.0
DEFAULT_DEADMAN = 1.0  # seconds a move survives without being confirmed again
_TICK = 0.1


class Axis(enum.Enum):
    AZM = "azm"
    ALT = "alt"

    @property
    def address(self) -> int:
        return Addr.AZM_MOTOR if self is Axis.AZM else Addr.ALT_MOTOR

    @property
    def label(self) -> str:
        return "Azimuth / RA axis" if self is Axis.AZM else "Altitude / Dec axis"


_AXIS_BY_ADDRESS = {axis.address: axis for axis in Axis}


@dataclass
class AxisState:
    degrees: Optional[float] = None
    updated: Optional[float] = None  # monotonic time of the last position
    direction: int = 0               # +1, -1 or 0 when not moving
    rate: int = 0
    deadline: float = 0.0


class MotionRefused(Exception):
    """A move or stop could not be sent (listen-only mode or a closed connection)."""


class MountMotion:
    """Bus listener plus a small worker thread for polling and the dead-man check."""

    def __init__(self, bus: AuxBus, *, poll_interval: float = DEFAULT_POLL_INTERVAL,
                 deadman: float = DEFAULT_DEADMAN, enabled: bool = False,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._bus = bus
        self._clock = clock
        self.poll_interval = poll_interval
        self.deadman = deadman
        self._enabled = enabled
        self._lock = threading.Lock()
        self._axes: Dict[Axis, AxisState] = {axis: AxisState() for axis in Axis}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="aux-motion", daemon=True)
        self._next_poll = 0.0

    # --- positions --------------------------------------------------------------

    def __call__(self, event: BusEvent) -> None:
        packet = event.frame
        if not isinstance(packet, AuxPacket) or packet.command != MTR_GET_POSITION:
            return
        axis = _AXIS_BY_ADDRESS.get(packet.source)  # replies come from the motor
        if axis is None or len(packet.data) < 3:
            return
        degrees = bam_to_degrees(packet.data[:3])
        with self._lock:
            state = self._axes[axis]
            state.degrees, state.updated = degrees, self._clock()

    def request_positions(self) -> bool:
        """Ask both motors for their position. False if nothing could be sent."""
        sent = False
        for axis in Axis:
            sent = self._bus.send(AuxPacket(Addr.SCANNER, axis.address, MTR_GET_POSITION)) or sent
        return sent

    def snapshot(self) -> Dict[str, Any]:
        now = self._clock()
        with self._lock:
            axes = {
                axis.value: {
                    "label": axis.label,
                    "degrees": None if s.degrees is None else round(s.degrees, 6),
                    "dms": None if s.degrees is None else format_dms(s.degrees),
                    "age": None if s.updated is None else round(now - s.updated, 1),
                    "direction": s.direction,
                    "rate": s.rate,
                }
                for axis, s in self._axes.items()
            }
        return {"axes": axes, "enabled": self.enabled}

    # --- on/off -------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        if not value and self.moving:
            self.stop()
        with self._lock:
            self._enabled = value
            self._next_poll = 0.0

    # --- motion -----------------------------------------------------------------

    def move(self, axis: Axis, direction: int, rate: int) -> None:
        """Start or keep moving ``axis``. Must be repeated within ``deadman`` seconds."""
        if direction not in (1, -1):
            raise ValueError("direction must be 1 or -1")
        if not MIN_RATE <= rate <= MAX_RATE:
            raise ValueError(f"rate must be between {MIN_RATE} and {MAX_RATE}")
        with self._lock:
            if not self._enabled:
                raise MotionRefused("Mount control is off. Switch it on to move the mount.")
            state = self._axes[axis]
            state.deadline = self._clock() + self.deadman
            if state.direction == direction and state.rate == rate:
                return  # already moving like this: only the deadline needed refreshing
            # Record the move before sending, so a confirmation that arrives while the
            # bus is busy does not send the same command twice.
            previous = (state.direction, state.rate)
            state.direction, state.rate = direction, rate
        command = MTR_SLEW_POSITIVE if direction > 0 else MTR_SLEW_NEGATIVE
        if not self._bus.send(AuxPacket(Addr.SCANNER, axis.address, command, bytes((rate,)))):
            with self._lock:
                state.direction, state.rate = previous
            raise MotionRefused("The move could not be sent.")

    def stop(self, axis: Optional[Axis] = None) -> bool:
        """Stop one axis, or both. Always sends, even if the axis seems idle."""
        sent = True
        for target in ([axis] if axis else list(Axis)):
            with self._lock:
                self._axes[target].direction = self._axes[target].rate = 0
            sent = self._bus.send(AuxPacket(Addr.SCANNER, target.address, MTR_SLEW_POSITIVE, b"\x00")) and sent
        return sent

    @property
    def moving(self) -> bool:
        with self._lock:
            return any(s.direction for s in self._axes.values())

    # --- lifecycle --------------------------------------------------------------

    def start(self) -> None:
        self._thread.start()

    def close(self) -> None:
        """Stop the worker and, if anything was moving, the motors."""
        self._stop.set()
        if self._thread.is_alive():
            self._thread.join()
        if self.moving:
            self.stop()

    def _run(self) -> None:
        while not self._stop.wait(_TICK):
            now = self._clock()
            with self._lock:
                expired = [axis for axis, s in self._axes.items() if s.direction and now > s.deadline]
            for axis in expired:
                log.warning("%s move was not confirmed in time; stopping it", axis.label)
                self.stop(axis)
            if self.enabled and now >= self._next_poll and not self._bus.listen_only:
                self._next_poll = now + self.poll_interval
                self.request_positions()
