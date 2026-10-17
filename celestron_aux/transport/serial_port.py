"""Serial connections: directly on the AUX bus, or through a hand controller."""

from __future__ import annotations

import logging
import time
from typing import List, Optional, Tuple

import serial

from .base import Transport, TransportClosed

log = logging.getLogger(__name__)


def list_serial_ports() -> List[Tuple[str, str]]:
    """(device, description) for every serial port on this computer, e.g. COM10 or /dev/cu.usbserial-1."""
    from serial.tools import list_ports

    return sorted((port.device, port.description or "") for port in list_ports.comports())


class _SerialTransport(Transport):
    baudrate = 19200

    def __init__(self, port: str, poll_interval: float = 0.05) -> None:
        super().__init__(poll_interval)
        self.port_name = port
        self._port: Optional[serial.Serial] = None

    def open(self) -> None:
        self._port = serial.Serial(
            port=self.port_name,
            baudrate=self.baudrate,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=self.poll_interval,
            xonxoff=False,
        )
        self._port.reset_input_buffer()
        self._port.reset_output_buffer()

    def close(self) -> None:
        if self._port is not None:
            self._port.close()
            self._port = None

    def read(self) -> bytes:
        port = self._require_port()
        first = port.read(1)  # blocks for up to poll_interval
        if not first:
            return b""
        return first + port.read(port.in_waiting)

    def write(self, data: bytes) -> None:
        self._require_port().write(data)

    def _require_port(self) -> serial.Serial:
        if self._port is None:
            raise TransportClosed("transport is not open")
        return self._port


class SerialBusTransport(_SerialTransport):
    """Direct connection to the AUX bus through a serial adapter on an AUX port."""

    baudrate = 19200
    echoes_tx = True  # the bus is shared, so our own frames come back

    @property
    def description(self) -> str:
        return f"serial {self.port_name} @ {self.baudrate}"

    def write(self, data: bytes) -> None:
        # The RTS line drives the adapter's bus transmitter. This sequence comes
        # unchanged from the original tool, where it was tuned against real
        # adapters; change it only with hardware to test on.
        port = self._require_port()
        port.rtscts = True
        port.rts = True
        port.write(data)
        port.rts = False
        port.rts = True
        port.rts = False
        time.sleep(0.1)
        port.rtscts = False


class HandControllerTransport(_SerialTransport):
    """Connection through a NexStar hand controller's serial port.

    The hand controller starts in its normal 9600-baud serial protocol and is
    switched into AUX pass-through at 115200 baud.
    """

    baudrate = 9600
    passthrough_baudrate = 115200
    echoes_tx = False

    # Undocumented handshake observed on real hand controllers; the expected
    # replies are only logged, not enforced.
    _GET_VERSION = b"\x56"  # replies 05 1f 23
    _SWITCH_TO_AUX = b"\x8a"  # replies f8
    _POST_SWITCH_HANDSHAKE = bytes.fromhex("0a000208004b0000d0c0")  # replies 05 00 06 38 c0

    @property
    def description(self) -> str:
        return f"hand controller {self.port_name}"

    def open(self) -> None:
        super().open()
        port = self._require_port()
        self._exchange(self._GET_VERSION, "version")
        self._exchange(self._SWITCH_TO_AUX, "switch to AUX")
        port.baudrate = self.passthrough_baudrate  # applied immediately by pyserial
        time.sleep(1.0)
        self._exchange(self._POST_SWITCH_HANDSHAKE, "post-switch handshake")

    def _exchange(self, request: bytes, label: str) -> None:
        port = self._require_port()
        port.write(request)
        time.sleep(0.5)
        reply = port.read(port.in_waiting)
        log.debug("hand controller %s reply: %s", label, reply.hex(" ") or "(none)")
