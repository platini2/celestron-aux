"""Static knowledge about the Celestron AUX bus.

Plain data only: bus addresses, command numbers, display names and mount model
identifiers. Nothing here talks to hardware.
"""

from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Tuple


class Addr:
    """Well-known bus addresses referenced by the code."""

    MAIN_BOARD = 0x01
    AZM_MOTOR = 0x10
    ALT_MOTOR = 0x11
    FOCUSER = 0x12
    DEW_HEATER = 0x17
    SCANNER = 0x2F  # the address this tool uses on the bus
    GPS = 0xB0
    STARSENSE_CAMERA = 0xB4
    BATTERY = 0xB6
    CHARGER = 0xB7
    LIGHTS = 0xBF


class Cmd:
    """Command numbers the code acts on. The full name table is below."""

    GET_VERSION = 0xFE
    MTR_GET_MODEL = 0x05
    GPS_GET_LAT = 0x01
    GPS_GET_LONG = 0x02
    GPS_GET_DATE = 0x03
    GPS_GET_YEAR = 0x04
    GPS_GET_TIME = 0x33
    GPS_TIME_VALID = 0x36
    GPS_LINKED = 0x37
    SS_RESET_CAPTURE = 0x90
    SS_GET_CAPTURE_DATA = 0x92
    SS_START_CAPTURE = 0x94


DEVICE_NAMES: Mapping[int, str] = {
    0x01: "Main Board",
    0x04: "Nexstar HC",
    0x0D: "Nexstar+ HC",
    0x0E: "Starsense HC",
    0x10: "AZM MC",
    0x11: "ALT MC",
    0x12: "Focuser",
    0x17: "Dew Heater Controller",
    0x20: "CPWI",
    0x21: "CFM",
    0x22: "Focuser Utility",
    0x2F: "AUXBUS Scanner",
    0x30: "CGX RA Switch",
    0x31: "CGX DEC Switch",
    0x32: "CGX DEC Autoguide Pt",
    0xB0: "GPS",
    0xB2: "RTC",
    0xB3: "Skyportal Accessory",
    0xB4: "Starsense Camera",
    0xB5: "Nexstar EVO WiFi",
    0xB6: "Battery Power Cont",
    0xB7: "Charge Port",
    0xB8: "Starsense Camera SW",
    0xBB: "Dew Heater Controller",
    0xBD: "Starsense Camera V2",
    0xBF: "Mount Lights",
}

# Devices that originate requests. In a packet between a controller and another
# device, the command number belongs to the other device.
CONTROLLERS = frozenset({0x04, 0x0D, 0x0E, 0x20, 0x21, 0x22, Addr.SCANNER})

# Model identifiers returned by MTR_GET_MODEL on the azimuth/RA motor.
# Families seen but not yet mapped to an exact id: 0x05xx CGE, 0x06xx Advanced GT,
# 0x09xx CPC, 0x0Axx GT, 0x0Dxx CGE Pro, 0x0Exx CGEM DX, 0x0Fxx LCM,
# 0x10xx SkyProdigy, 0x13xx StarSeeker, 0x15xx Cosmos, 0x19xx AstroFi, 0x1Axx SkyWatcher.
MOUNT_MODELS: Mapping[int, str] = {
    0x0001: "Nexstar GPS",
    0x0783: "Nexstar SLT",
    0x0B83: "4/5SE",
    0x0C82: "6/8SE",
    0x1189: "CPC Deluxe",
    0x1283: "GT Series",
    0x1485: "AVX",
    0x1687: "Nexstar Evolution",
    0x1788: "CGX",
    0x1888: "CGX-L",
}

