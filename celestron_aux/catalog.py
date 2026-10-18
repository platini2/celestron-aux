"""Knowledge about the Celestron AUX bus: addresses, commands and their names.

The built-in tables below can be extended by a user catalog: names you give to
devices and commands you have decoded, kept in a JSON file of your own that is
layered over the built-in names (see ``UserCatalog``). Nothing here talks to
hardware.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Tuple

log = logging.getLogger(__name__)


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
    EVOLUTION_WIFI = 0xB5
    BATTERY = 0xB6
    CHARGER = 0xB7
    STARSENSE_AUTOGUIDER = 0xB9
    STARSENSE_CAMERA_V2 = 0xBD
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
    0xB9: "Starsense AutoGuider",
    0xBB: "Dew Heater Controller",
    0xBD: "Starsense AutoAlign V2",
    0xBF: "Mount Lights",
}

# Devices that originate requests. In a packet between a controller and another
# device, the command number belongs to the other device.
CONTROLLERS = frozenset({0x04, 0x0D, 0x0E, 0x20, 0x21, 0x22, Addr.SCANNER})

# Accessories that also send commands to the motors (the autoguider sends guiding
# corrections). Their traffic with a motor uses the motor's command names.
MOTOR_CLIENTS = frozenset({Addr.STARSENSE_AUTOGUIDER})
MOTORS = frozenset({Addr.AZM_MOTOR, Addr.ALT_MOTOR})

# Model identifiers returned by MTR_GET_MODEL on the azimuth/RA motor.
MOUNT_MODELS: Mapping[int, str] = {
    0x0001: "Nexstar GPS",
    0x0682: "Advanced GT",  # from the INDI celestronaux driver
    0x0783: "Nexstar SLT",
    0x0B83: "4/5SE",
    0x0C82: "6/8SE",
    0x1189: "CPC Deluxe",
    0x1283: "GT Series",
    0x1485: "AVX",
    0x1687: "Nexstar Evolution",
    0x1788: "CGX",
    0x1888: "CGX-L",
    0x1D8A: "Origin",
}

# Mount families, by the first byte of the model id, used when the exact id is
# not in MOUNT_MODELS. Every exact model must belong to a family that agrees
# with it; mount_table_problems() checks that.
MOUNT_FAMILIES: Mapping[int, str] = {
    # Known families:
    0x05: "CGE",
    0x06: "Advanced GT / CG-5",
    0x09: "CPC",
    0x0A: "GT",
    0x0D: "CGE Pro",
    0x0E: "CGEM DX",
    0x0F: "LCM",
    0x10: "SkyProdigy",
    0x13: "StarSeeker",
    0x15: "Cosmos",
    0x18: "CGX-L",
    0x19: "AstroFi",
    0x1A: "SkyWatcher",
    # Inferred from the exact ids in MOUNT_MODELS:
    0x07: "Nexstar SLT",
    0x0B: "4/5SE",
    0x0C: "6/8SE",
    0x11: "CPC Deluxe",
    0x12: "GT Series",
    0x14: "AVX",
    0x16: "Nexstar Evolution",
    0x17: "CGX",
    0x1D: "Origin",
}

# Replies of a single byte (the original Nexstar GPS answers 01) have no family byte.
_SINGLE_BYTE_MODELS = frozenset({0x0001})


def mount_table_problems() -> List[str]:
    """Exact models without a family, or whose family names a different mount."""
    problems = []
    for model, name in sorted(MOUNT_MODELS.items()):
        if model in _SINGLE_BYTE_MODELS:
            continue
        family = MOUNT_FAMILIES.get(model >> 8)
        if family is None:
            problems.append(f"0x{model:04x} {name}: no family 0x{model >> 8:02x}")
        elif not _same_mount(name, family):
            problems.append(f"0x{model:04x} {name}: family 0x{model >> 8:02x} is {family}")
    return problems


def _same_mount(model: str, family: str) -> bool:
    simplify = lambda text: text.lower().replace("-", " ")
    model, family = simplify(model), simplify(family)
    return model in family or family in model

_MOTOR_COMMANDS: Dict[int, str] = {
    0x01: "MTR_GET_POSITION",  # 16/24-bit BAM position
    0x02: "MTR_GOTO_FAST",  # goto at rate 9, 16/24-bit BAM position
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
    0x17: "MTR_GOTO_SLOW",  # goto at a slow, variable rate, 16/24-bit BAM position
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
    0x49: "MTR_GET_SWITCH_CALIBRATION",
    0x4A: "MTR_SET_PRN_VALUE",
    0x4B: "MTR_GET_PRN_VALUE",
    0x50: "MTR_SEND_WARNING",
    0x51: "MTR_SEND_ERROR",
    0x5B: "MTR_SET_PID_KP",
    0x5C: "MTR_SET_PID_KI",
    0x5D: "MTR_SET_PID_KD",
    0x5F: "MTR_ENABLE_PID_ANALYSIS",
    # Firmware programming (Paquette). MTR_PROGRAM_ENTER puts the motor
    # controller into flashing mode, where it stops working until it is reprogrammed.
    0x81: "MTR_PROGRAM_ENTER",
    0x82: "MTR_PROGRAM_INIT",
    0x83: "MTR_PROGRAM_DATA",
    0x84: "MTR_PROGRAM_END",
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
        0x02: "DEWHEATER_GET_LED_BRIGHTNESS",
        0x03: "DEWHEATER_SET_INPUT_LIMIT",
        0x04: "DEWHEATER_GET_LIMIT_POWER",
        0x10: "DEWHEATER_GET_POWER_PORTS",
        0x11: "DEWHEATER_GET_PORTS",
        0x12: "DEWHEATER_GET_STATUS",
        0x14: "DEWHEATER_ENABLE_PORT",
        0x16: "DEWHEATER_SET_AUTO_AGGRESSION",
        0x17: "DEWHEATER_SET_MANUAL_POWER",
        0x18: "DEWHEATER_GET_AMBIENT",
        0x19: "DEWHEATER_RECALIBRATE_SENSOR",
        0x1A: "DEWHEATER_IS_CALIBRATING",
        0x20: "DEWHEATER_SET_LED_BRIGHTNESS",
        0x21: "DEWHEATER_UNKNOWN_21",  # CPWI sends it at startup; a Celestron 2X replies ff
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
        0x55: "GPS_GET_HW_VER",
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
    0xB5: {0x10: "WIFI_SET_ENABLE", 0xFE: "WIFI_GET_FW_VER"},  # 0x10: Evolution WiFi on/off
    0xB6: {0x10: "BAT_GET_VOLTAGE", 0x18: "BAT_GET_MAX_CURRENT", 0xFE: "BAT_GET_FW_VER"},
    0xB7: {0x10: "CHG_MODE", 0xFE: "CHG_GET_FW_VER"},  # 0x10 both reads and sets
    # StarSense AutoGuider: names and payload layouts from community reverse engineering.
    0xB9: {
        0x01: "SSAG_START_PLATE_SOLVE",
        0x02: "SSAG_GET_PLATE_STATUS",
        0x03: "SSAG_START_CALIBRATE",
        0x04: "SSAG_GET_CALIBRATE_STATUS",
        0x07: "SSAG_UNKNOWN_07",
        0x20: "SSAG_SET_GUIDING",
        0x23: "SSAG_GET_GUIDING_DATA",
        0x30: "SSAG_SET_MOUNT_INFO",
        0x31: "SSAG_SET_GEOLOCATION",
        0x32: "SSAG_SET_TIME_NOW",
        0x45: "SSAG_UNKNOWN_45",
        0xFE: "SSAG_GET_FW_VER",
    },
    0xBB: {0xFE: "DEWHEATER_GET_FW_VER"},
    # StarSense AutoAlign V2: the StarSense AutoGuider's command set without its
    # guiding commands (0x2x). CPWI sends it mount info, location and time with
    # the same payloads as the AutoGuider (seen on real hardware).
    0xBD: {
        0x01: "SS2_START_PLATE_SOLVE",
        0x02: "SS2_GET_PLATE_STATUS",
        0x03: "SS2_START_CALIBRATE",
        0x04: "SS2_GET_CALIBRATE_STATUS",
        0x07: "SS2_UNKNOWN_07",
        0x30: "SS2_SET_MOUNT_INFO",
        0x31: "SS2_SET_GEOLOCATION",
        0x32: "SS2_SET_TIME_NOW",
        0x45: "SS2_UNKNOWN_45",
        0xFE: "SS2_GET_FW_VER",
    },
    0xBF: {0x10: "LIGHT_LEVEL", 0xFE: "LIGHT_GET_FW_VER"},  # 0x10 both reads and sets
}

# Commands every AUX device answers. A device's own table can give a more
# specific name (MTR_GET_VERSION, GPS_GET_FW_VER, ...), which then wins.
GENERIC_COMMANDS: Mapping[int, str] = {Cmd.GET_VERSION: "GET_VERSION"}

UNKNOWN = "Unknown"


MAX_NAME_LENGTH = 40
DEFAULT_USER_CATALOG = Path.home() / ".celestron-aux" / "catalog.json"


class CatalogError(Exception):
    """The user catalog file cannot be read or saved, or a name is not acceptable."""


class UserCatalog:
    """Your own device and command names, saved as JSON and layered over the built-in ones.

    File format (addresses and commands as hex strings)::

        {"devices": {"0x99": "My accessory"},
         "commands": {"0x10": {"0x77": "MTR_SOMETHING"}}}
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._devices: Dict[int, str] = {}
        self._commands: Dict[int, Dict[int, str]] = {}
        self.load_error: Optional[str] = None  # set when the file exists but cannot be read

    # --- file ---------------------------------------------------------------------

    def load(self) -> None:
        """Read the file, if there is one. A broken file is reported and left untouched."""
        if self.path is None or not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            devices = {_hex_key(k): _clean_name(v) for k, v in data.get("devices", {}).items()}
            commands = {
                _hex_key(device): {_hex_key(c): _clean_name(n) for c, n in names.items()}
                for device, names in data.get("commands", {}).items()
            }
        except (OSError, ValueError, AttributeError, CatalogError) as exc:
            self.load_error = f"{self.path}: {exc}"
            log.warning("could not read the user catalog %s", self.load_error)
            return
        with self._lock:
            self._devices, self._commands = devices, commands
            self.load_error = None

    def save(self) -> None:
        if self.path is None:
            raise CatalogError("No catalog file is configured.")
        if self.load_error:
            raise CatalogError(f"Not saving over a catalog file that could not be read ({self.load_error}).")
        with self._lock:
            data = {
                "devices": {f"0x{a:02x}": n for a, n in sorted(self._devices.items())},
                "commands": {f"0x{d:02x}": {f"0x{c:02x}": n for c, n in sorted(names.items())}
                             for d, names in sorted(self._commands.items()) if names},
            }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Write to a temporary file first, so a crash never leaves a half-written catalog.
        handle, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=".catalog-", suffix=".json")
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            json.dump(data, file, indent=2)
            file.write("\n")
        os.replace(temporary, self.path)

    # --- names --------------------------------------------------------------------

    def device(self, address: int) -> Optional[str]:
        with self._lock:
            return self._devices.get(address)

    def commands(self, device: int) -> Dict[int, str]:
        with self._lock:
            return dict(self._commands.get(device, {}))

    def set_device(self, address: int, name: Optional[str]) -> None:
        """Name a device, or forget your name for it with None."""
        _check_byte(address)
        with self._lock:
            if name is None:
                self._devices.pop(address, None)
            else:
                self._devices[address] = _clean_name(name)

    def set_command(self, device: int, command: int, name: Optional[str]) -> None:
        """Name a command of a device, or forget your name for it with None."""
        _check_byte(device)
        _check_byte(command)
        with self._lock:
            names = self._commands.setdefault(device, {})
            if name is None:
                names.pop(command, None)
            else:
                names[command] = _clean_name(name)

    def entries(self) -> List[Dict[str, Any]]:
        """Every name you have given, devices first."""
        with self._lock:
            items: List[Dict[str, Any]] = [
                {"device": a, "command": None, "name": n} for a, n in sorted(self._devices.items())
            ]
            items += [{"device": d, "command": c, "name": n}
                      for d, names in sorted(self._commands.items()) for c, n in sorted(names.items())]
        return items


