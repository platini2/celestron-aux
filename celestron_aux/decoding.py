"""Turns AUX packets into human-readable descriptions.

Payload decoders are registered per (device, command). A decoder receives the
payload and whether the packet is a reply from the device (as opposed to a
request sent to it), because several commands carry different data each way.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
import math
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Iterable, Mapping, Optional, Tuple

from . import catalog
from .catalog import Addr, Cmd
from .protocol import AuxPacket
from .protocol.units import bam_to_degrees, format_dms, format_hms, format_version

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
    value: str        # the decoded payload, or "" when it cannot be decoded
    payload: str      # the payload bytes as hex, always
    malformed: bool = False  # a decoder exists but the payload did not fit it

    @property
    def text(self) -> str:
        """The value for plain-text output: decoded if possible, otherwise the hex."""
        if self.value:
            return self.value
        return f"{self.payload} (unexpected format)" if self.malformed else self.payload


def describe_packet(packet: AuxPacket) -> PacketDescription:
    subject = catalog.subject_device(packet.source, packet.destination)
    value, malformed = decode_payload(packet, subject)
    return PacketDescription(
        sender=catalog.device_name(packet.source),
        sender_id=packet.source,
        receiver=catalog.device_name(packet.destination),
        receiver_id=packet.destination,
        command=catalog.command_name(subject, packet.command),
        command_id=packet.command,
        value=value,
        payload=packet.data.hex(),
        malformed=malformed,
    )


def decode_payload(packet: AuxPacket, subject: int) -> Tuple[str, bool]:
    """Decode the payload of ``packet``.

    Returns (decoded text or "", malformed). The text is empty when the payload
    is empty, when no decoder knows the command, or when the payload does not
    fit the decoder (then ``malformed`` is True).
    """
    if not packet.data:
        return "", False
    decoder = _DECODERS.get((subject, packet.command)) or _DECODERS.get((ANY_DEVICE, packet.command))
    if decoder is None:
        return "", False
    try:
        return decoder(packet.data, packet.source == subject), False
    except (IndexError, ValueError, struct.error):
        return "", True


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


@_decodes([Addr.GPS], [0x55])
def _gps_hardware_version(data: bytes, is_reply: bool) -> str:
    # 0xab on Celestron GPS units (Paquette: the Motorola GPS module).
    label = {0xAB: "Celestron GPS"}.get(data[0])
    return f"0x{data[0]:02x}" + (f" ({label})" if label else "")


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


# --- StarSense AutoGuider and StarSense AutoAlign V2 ---------------------------
# The V2 uses the AutoGuider's commands except guiding (0x2x), so the decoders
# are shared. Payload layouts from community reverse engineering, checked against
# published examples and captures from a real StarSense AutoAlign V2: RA/Dec are two big-endian
# float32 in radians, the location two big-endian float32 in degrees, and the
# time is seconds since 2000-01-01 UTC.

_SSAG = [Addr.STARSENSE_AUTOGUIDER]
_IN_PROGRESS, _SOLVED, _FAILED = 0, 1, 4  # status byte of plate solves and calibrations; 4 seen on real failures
_SSAG_AND_V2 = [Addr.STARSENSE_AUTOGUIDER, Addr.STARSENSE_CAMERA_V2]
_EPOCH_2000 = datetime(2000, 1, 1, tzinfo=timezone.utc)


def _ra_dec(data: bytes) -> str:
    _require(data, 8)
    ra, dec = (math.degrees(value) for value in struct.unpack(">ff", data[:8]))
    return f"RA {format_hms(ra)}, Dec {format_dms(dec)}"


@_decodes(_SSAG_AND_V2, [0x02])
@_decodes([Addr.STARSENSE_CAMERA_V2], [0x4B])  # same layout: a status, then RA/Dec
def _ssag_plate_status(data: bytes, is_reply: bool) -> str:
    if data[0] == _IN_PROGRESS:
        return "In progress"
    if data[0] == _FAILED:
        return f"Failed (status {data[0]})"  # no position: the camera sends zeros
    return f"Solved, {_ra_dec(data[1:])}" if data[0] == _SOLVED else f"Status {data[0]}, {_ra_dec(data[1:])}"


@_decodes(_SSAG_AND_V2, [0x03])
def _ssag_start_calibrate(data: bytes, is_reply: bool) -> str:
    return _ra_dec(data)


@_decodes(_SSAG_AND_V2, [0x04])
def _ssag_calibrate_status(data: bytes, is_reply: bool) -> str:
    if data[0] == _IN_PROGRESS:
        return "In progress"
    if data[0] == _FAILED:
        return f"Failed (status {data[0]})"
    _require(data, 5)
    state = "Solved" if data[0] == 1 else f"Status {data[0]}"
    return f"{state}, x {_uint(data, 1, 3)}, y {_uint(data, 3, 5)}"


@_decodes(_SSAG, [0x20])
def _ssag_set_guiding(data: bytes, is_reply: bool) -> str:
    return "Disabled" if data[0] == 0 else f"Enabled, flags 0x{data[0]:02x}"


@_decodes([Addr.STARSENSE_CAMERA_V2], [0x07])
def _ss2_is_calibrated(data: bytes, is_reply: bool) -> str:
    return {0: "No", 1: "Yes"}.get(data[0], str(data[0])) if is_reply else data.hex()


@_decodes([Addr.STARSENSE_CAMERA_V2], [0x13])
def _ss2_set_aspa(data: bytes, is_reply: bool) -> str:
    if is_reply:
        return "OK" if data[0] == 0 else data.hex()
    return {1: "Start", 0: "Stop", 4: "Adjust"}.get(data[0], data.hex())


@_decodes([Addr.STARSENSE_CAMERA_V2], [0x15])
def _ss2_aspa_pole(data: bytes, is_reply: bool) -> str:
    if data[0] == _SOLVED and len(data) >= 9:
        return f"Polar axis {_ra_dec(data[1:])}"
    return "No estimate yet" if data[0] == _FAILED else f"Status {data[0]}"


@_decodes([Addr.STARSENSE_CAMERA_V2], [0x16])
def _ss2_aspa_error(data: bytes, is_reply: bool) -> str:
    # Status, then the altitude and azimuth errors of the polar axis in degrees (float32).
    if data[0] != _SOLVED or len(data) < 9:
        return "No measurement" if not any(data) else f"Status {data[0]}"
    alt, azm = struct.unpack(">ff", data[1:9])
    return f"Altitude error {alt * 60:+.2f}', azimuth error {azm * 60:+.2f}'"


@_decodes([Addr.STARSENSE_CAMERA_V2], [0x17])
def _ss2_aspa_progress(data: bytes, is_reply: bool) -> str:
    # Status, completion (0-100), uncertainty in degrees (float32), then three bytes not understood.
    _require(data, 6)
    text = f"Completion {data[1]} %, uncertainty {struct.unpack('>f', data[2:6])[0]:.2f}\N{DEGREE SIGN}"
    return text if data[0] == _SOLVED else f"Waiting ({text})"


@_decodes(_SSAG_AND_V2, [0x45])
def _ssag_image_rotation(data: bytes, is_reply: bool) -> str:
    # The angle of the camera image on the sky, as a big-endian float32 in degrees.
    _require(data, 4)
    return f"Rotation {struct.unpack('>f', data[:4])[0]:.2f}\N{DEGREE SIGN}"


# The stored calibration (0x08), one slot per request. What each slot holds was
# worked out from a real calibration on Altair; slots 0-1 are a position 1.1 deg
# from the calibration star whose exact meaning is not settled.
_CALIBRATION_SLOTS = {
    0: "offset position RA",
    1: "offset position Dec",
    2: "calibration star RA",
    3: "calibration star Dec",
    4: "image rotation",
    5: "calibration time",
}
_RA_SLOTS, _DEC_SLOTS, _ROTATION_SLOT, _TIME_SLOT = (0, 2), (1, 3), 4, 5

# A reply does not say which slot it holds, so the decoder remembers the slot that
# was requested last. CPWI asks for one slot and gets its reply before the next.
_last_calibration_slot: Optional[int] = None


def _slot_name(slot: int) -> str:
    return f"Slot {slot}: {_CALIBRATION_SLOTS.get(slot, 'unknown')}"


@_decodes([Addr.STARSENSE_CAMERA_V2], [0x08])
def _ss2_calibration_data(data: bytes, is_reply: bool) -> str:
    global _last_calibration_slot
    if not is_reply:
        _last_calibration_slot = data[0]
        return _slot_name(data[0])
    _require(data, 4)
    value = struct.unpack(">f", data[:4])[0]
    slot = _last_calibration_slot
    if value == 0:
        text = "0 (empty)"
    elif slot == _TIME_SLOT or (slot is None and value > 1e9):
        # Unix seconds, stored with a resolution of about 2 minutes. On real hardware
        # it read exactly one day after the calibration.
        when = datetime.fromtimestamp(value, timezone.utc)
        text = f"{when:%Y-%m-%d %H:%M} UTC ({value:.0f})"
    elif slot in _RA_SLOTS:
        text = f"{format_hms(value)} ({value:.4f}\N{DEGREE SIGN})"
    elif slot in _DEC_SLOTS:
        text = format_dms(value)
    elif slot == _ROTATION_SLOT:
        text = f"{value:.2f}\N{DEGREE SIGN}"
    else:
        text = f"{value:.6g}"
    return text if slot is None else f"{_slot_name(slot)}, {text}"


@_decodes(_SSAG_AND_V2, [0x30])
def _ssag_mount_info(data: bytes, is_reply: bool) -> str:
    # The model byte is the first byte of the MTR_GET_MODEL id, so it names a mount family.
    # The second byte is a mounting flag whose values are not known yet: one source reads 1
    # as alt-az, but a CPC on an equatorial wedge also sends 1. So it is shown as is.
    _require(data, 4)
    family = catalog.MOUNT_FAMILIES.get(data[0])
    model = f"Model 0x{data[0]:02x}" + (f" ({family} family)" if family else "")
    return f"{model}, mount mode {data[1]}, AZM firmware {data[2]}.{data[3]}"


@_decodes(_SSAG_AND_V2, [0x31])
def _ssag_geolocation(data: bytes, is_reply: bool) -> str:
    _require(data, 8)
    latitude, longitude = struct.unpack(">ff", data[:8])
    return f"Lat {latitude:.4f}, Lon {longitude:.4f}"


@_decodes(_SSAG_AND_V2, [0x32])
def _ssag_time(data: bytes, is_reply: bool) -> str:
    _require(data, 4)
    when = _EPOCH_2000 + timedelta(seconds=_uint(data, 0, 4))
    return when.strftime("%Y-%m-%d %H:%M:%S UTC")


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


# The commands below come from community reverse engineering of how CPWI talks to
# the dew heater controller. Heater channels count from 0 on the bus.

@_decodes([Addr.DEW_HEATER], [0x02, 0x20])
def _dew_led_brightness(data: bytes, is_reply: bool) -> str:
    return f"Brightness {data[0]}"


@_decodes([Addr.DEW_HEATER], [0x03])
def _dew_set_input_limit(data: bytes, is_reply: bool) -> str:
    _require(data, 2)
    return f"{_uint(data, 0, 2) / 1000:g} A"


@_decodes([Addr.DEW_HEATER], [0x14])
def _dew_enable_port(data: bytes, is_reply: bool) -> str:
    _require(data, 2)
    return f"Port {data[0]} {_ON_OFF.get(data[1], str(data[1])).lower()}"


@_decodes([Addr.DEW_HEATER], [0x16])
def _dew_set_aggression(data: bytes, is_reply: bool) -> str:
    _require(data, 2)
    return f"Heater {data[0] + 1}, aggression {data[1]}"


@_decodes([Addr.DEW_HEATER], [0x17])
def _dew_set_manual_power(data: bytes, is_reply: bool) -> str:
    _require(data, 2)
    return f"Heater {data[0] + 1}, power {round(data[1] / 2.55)}%"


@_decodes([Addr.DEW_HEATER], [0x19])
def _dew_recalibrate(data: bytes, is_reply: bool) -> str:
    return "Start" if data[0] else "Stop"


@_decodes([Addr.DEW_HEATER], [0x1A])
def _dew_is_calibrating(data: bytes, is_reply: bool) -> str:
    return "Calibrating" if data[0] else "Not calibrating"


@_decodes([Addr.DEW_HEATER], [0x18])
def _dew_ambient(data: bytes, is_reply: bool) -> str:
    _require(data, 9)
    temperature = _uint(data, 0, 4) / 1000
    dew_point = _uint(data, 4, 8) / 1000
    return (
        f"Temp: {temperature:g}\N{DEGREE SIGN}C Dew: {dew_point:g}\N{DEGREE SIGN}C "
        f"Hum: {data[8]}%"
    )


# --- power, charger, lights and focuser -------------------------------------
# These formats come from community reverse engineering (the INDI celestronaux
# driver and caux-simulator), not from Celestron documentation. Payloads of an
# unexpected length fall back to hex, so a wrong guess shows up as raw bytes
# rather than as a believable but wrong number.

_ON_OFF = {0: "Off", 1: "On"}


@_decodes([Addr.BATTERY], [0x10])
def _battery_status(data: bytes, is_reply: bool) -> str:
    if not is_reply:
        return data.hex()
    _require(data, 6)
    level = {0: "low", 1: "medium", 2: "high"}.get(data[1])
    status = f"status {data[1]}" + (f" ({level})" if level else "")
    charging = "charging" if data[0] else "not charging"
    return f"{_uint(data, 2, 6) / 1_000_000:.3f} V, {status}, {charging}"


@_decodes([Addr.EVOLUTION_WIFI], [0x10])
def _wifi_enable(data: bytes, is_reply: bool) -> str:
    return _ON_OFF.get(data[0], str(data[0]))


@_decodes([Addr.BATTERY], [0x18])
def _battery_current(data: bytes, is_reply: bool) -> str:
    _require(data, 2)
    return f"{_uint(data, 0, 2)} mA"


@_decodes([Addr.CHARGER], [0x10])
def _charger_mode(data: bytes, is_reply: bool) -> str:
    state = _ON_OFF.get(data[0], str(data[0]))
    return f"Charging {state.lower()}" if is_reply else f"Set charging {state.lower()}"


_LIGHTS = {0: "tray", 1: "logo", 2: "Wi-Fi"}


@_decodes([Addr.LIGHTS], [0x10])
def _light_level(data: bytes, is_reply: bool) -> str:
    if is_reply:
        return f"Level {data[0]}"
    light = _LIGHTS.get(data[0], f"light {data[0]}")
    if len(data) >= 2:
        return f"Set {light} light to {data[1]}"
    return f"{light[:1].upper()}{light[1:]} light"


@_decodes([Addr.FOCUSER], [0x01])
def _focuser_position(data: bytes, is_reply: bool) -> str:
    _require(data, 3)
    return str(_uint(data, 0, 3))


@_decodes([Addr.FOCUSER], [0x02])
def _focuser_goto(data: bytes, is_reply: bool) -> str:
    if is_reply:
        return _labelled(data[0], _ACK)
    _require(data, 3)
    return f"To {_uint(data, 0, 3)}"


@_decodes([Addr.FOCUSER], [0x13])
def _focuser_slew_done(data: bytes, is_reply: bool) -> str:
    return _labelled(data[0], {0: "Moving", 0xFF: "Done"})


@_decodes([Addr.FOCUSER], [0x24, 0x25])
def _focuser_move_rate(data: bytes, is_reply: bool) -> str:
    return _labelled(data[0], _ACK) if is_reply else f"Rate {data[0]}"


@_decodes([Addr.FOCUSER], [0x2C])
def _focuser_limits(data: bytes, is_reply: bool) -> str:
    _require(data, 8)
    return f"Limits {_uint(data, 0, 4)} to {_uint(data, 4, 8)}"