_MOTOR_COMMANDS: Dict[int, str] = {
    0x01: "MTR_GET_POSITION",  # 16/24-bit BAM position
    0x02: "MTR_GOTO",  # coarse, 16/24-bit BAM position
    0x04: "MTR_SET_POSITION",  # resets to a new 16/24-bit BAM position
    0x05: "MTR_GET_MODEL",
    0x06: "MTR_PTRACK",  # 16/24-bit speed in arcsec/1024, positive
    0x07: "MTR_NTRACK",  # 16/24-bit speed in arcsec/1024, negative
    0x08: "MTR_REMOTE_SWITCH_ALIVE",
    0x09: "MTR_SWITCH_STATE_CHANGE",
    0x0A: "MTR_AG_STATE_CHANGE",
    0x0B: "MTR_MVSWITCH",  # ALT only, finds the down-stop switch
    0x0C: "MTR_PECTRAIN",  # AZM only, EQ North/South must be on first
    0x0D: "MTR_PECPLAY",  # AZM only, EQ North/South must be on first
    0x0E: "MTR_GET_PEC_BIN",
    0x10: "MTR_SET_POS_BACKLASH",  # 1 byte, 0 = off, 99 = max
    0x11: "MTR_SET_NEG_BACKLASH",  # 1 byte, 0 = off, 99 = max
    0x12: "MTR_IS_MVSWITCH_OVER",
    0x13: "MTR_IS_GOTO_OVER",
    0x14: "MTR_PEC_STATE_CHANGE",
    0x15: "MTR_IS_PECTRAIN_OVER",
    0x16: "MTR_CANCEL_PECTRAIN",
    0x17: "MTR_GOTO2",
    0x18: "MTR_IS_INDEX_FOUND",
    0x19: "MTR_FIND_INDEX",
    0x1A: "MTR_SET_USER_LIMIT_MIN",
    0x1B: "MTR_SET_USER_LIMIT_MAX",
    0x1C: "MTR_GET_USER_LIMIT_MIN",
    0x1D: "MTR_GET_USER_LIMIT_MAX",
    0x1E: "MTR_IS_USER_LIMIT_ENABLED",
    0x1F: "MTR_SET_USER_LIMIT_ENABLED",
    0x20: "MTR_SET_CUSTOM_RATE9",
    0x21: "MTR_GET_CUSTOM_RATE9",
    0x22: "MTR_SET_CUSTOM_RATE9_ENA",
    0x23: "MTR_GET_CUSTOM_RATE9_ENA",
    0x24: "MTR_PMSLEW_RATE",
    0x25: "MTR_NMSLEW_RATE",
    0x26: "MTR_AUX_GUIDE",
    0x27: "MTR_IS_AUX_GUIDE_ACTIVE",
    0x2A: "MTR_HS_CALIBRATION_ENABLE",
    0x2B: "MTR_IS_HS_CALIBRATED",
    0x2C: "MTR_GET_HS_POSITIONS",
    0x30: "MTR_EEPROM_READ",
    0x31: "MTR_EEPROM_WRITE",
    0x32: "MTR_PROGRAM_READ",
    0x38: "MTR_CORDWRAP_ON",
    0x39: "MTR_CORDWRAP_OFF",
    0x3A: "MTR_SET_CORDWRAP_POSITION",
    0x3B: "MTR_IS_CORDWRAP_ON",
    0x3C: "MTR_GET_CORDWRAP_POSITION",
    0x3D: "MTR_SET_SHUTTER",
    0x40: "MTR_GET_POS_BACKLASH",
    0x41: "MTR_GET_NEG_BACKLASH",
    0x46: "MTR_SET_AUTOGUIDE_RATE",
    0x47: "MTR_GET_AUTOGUIDE_RATE",
    0x48: "MTR_SET_SWITCH_CALIBRATION",
    0x49: "MTR_SET_SWITCH_CALIBRATION",  # listed twice in the source data; probably the GET
    0x4A: "MTR_SET_PRN_VALUE",
    0x4B: "MTR_GET_PRN_VALUE",
    0x50: "MTR_SEND_WARNING",
    0x51: "MTR_SEND_ERROR",
    0x5B: "MTR_SET_PID_KP",
    0x5C: "MTR_SET_PID_KI",
    0x5D: "MTR_SET_PID_KD",
    0x5F: "MTR_ENABLE_PID_ANALYSIS",
    0x8A: "MTR_PROGRAMMER_ENABLE",
    0xEE: "MTR_GET_HARDSWITCH_ENABLE",
    0xEF: "MTR_SET_HARDSWITCH_ENABLE",
    0xFA: "MTR_GET_CHIPVERSION",
    0xFB: "MTR_GET_BOOTVERSION",
    0xFC: "MTR_IS_APPROACH_DIR_NEG",
    0xFD: "MTR_SET_APPROACH_DIR_NEG",
    0xFE: "MTR_GET_VERSION",
}