def _hex_key(text: str) -> int:
    value = int(str(text), 16)
    _check_byte(value)
    return value


def _check_byte(value: int) -> None:
    if not 0 <= value <= 0xFF:
        raise CatalogError(f"0x{value:x} is not a bus address or command (0x00-0xff)")


def _clean_name(name: object) -> str:
    text = " ".join(str(name).split())
    if not text:
        raise CatalogError("A name cannot be empty.")
    if len(text) > MAX_NAME_LENGTH or not text.isprintable():
        raise CatalogError(f"Names must be printable and at most {MAX_NAME_LENGTH} characters.")
    return text


_user = UserCatalog()  # empty until use_user_catalog is called


def use_user_catalog(catalog: UserCatalog) -> None:
    """Make ``catalog`` the user names layered over the built-in tables."""
    global _user
    _user = catalog


def user_catalog() -> UserCatalog:
    return _user


def device_name(address: int) -> str:
    return _user.device(address) or DEVICE_NAMES.get(address, UNKNOWN)


def is_known_device(address: int) -> bool:
    return address in DEVICE_NAMES or _user.device(address) is not None


def known_addresses() -> List[int]:
    """Every address with a name, built-in or yours."""
    return sorted(set(DEVICE_NAMES) | {e["device"] for e in _user.entries() if e["command"] is None})


