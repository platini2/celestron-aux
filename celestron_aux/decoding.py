"""Turns AUX packets into human-readable descriptions.

Payload decoders are registered per (device, command). A decoder receives the
payload and whether the packet is a reply from the device (as opposed to a
request sent to it), because several commands carry different data each way.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Callable, Dict, Iterable, Mapping, Optional, Tuple

from . import catalog
from .catalog import Addr, Cmd
from .protocol import AuxPacket
from .protocol.units import bam_to_degrees, format_dms, format_version

PayloadDecoder = Callable[[bytes, bool], str]

ANY_DEVICE = None
_DECODERS: Dict[Tuple[Optional[int], int], PayloadDecoder] = {}


@dataclass(frozen=True)
class PacketDescription:
    sender: str
    sender_id: int
    receiver: str
    receiver_id: int
    command: str
    command_id: int
    value: str


def describe_packet(packet: AuxPacket) -> PacketDescription:
    subject = catalog.subject_device(packet.source, packet.destination)
    return PacketDescription(
        sender=catalog.device_name(packet.source),
        sender_id=packet.source,
        receiver=catalog.device_name(packet.destination),
        receiver_id=packet.destination,
        command=catalog.command_name(subject, packet.command),
        command_id=packet.command,
        value=describe_payload(packet, subject),
    )


def describe_payload(packet: AuxPacket, subject: int) -> str:
    """Decode the payload of ``packet``; falls back to hex for unknown commands."""
    if not packet.data:
        return ""
    decoder = _DECODERS.get((subject, packet.command)) or _DECODERS.get((ANY_DEVICE, packet.command))
    if decoder is None:
        return packet.data.hex()
    try:
        return decoder(packet.data, packet.source == subject)
    except (IndexError, ValueError, struct.error):
        return f"{packet.data.hex()} (unexpected format)"


def _decodes(devices: Iterable[Optional[int]], commands: Iterable[int]):
    def register(decoder: PayloadDecoder) -> PayloadDecoder:
        for device in devices:
            for command in commands:
                _DECODERS[(device, command)] = decoder
        return decoder

    return register


def _require(data: bytes, size: int) -> None:
    if len(data) < size:
        raise IndexError(f"expected at least {size} bytes, got {len(data)}")


def _uint(data: bytes, start: int = 0, end: Optional[int] = None) -> int:
    return int.from_bytes(data[start:end], "big")


def _labelled(value: int, labels: Mapping[int, str]) -> str:
    label = labels.get(value)
    return f"{value} - {label}" if label else str(value)


# --- any device -------------------------------------------------------------

@_decodes([ANY_DEVICE], [Cmd.GET_VERSION])
def _version(data: bytes, is_reply: bool) -> str:
    return format_version(data)


# --- motor controllers ------------------------------------------------------

_MOTORS = (Addr.AZM_MOTOR, Addr.ALT_MOTOR)
_ACK = {1: "ACK"}


@_decodes(_MOTORS, [0x01, 0x02, 0x3A, 0x3C])
def _motor_position(data: bytes, is_reply: bool) -> str:
    return format_dms(bam_to_degrees(data[:3]))


@_decodes(_MOTORS, [0x17])
def _motor_goto2(data: bytes, is_reply: bool) -> str:
    return _labelled(data[0], _ACK) if is_reply else format_dms(bam_to_degrees(data[:3]))


@_decodes(_MOTORS, [0x06, 0x07])
def _motor_track_rate(data: bytes, is_reply: bool) -> str:
    if is_reply:
        return _labelled(data[0], _ACK)
    return f"{_uint(data, 0, 3) / 1024:.4f} arcsec/s"


@_decodes(_MOTORS, [Cmd.MTR_GET_MODEL])
def _motor_model(data: bytes, is_reply: bool) -> str:
    model = _uint(data)
    return f"0x{model:04x} ({catalog.mount_name(model)})"


@_decodes(_MOTORS, [0x12])
def _motor_switch_over(data: bytes, is_reply: bool) -> str:
    return _labelled(data[0], {0: "False", 1: "True"})


@_decodes(_MOTORS, [0x13])
def _motor_goto_over(data: bytes, is_reply: bool) -> str:
    return _labelled(data[0], {0: "Not done", 1: "Check", 0xFF: "Done"})


@_decodes(_MOTORS, [0x23])
def _motor_custom_rate_enabled(data: bytes, is_reply: bool) -> str:
    return _labelled(data[0], {0: "Disabled", 1: "Enabled"})


@_decodes(_MOTORS, [0x24, 0x25, 0x40, 0x41])
def _motor_single_byte(data: bytes, is_reply: bool) -> str:
    return str(data[0])


@_decodes(_MOTORS, [0x3B])
def _motor_cordwrap_on(data: bytes, is_reply: bool) -> str:
    return _labelled(data[0], {0: "Disabled", 0xFF: "Enabled"})


@_decodes(_MOTORS, [0x46, 0x47])
def _motor_autoguide_rate(data: bytes, is_reply: bool) -> str:
    return f"{100 * data[0] / 256:g}%"


@_decodes(_MOTORS, [0xFC, 0xFD])
def _motor_approach_direction(data: bytes, is_reply: bool) -> str:
    return _labelled(data[0], {0: "Positive", 1: "Negative"})


# --- GPS --------------------------------------------------------------------

_YES_NO = {0: "No", 1: "Yes"}


@_decodes([Addr.GPS], [Cmd.GPS_GET_LAT, Cmd.GPS_GET_LONG])
def _gps_coordinate(data: bytes, is_reply: bool) -> str:
    return format_dms(bam_to_degrees(data[:3]))


@_decodes([Addr.GPS], [Cmd.GPS_GET_DATE])
def _gps_date(data: bytes, is_reply: bool) -> str:
    _require(data, 2)
    return f"{data[0]}/{data[1]}"  # month/day


@_decodes([Addr.GPS], [Cmd.GPS_GET_YEAR])
def _gps_year(data: bytes, is_reply: bool) -> str:
    return str(_uint(data))


@_decodes([Addr.GPS], [0x07])
def _gps_satellites(data: bytes, is_reply: bool) -> str:
    _require(data, 2)
    return f"{data[0]} - {data[1]}"


@_decodes([Addr.GPS], [Cmd.GPS_GET_TIME])
def _gps_time(data: bytes, is_reply: bool) -> str:
    _require(data, 3)
    return f"{data[0]:02d}:{data[1]:02d}:{data[2]:02d}"


@_decodes([Addr.GPS], [Cmd.GPS_TIME_VALID, Cmd.GPS_LINKED])
def _gps_flag(data: bytes, is_reply: bool) -> str:
    return _labelled(data[0], _YES_NO)


# --- StarSense camera -------------------------------------------------------

@_decodes([Addr.STARSENSE_CAMERA], [0x3F])
def _starsense_align_center(data: bytes, is_reply: bool) -> str:
    if len(data) == 8:
        x, y = struct.unpack("<II", data)
        return f"{x} - {y}"
    return str(data[0])


@_decodes([Addr.STARSENSE_CAMERA], [0x91, 0x92, 0x94, 0x9F])
def _starsense_status(data: bytes, is_reply: bool) -> str:
    status = _labelled(data[0], {1: "Error", 3: "Ready", 9: "Finished"})
    return f"{status} - Stars: {data[1]}" if len(data) > 1 else status


# --- dew heater controller --------------------------------------------------

@_decodes([Addr.DEW_HEATER], [0x00])
def _dew_power(data: bytes, is_reply: bool) -> str:
    _require(data, 4)
    return f"{_uint(data, 0, 2) / 1000:g} V - {_uint(data, 2, 4) / 1000:g} A"


@_decodes([Addr.DEW_HEATER], [0x04])
def _dew_power_limit(data: bytes, is_reply: bool) -> str:
    _require(data, 4)
    return f"{_uint(data, 0, 2) / 1000:g} A - {_uint(data, 2, 4) / 1000:g} A"


@_decodes([Addr.DEW_HEATER], [0x10])
def _dew_power_ports(data: bytes, is_reply: bool) -> str:
    return str(data[0])


@_decodes([Addr.DEW_HEATER], [0x11])
def _dew_ports(data: bytes, is_reply: bool) -> str:
    if is_reply:
        return str(data[0])
    return _labelled(data[0], {0: "Get 12V ports", 1: "Get dew heater ports"})


@_decodes([Addr.DEW_HEATER], [0x12])
def _dew_status(data: bytes, is_reply: bool) -> str:
    if not is_reply:
        return _labelled(data[0], {0: "Port 1", 1: "Port 2"})
    _require(data, 6)
    mode = {0: "Manual", 1: "Auto"}.get(data[1], str(data[1]))
    power = data[2]
    text = f"Port {data[0]} - {mode} - Power: {'On' if power else 'Off'} {power} - Aggression {data[5]}"
    if power:
        _require(data, 10)
        text += f" - Temp {_uint(data, 6, 10) / 1000:g}\N{DEGREE SIGN}C"
    return text


@_decodes([Addr.DEW_HEATER], [0x18])
def _dew_ambient(data: bytes, is_reply: bool) -> str:
    _require(data, 9)
    temperature = _uint(data, 0, 4) / 1000
    dew_point = _uint(data, 4, 8) / 1000
    return (
        f"Temp: {temperature:g}\N{DEGREE SIGN}C Dew: {dew_point:g}\N{DEGREE SIGN}C "
        f"Hum: {data[8]}%"
    )