_DEVICE_COMMANDS: Dict[int, Dict[int, str]] = {
    0x01: {0xFE: "MB_GET_FW_VER"},
    0x04: {0xFE: "NXS_GET_FW_VER"},
    0x0D: {0xFE: "NXS_GET_FW_VER"},
    0x0E: {0xFE: "NXS_GET_FW_VER"},
    0x10: _MOTOR_COMMANDS,
    0x11: _MOTOR_COMMANDS,
    0x12: {
        0x01: "FOCUS_GET_POSITION",
        0x02: "FOCUS_GOTO_FAST",
        0x05: "FOCUS_GET_MODEL",
        0x13: "FOCUS_SLEW_DONE",
        0x24: "FOCUS_MOVE_POS",
        0x25: "FOCUS_MOVE_NEG",
        0x2A: "FOCUS_CALIBRATION_ENABLE",
        0x2B: "FOCUS_IS_CALIBRATED",
        0x2C: "FOCUS_GET_CALIBRATION_POSITIONS",
        0x3B: "FOCUS_UNKNOWN_3B",
        0xFE: "FOCUS_GET_FW_VER",
    },
    0x17: {
        0x00: "DEWHEATER_GET_CURRENT_POWER",
        0x04: "DEWHEATER_GET_LIMIT_POWER",
        0x10: "DEWHEATER_GET_POWER_PORTS",
        0x11: "DEWHEATER_GET_PORTS",
        0x12: "DEWHEATER_GET_STATUS",
        0x18: "DEWHEATER_GET_AMBIENT",
    },
    0x20: {0xFE: "NXS_GET_FW_VER"},
    0xB0: {
        0x01: "GPS_GET_LAT",
        0x02: "GPS_GET_LONG",
        0x03: "GPS_GET_DATE",
        0x04: "GPS_GET_YEAR",
        0x07: "GPS_GET_SAT_INFO",
        0x08: "GPS_GET_RCVR_STATUS",
        0x33: "GPS_GET_TIME",
        0x36: "GPS_TIME_VALID",
        0x37: "GPS_LINKED",
        0xA0: "GPS_GET_COMPASS",
        0xFE: "GPS_GET_FW_VER",
    },
    0xB3: {0xFE: "WIFI_GET_FW_VER"},
    0xB4: {
        0x3E: "SS_SET_ALIGN_CENTER",
        0x3F: "SS_GET_ALIGN_CENTER",
        0x90: "SS_RESET_CAPTURE",
        0x91: "SS_GET_STATUS",
        0x92: "SS_GET_CAPTURE_DATA",
        0x94: "SS_START_CAPTURE",
        0x9F: "SS_GET_IMAGE3",
        0xFE: "SS_GET_FW_VER",
    },
    0xB5: {0xFE: "WIFI_GET_FW_VER"},
    0xB6: {0x10: "BAT_GET_VOLTAGE", 0x18: "BAT_GET_CURRENT", 0xFE: "BAT_GET_FW_VER"},
    0xB7: {0x10: "CHG_MODE", 0xFE: "CHG_GET_FW_VER"},  # 0x10 both reads and sets
    0xBB: {0xFE: "DEWHEATER_GET_FW_VER"},
    0xBD: {0xFE: "SS2_GET_FW_VER"},
    0xBF: {0x10: "LIGHT_LEVEL", 0xFE: "LIGHT_GET_FW_VER"},  # 0x10 both reads and sets
}

UNKNOWN = "Unknown"


def device_name(address: int) -> str:
    return DEVICE_NAMES.get(address, UNKNOWN)


def is_known_device(address: int) -> bool:
    return address in DEVICE_NAMES


def command_name(device: int, command: int) -> str:
    return _DEVICE_COMMANDS.get(device, {}).get(command, UNKNOWN)


def is_known_command(device: int, command: int) -> bool:
    return command in _DEVICE_COMMANDS.get(device, {})


def commands_for(device: int) -> List[Tuple[int, str]]:
    """All known commands of ``device``, sorted by command number."""
    return sorted(_DEVICE_COMMANDS.get(device, {}).items())


def mount_name(model: Optional[int]) -> str:
    if model is None:
        return "Not detected"
    return MOUNT_MODELS.get(model, f"{UNKNOWN} (0x{model:04x})")


def subject_device(source: int, destination: int) -> int:
    """Return the device a packet is about; command numbers are scoped by it."""
    if source == Addr.SCANNER:
        return destination
    if destination == Addr.SCANNER:
        return source
    return destination if source in CONTROLLERS else source