def _commands(device: int) -> Dict[int, str]:
    return {**GENERIC_COMMANDS, **_DEVICE_COMMANDS.get(device, {}), **_user.commands(device)}


def command_name(device: int, command: int) -> str:
    return _commands(device).get(command, UNKNOWN)


def is_known_command(device: int, command: int) -> bool:
    return command in _commands(device)


def commands_for(device: int) -> List[Tuple[int, str]]:
    """All known commands of ``device``, sorted by command number."""
    return sorted(_commands(device).items())


def mount_name(model: Optional[int]) -> str:
    """The exact model if known, else its family (marked as such), else Unknown."""
    if model is None:
        return "Not detected"
    if model in MOUNT_MODELS:
        return MOUNT_MODELS[model]
    family = MOUNT_FAMILIES.get(model >> 8) if model > 0xFF else None
    if family:
        return f"{family} family (0x{model:04x})"
    return f"{UNKNOWN} (0x{model:04x})"


def subject_device(source: int, destination: int) -> int:
    """Return the device a packet is about; command numbers are scoped by it."""
    if source == Addr.SCANNER:
        return destination
    if destination == Addr.SCANNER:
        return source
    if source in MOTOR_CLIENTS and destination in MOTORS:
        return destination
    return destination if source in CONTROLLERS else source
