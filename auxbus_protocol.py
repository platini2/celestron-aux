#!/usr/bin/env python

"""auxbus_protocol.py: Celestron AUXBUS protocol data and pure decode/encode logic.

No I/O, no globals, no threading — everything here is safe to unit test directly.
Shared by celestron.py and celestronui.py.
"""

from dataclasses import dataclass, field

PREAMBLE = 0x3b
PREAMBLE2 = 0x3c

DEGREE_SIGN = u'\N{DEGREE SIGN}'

# Starsense camera geometry. The two apps this module was extracted from had
# diverged on these (5.16 vs 6.88 degree FOV, opposite pixel-Y sign, different
# resolution) — standardized here on celestron.py's values.
STARSENSE_XFOV = 5.16
STARSENSE_YFOV = 5.16
STARSENSE_XRES = 960
STARSENSE_YRES = 960
STARSENSE_Y_SIGN = -1

MOUNTS = {
            0x01 : 'Nexstar GPS Legacy',
            0x0001 : 'Nexstar GPS',
            ### 0x05xx : 'CGE',
            ### 0x06xx : 'Advanced GT',
            0x0783 : 'Nexstar SLT',
            ### 0x09xx : 'CPC',
            ### 0x0axx : 'GT',
            0x0b83 : '4/5SE',
            0x0c82 : '6/8SE',
            ### 0x0dxx : 'CGE Pro',
            ### 0x0exx : 'CGEM DX',
            ### 0x0fxx : 'LCM',
            ### 0x10xx : 'Skyprodigy',
            0x1189 : 'CPC Deluxe',
            0x1283 : 'GT Series',
            ### 0x13xx : 'Starseeker',
            0x1485 : 'AVX',
            ### 0x15xx : 'Cosmos',
            0x1687 : 'Nexstar Evolution',
            0x1788 : 'CGX',
            0x1888 : 'CGX-L'}
            ### 0x19xx : 'Astrofi',
            ### 0x1axx : 'SkyWatcher'}

# Real hardware device addresses only. Each app's own scanner identity
# (0x2f for celestron.py, 0x22 for celestronui.py) is overlaid locally by the
# session rather than baked in here, since the two apps use different self-IDs.
DEVICES = {
            0x01 : 'Main Board',
            0x04 : 'Nexstar HC',
            0x0d : 'Nexstar+ HC',
            0x0e : 'Starsense HC',
            0x10 : 'AZM MC',
            0x11 : 'ALT MC',
            0x12 : 'Focuser',
            0x17 : 'Dew Heater Controller',
            0x20 : 'CPWI',
            0x21 : 'CFM',
            0x22 : 'Focuser Utility',
            0x30 : 'CGX RA Switch',
            0x31 : 'CGX DEC Switch',
            0x32 : 'CGX DEC Autoguide Pt',
            0xb0 : 'GPS',
            0xb2 : 'RTC',
            0xb3 : 'Skyportal Accessory',
            0xb4 : 'Starsense Camera',
            0xb5 : 'Nexstar EVO WiFi',
            0xb6 : 'Battery Power Cont',
            0xb7 : 'Charge Port',
            0xb8 : 'Starsense Camera SW',
            0xbb : 'Dew Heater Controller',
            0xbf : 'Mount Lights'}

# Controller device IDs, minus each app's own scanner ID (apps append their own).
BASE_CONTROLLERS = [0x04, 0x0d, 0x0e, 0x20, 0x21, 0x22]

COMMANDS = {
            (0x01, 0xfe) : 'MB_GET_FW_VER',
            (0x04, 0xfe) : 'NXS_GET_FW_VER',
            (0x0d, 0xfe) : 'NXS_GET_FW_VER',
            (0x0e, 0xfe) : 'NXS_GET_FW_VER',
            (0x10, 0x01) : 'MTR_GET_POSITION',		# Returns 16/24 bit BAM position
            (0x10, 0x02) : 'MTR_GOTO',			# Coarse, 16/24 bit BAM position
            (0x10, 0x04) : 'MTR_SET_POSITION',		# Resets to new 16/24 bit BAM position
            (0x10, 0x05) : 'MTR_GET_MODEL',		# Return the model number of the telescope
            (0x10, 0x06) : 'MTR_PTRACK',		# 16/24 bit speed [arcseconds/1024] + track rate
            (0x10, 0x07) : 'MTR_NTRACK',		# 16/24 bit speed [arcseconds/1024] - track rate
            (0x10, 0x08) : 'MTR_REMOTE_SWITCH_ALIVE',
            (0x10, 0x09) : 'MTR_SWITCH_STATE_CHANGE',
            (0x10, 0x0a) : 'MTR_AG_STATE_CHANGE 0x0A',
            (0x10, 0x0b) : 'MTR_MVSWITCH',		# ALT only, finds downstop switch
            (0x10, 0x0c) : 'MTR_PECTRAIN',		# AZM only, EQNorth/South must be on before sending this command
            (0x10, 0x0d) : 'MTR_PECPLAY',		# AZM only, EQNorth/South must be on before sending this command
            (0x10, 0x0e) : 'MTR_GET_PEC_BIN',		# Gets PEC bin
            (0x10, 0x10) : 'MTR_SET_POS_BACKLASH',	# Positive backlash, followed by 1 byte (0-99) 0=Off, 99=Max
            (0x10, 0x11) : 'MTR_SET_NEG_BACKLASH',	# Negative backlash, followed by 1 byte (0-99) 0=Off, 99=Max
            (0x10, 0x12) : 'MTR_IS_MVSWITCH_OVER',
            (0x10, 0x13) : 'MTR_IS_GOTO_OVER',
            (0x10, 0x14) : 'MTR_PEC_STATE_CHANGE',
            (0x10, 0x15) : 'MTR_IS_PECTRAIN_OVER',
            (0x10, 0x16) : 'MTR_CANCEL_PECTRAIN',
            (0x10, 0x17) : 'MTR_GOTO2',
            (0x10, 0x18) : 'MTR_IS_INDEX_FOUND',
            (0x10, 0x19) : 'MTR_FIND_INDEX',
            (0x10, 0x1a) : 'MTR_SET_USER_LIMIT_MIN',
            (0x10, 0x1b) : 'MTR_SET_USER_LIMIT_MAX',
            (0x10, 0x1c) : 'MTR_GET_USER_LIMIT_MIN',
            (0x10, 0x1d) : 'MTR_GET_USER_LIMIT_MAX',
            (0x10, 0x1e) : 'MTR_IS_USER_LIMIT_ENABLED',
            (0x10, 0x1f) : 'MTR_SET_USER_LIMIT_ENABLED',
            (0x10, 0x20) : 'MTR_SET_CUSTOM_RATE9',
            (0x10, 0x21) : 'MTR_GET_CUSTOM_RATE9',
            (0x10, 0x22) : 'MTR_SET_CUSTOM_RATE9_ENA',
            (0x10, 0x23) : 'MTR_GET_CUSTOM_RATE9_ENA',
            (0x10, 0x24) : 'MTR_PMSLEW_RATE',
            (0x10, 0x25) : 'MTR_NMSLEW_RATE',
            (0x10, 0x26) : 'MTR_AUX_GUIDE',
            (0x10, 0x27) : 'MTR_IS_AUX_GUIDE_ACTIVE',
            (0x10, 0x2a) : 'MTR_HS_CALIBRATION_ENABLE',
            (0x10, 0x2b) : 'MTR_IS_HS_CALIBRATED',
            (0x10, 0x2c) : 'MTR_GET_HS_POSITIONS',
            (0x10, 0x30) : 'MTR_EEPROM_READ',
            (0x10, 0x31) : 'MTR_EEPROM_WRITE',
            (0x10, 0x32) : 'MTR_PROGRAM_READ',
            (0x10, 0x38) : 'MTR_CORDWRAP_ON',
            (0x10, 0x39) : 'MTR_CORDWRAP_OFF',
            (0x10, 0x3a) : 'MTR_SET_CORDWRAP_POSITION',
            (0x10, 0x3b) : 'MTR_IS_CORDWRAP_ON',
            (0x10, 0x3c) : 'MTR_GET_CORDWRAP_POSITION',
            (0x10, 0x3d) : 'MTR_SET_SHUTTER',
            (0x10, 0x40) : 'MTR_GET_POS_BACKLASH',
            (0x10, 0x41) : 'MTR_GET_NEG_BACKLASH',
            (0x10, 0x46) : 'MTR_SET_AUTOGUIDE_RATE',
            (0x10, 0x47) : 'MTR_GET_AUTOGUIDE_RATE',
            (0x10, 0x48) : 'MTR_SET_SWITCH_CALIBRATION',
            (0x10, 0x49) : 'MTR_SET_SWITCH_CALIBRATION',
            (0x10, 0x4a) : 'MTR_SET_PRN_VALUE',
            (0x10, 0x4b) : 'MTR_GET_PRN_VALUE',
            (0x10, 0x50) : 'MTR_SEND_WARNING',
            (0x10, 0x51) : 'MTR_SEND_ERROR',
            (0x10, 0x5b) : 'MTR_SET_PID_KP',
            (0x10, 0x5c) : 'MTR_SET_PID_KI',
            (0x10, 0x5d) : 'MTR_SET_PID_KD',
            (0x10, 0x5f) : 'MTR_ENABLE_PID_ANALYSIS',
            (0x10, 0x8a) : 'MTR_PROGRAMMER_ENABLE',
            (0x10, 0xee) : 'MTR_GET_HARDSWITCH_ENABLE',
            (0x10, 0xef) : 'MTR_SET_HARDSWITCH_ENABLE',
            (0x10, 0xfa) : 'MTR_GET_CHIPVERSION',
            (0x10, 0xfb) : 'MTR_GET_BOOTVERSION',
            (0x10, 0xfc) : 'MTR_IS_APPROACH_DIR_NEG',
            (0x10, 0xfd) : 'MTR_SET_APPROACH_DIR_NEG',
            (0x10, 0xfe) : 'MTR_GET_VERSION',
            (0x11, 0x01) : 'MTR_GET_POSITION',
            (0x11, 0x02) : 'MTR_GOTO',
            (0x11, 0x04) : 'MTR_SET_POSITION',
            (0x11, 0x05) : 'MTR_GET_MODEL',
            (0x11, 0x06) : 'MTR_PTRACK',
            (0x11, 0x07) : 'MTR_NTRACK',
            (0x11, 0x08) : 'MTR_REMOTE_SWITCH_ALIVE',
            (0x11, 0x09) : 'MTR_SWITCH_STATE_CHANGE',
            (0x11, 0x0a) : 'MTR_AG_STATE_CHANGE 0x0A',
            (0x11, 0x0b) : 'MTR_MVSWITCH',
            (0x11, 0x0c) : 'MTR_PECTRAIN',
            (0x11, 0x0d) : 'MTR_PECPLAY',
            (0x11, 0x0e) : 'MTR_GET_PEC_BIN',
            (0x11, 0x10) : 'MTR_SET_POS_BACKLASH',
            (0x11, 0x11) : 'MTR_SET_NEG_BACKLASH',
            (0x11, 0x12) : 'MTR_IS_MVSWITCH_OVER',
            (0x11, 0x13) : 'MTR_IS_GOTO_OVER',
            (0x11, 0x14) : 'MTR_PEC_STATE_CHANGE',
            (0x11, 0x15) : 'MTR_IS_PECTRAIN_OVER',
            (0x11, 0x16) : 'MTR_CANCEL_PECTRAIN',
            (0x11, 0x17) : 'MTR_GOTO2',
            (0x11, 0x18) : 'MTR_IS_INDEX_FOUND',
            (0x11, 0x19) : 'MTR_FIND_INDEX',
            (0x11, 0x1a) : 'MTR_SET_USER_LIMIT_MIN',
            (0x11, 0x1b) : 'MTR_SET_USER_LIMIT_MAX',
            (0x11, 0x1c) : 'MTR_GET_USER_LIMIT_MIN',
            (0x11, 0x1d) : 'MTR_GET_USER_LIMIT_MAX',
            (0x11, 0x1e) : 'MTR_IS_USER_LIMIT_ENABLED',
            (0x11, 0x1f) : 'MTR_SET_USER_LIMIT_ENABLED',
            (0x11, 0x20) : 'MTR_SET_CUSTOM_RATE9',
            (0x11, 0x21) : 'MTR_GET_CUSTOM_RATE9',
            (0x11, 0x22) : 'MTR_SET_CUSTOM_RATE9_ENA',
            (0x11, 0x23) : 'MTR_GET_CUSTOM_RATE9_ENA',
            (0x11, 0x24) : 'MTR_PMSLEW_RATE',
            (0x11, 0x25) : 'MTR_NMSLEW_RATE',
            (0x11, 0x26) : 'MTR_AUX_GUIDE',
            (0x11, 0x27) : 'MTR_IS_AUX_GUIDE_ACTIVE',
            (0x11, 0x2a) : 'MTR_HS_CALIBRATION_ENABLE',
            (0x11, 0x2b) : 'MTR_IS_HS_CALIBRATED',
            (0x11, 0x2c) : 'MTR_GET_HS_POSITIONS',
            (0x11, 0x30) : 'MTR_EEPROM_READ',
            (0x11, 0x31) : 'MTR_EEPROM_WRITE',
            (0x11, 0x32) : 'MTR_PROGRAM_READ',
            (0x11, 0x38) : 'MTR_CORDWRAP_ON',
            (0x11, 0x39) : 'MTR_CORDWRAP_OFF',
            (0x11, 0x3a) : 'MTR_SET_CORDWRAP_POSITION',
            (0x11, 0x3b) : 'MTR_IS_CORDWRAP_ON',
            (0x11, 0x3c) : 'MTR_GET_CORDWRAP_POSITION',
            (0x11, 0x3d) : 'MTR_SET_SHUTTER',
            (0x11, 0x40) : 'MTR_GET_POS_BACKLASH',
            (0x11, 0x41) : 'MTR_GET_NEG_BACKLASH',
            (0x11, 0x46) : 'MTR_SET_AUTOGUIDE_RATE',
            (0x11, 0x47) : 'MTR_GET_AUTOGUIDE_RATE',
            (0x11, 0x48) : 'MTR_SET_SWITCH_CALIBRATION',
            (0x11, 0x49) : 'MTR_SET_SWITCH_CALIBRATION',
            (0x11, 0x4a) : 'MTR_SET_PRN_VALUE',
            (0x11, 0x4b) : 'MTR_GET_PRN_VALUE',
            (0x11, 0x50) : 'MTR_SEND_WARNING',
            (0x11, 0x51) : 'MTR_SEND_ERROR',
            (0x11, 0x5b) : 'MTR_SET_PID_KP',
            (0x11, 0x5c) : 'MTR_SET_PID_KI',
            (0x11, 0x5d) : 'MTR_SET_PID_KD',
            (0x11, 0x5f) : 'MTR_ENABLE_PID_ANALYSIS',
            (0x11, 0x8a) : 'MTR_PROGRAMMER_ENABLE',
            (0x11, 0xee) : 'MTR_GET_HARDSWITCH_ENABLE',
            (0x11, 0xef) : 'MTR_SET_HARDSWITCH_ENABLE',
            (0x11, 0xfa) : 'MTR_GET_CHIPVERSION',
            (0x11, 0xfb) : 'MTR_GET_BOOTVERSION',
            (0x11, 0xfc) : 'MTR_IS_APPROACH_DIR_NEG',
            (0x11, 0xfd) : 'MTR_SET_APPROACH_DIR_NEG',
            (0x11, 0xfe) : 'MTR_GET_VERSION',
            (0x12, 0x01) : 'FOCUS_GET_POSITION',
            (0x12, 0x02) : 'FOCUS_GOTO_FAST',
            (0x12, 0x05) : 'FOCUS_GET_MODEL',
            (0x12, 0x13) : 'FOCUS_SLEW_DONE',
            (0x12, 0x24) : 'FOCUS_MOVE_POS',
            (0x12, 0x25) : 'FOCUS_MOVE_NEG',
            (0x12, 0x2a) : 'FOCUS_CALIBRATION_ENABLE',
            (0x12, 0x2b) : 'FOCUS_IS_CALIBRATED',
            (0x12, 0x2c) : 'FOCUS_GET_CALIBRATION_POSITIONS',
            (0x12, 0x3b) : 'FOCUS_XXX',
            (0x12, 0xfe) : 'FUCUS_GET_FW_VER',
            (0x17, 0x00) : 'DEWHEATER_GET_CURRENT_POWER',
            (0x17, 0x04) : 'DEWHEATER_GET_LIMIT_POWER',
            (0x17, 0x10) : 'DEWHEATER_GET_POWER_PORTS',
            (0x17, 0x11) : 'DEWHEATER_GET_PORTS',
            (0x17, 0x12) : 'DEWHEATER_GET_STATUS',
            (0x17, 0x18) : 'DEWHEATER_GET_AMBIENT',
            (0x20, 0xfe) : 'NXS_GET_FW_VER',
            (0xb0, 0x01) : 'GPS_GET_LAT',
            (0xb0, 0x02) : 'GPS_GET_LONG',
            (0xb0, 0x03) : 'GPS_GET_DATE',
            (0xb0, 0x04) : 'GPS_GET_YEAR',
            (0xb0, 0x07) : 'GPS_GET_SAT_INFO',
            (0xb0, 0x08) : 'GPS_GET_RCVR_STATUS',
            (0xb0, 0x33) : 'GPS_GET_TIME',
            (0xb0, 0x36) : 'GPS_TIME_VALID',
            (0xb0, 0x37) : 'GPS_LINKED',
            (0xb0, 0xa0) : 'GPS_GET_COMPASS',
            (0xb0, 0xfe) : 'GPS_GET_FW_VER',
            (0xb3, 0xfe) : 'WIFI_GET_FW_VER',
            (0xb4, 0x3e) : 'SS_SET_ALIGN_CENTER',
            (0xb4, 0x3f) : 'SS_GET_ALIGN_CENTER',
            (0xb4, 0x90) : 'SS_RESET_CAPTURE',
            (0xb4, 0x91) : 'SS_GET_STATUS',
            (0xb4, 0x92) : 'SS_GET_CAPTURE_DATA',
            (0xb4, 0x94) : 'SS_START_CAPTURE',
            (0xb4, 0x9f) : 'SS_GET_IMAGE3',
            (0xb4, 0xfe) : 'SS_GET_FW_VER',
            (0xb5, 0xfe) : 'WIFI_GET_FW_VER',
            (0xb6, 0x10) : 'BAT_GET_VOLTAGE',
            (0xb6, 0x18) : 'BAT_GET_CURRENT',
            (0xb6, 0xfe) : 'BAT_GET_FW_VER',
            (0xb7, 0x10) : 'CHG_GET_MODE',
            (0xb7, 0xfe) : 'CHG_GET_FW_VER',
            (0xbb, 0xfe) : 'DEWHEATER_GET_FW_VER',
            (0xbf, 0x10) : 'LIGHT_GET_LEVEL',
            (0xbf, 0xfe) : 'LIGHT_GET_FW_VER'}


def twos_comp(val, bits):
    """compute the 2's complement of int value val"""
    if (val & (1 << (bits - 1))) != 0:
        val = val - (1 << bits)
    return val


def tosigned24(hexnum):
    n = int(hexnum, 16)
    n = n & 0xffffff
    return n | (-(n & 0x800000))


def hextoposition(hexnum):
    return tosigned24(hexnum) / pow(2, 24) * 360


def decdeg2dms(dd):
    d = int(dd)
    m = int((abs(dd) - abs(d)) * 60)
    s = round((abs(dd) - abs(d) - m / 60) * 3600.00, 2)
    return d, m, s


def checksum(byte_values):
    """AUXBUS checksum: (65536 - sum(byte_values)) & 0xff."""
    return (65536 - sum(byte_values)) & 0xff


def build_frame(sender, receiver, command, value_hex):
    """Build a 0x3b command/response frame as a hex string (preamble..checksum)."""
    commandvalue = [int(value_hex[c:c + 2], 16) for c in range(0, len(value_hex), 2)]
    length = 3 + len(value_hex) // 2
    chk = checksum([length, sender, receiver, command] + commandvalue)
    return "{:02x}{:02x}{:02x}{:02x}{:02x}{}{:02x}".format(
        PREAMBLE, length, sender, receiver, command, value_hex, chk)


@dataclass
class ParsedFrame:
    sender: int = 0
    receiver: int = 0
    command: int = 0
    commandvalue: list = field(default_factory=list)
    checksum_ok: bool = False


def parse_frame(msg_hex):
    """Parse a 0x3b command/response frame. Never raises — malformed or
    truncated input yields a ParsedFrame with checksum_ok=False."""
    if len(msg_hex) < 2 * 6:
        return ParsedFrame()
    try:
        length = int(msg_hex[2:4], 16)
        sender = int(msg_hex[4:6], 16)
        receiver = int(msg_hex[6:8], 16)
        command = int(msg_hex[8:10], 16)
        frame_len = 2 * (length + 3)
        if len(msg_hex) != frame_len:
            return ParsedFrame(sender, receiver, command, [], False)
        commandvalue = [hex(int(msg_hex[c:c + 2], 16)) for c in range(10, 2 * (2 + length), 2)]
        checksum_byte = int(msg_hex[2 * (2 + length):2 * (2 + length) + 2], 16)
        payload_bytes = [int(msg_hex[c:c + 2], 16) for c in range(2, 2 * (2 + length), 2)]
        ok = checksum(payload_bytes) == checksum_byte
        return ParsedFrame(sender, receiver, command, commandvalue, ok)
    except ValueError:
        return ParsedFrame()


def _decode_command_value(sender, device, command, commandvalue):
    if hex(command) == '0xfe':
        if len(commandvalue) < 4:
            commandvalue = '.'.join([format(int(c, 16)) for c in commandvalue])
        else:
            commandvalue = format(int(commandvalue[0], 16)) + '.' + format(int(commandvalue[1], 16)) + '.' + str(int(format(int(commandvalue[2], 16), '02x') + format(int(commandvalue[3], 16), '02x'), 16))
    elif hex(device) == '0x10' or hex(device) == '0x11':
        if hex(command) == '0x1' or hex(command) == '0x2' or hex(command) == '0x3a' or hex(command) == '0x3c':
            commandvaluehex = ''.join([format(int(c, 16), '02x') for c in commandvalue])
            latitude = twos_comp(int(commandvaluehex, 16), 24)
            latitude = latitude * 360 / pow(2, 24)
            d, m, s = decdeg2dms(latitude)
            commandvalue = format(d) + '°' + format(m) + '\'' + format(s) + '"'
        elif hex(command) == '0x17' or hex(command) == '0x6' or hex(command) == '0x7':
            if sender == device:
                if hex(int(commandvalue[0], 16)) == '0x1': commandvalue = '1 - ACK'
            else:
                commandvaluehex = ''.join([format(int(c, 16), '02x') for c in commandvalue])
                latitude = twos_comp(int(commandvaluehex, 16), 24)
                latitude = latitude * 360 / pow(2, 24)
                d, m, s = decdeg2dms(latitude)
                commandvalue = format(d) + '°' + format(m) + '\'' + format(s) + '"'
        elif hex(command) == '0x5':
            commandvalue = format(''.join([format(int(c, 16), '02x') for c in commandvalue]))
        elif hex(command) == '0x12':
            if hex(int(commandvalue[0], 16)) == '0x0': commandvalue = '0 - False'
            elif hex(int(commandvalue[0], 16)) == '0x1': commandvalue = '1 - True'
        elif hex(command) == '0x13':
            if hex(int(commandvalue[0], 16)) == '0x0': commandvalue = '0 - Not Done'
            elif hex(int(commandvalue[0], 16)) == '0x1': commandvalue = '1 - Check'
            elif hex(int(commandvalue[0], 16)) == '0xff': commandvalue = '255 - Done'
        elif hex(command) == '0x23':
            if hex(int(commandvalue[0], 16)) == '0x0': commandvalue = '0 - Disabled'
            elif hex(int(commandvalue[0], 16)) == '0x1': commandvalue = '1 - Enabled'
        elif hex(command) == '0x24' or hex(command) == '0x25' or hex(command) == '0x40' or hex(command) == '0x41':
            commandvalue = format(int(commandvalue[0], 16))
        elif hex(command) == '0x3b':
            if hex(int(commandvalue[0], 16)) == '0x0': commandvalue = '0 - Disabled'
            elif hex(int(commandvalue[0], 16)) == '0xff': commandvalue = '255 - Enabled'
        elif hex(command) == '0x46' or hex(command) == '0x47':
            commandvalue = format(100 * int(commandvalue[0], 16) / 256)
        elif hex(command) == '0xfc' or hex(command) == '0xfd':
            if hex(int(commandvalue[0], 16)) == '0x0': commandvalue = '0 - Positive'
            elif hex(int(commandvalue[0], 16)) == '0x1': commandvalue = '1 - Negative'
    elif hex(device) == '0xb0':
        if hex(command) == '0x1' or hex(command) == '0x2':
            commandvaluehex = ''.join([format(int(c, 16), '02x') for c in commandvalue])
            latitude = twos_comp(int(commandvaluehex, 16), 24)
            latitude = latitude * 360 / pow(2, 24)
            d, m, s = decdeg2dms(latitude)
            commandvalue = format(d) + '°' + format(m) + '\'' + format(s) + '"'
        elif hex(command) == '0x3':
            commandvalue = format(int(commandvalue[0], 16)) + '/' + format(int(commandvalue[1], 16))
        elif hex(command) == '0x4':
            commandvalue = format(int(''.join([format(int(c, 16), '02x') for c in commandvalue]), 16))
        elif hex(command) == '0x7':
            commandvalue = format(int(commandvalue[0], 16)) + ' - ' + format(int(commandvalue[1], 16))
        elif hex(command) == '0x33':
            commandvalue = format(int(commandvalue[0], 16)) + ':' + format(int(commandvalue[1], 16)) + ':' + format(int(commandvalue[2], 16))
        elif hex(command) == '0x36' or hex(command) == '0x37':
            if hex(int(commandvalue[0], 16)) == '0x0': commandvalue = '0 - No'
            elif hex(int(commandvalue[0], 16)) == '0x1': commandvalue = '1 - Yes'
    elif hex(device) == '0xb4':
        if hex(command) == '0x3f':
            if len(commandvalue) == 8:
                centerx = ''.join([format(int(c, 16), '02x') for c in reversed(commandvalue[0:4])])
                centery = ''.join([format(int(c, 16), '02x') for c in reversed(commandvalue[4:8])])
                commandvalue = format(int(centerx, 16)) + ' - ' + format(int(centery, 16))
            else:
                commandvalue = format(int(commandvalue[0], 16))
        if hex(command) == '0x91' or hex(command) == '0x92' or hex(command) == '0x94' or hex(command) == '0x9f':
            if hex(int(commandvalue[0], 16)) == '0x1': commandvalue = '1 (Error)' + ' - Stars: ' + format(int(commandvalue[1], 16))
            elif hex(int(commandvalue[0], 16)) == '0x3': commandvalue = '3 (Ready)' + ' - Stars: ' + format(int(commandvalue[1], 16))
            elif hex(int(commandvalue[0], 16)) == '0x9': commandvalue = '9 (Finished)' + ' - Stars: ' + format(int(commandvalue[1], 16))
    elif hex(device) == '0x17':
        if hex(command) == '0x0':
            commandvalue = format(int(''.join([format(int(c, 16), '02x') for c in commandvalue[0:2]]), 16) / 1000) + ' V - ' + format(int(''.join([format(int(c, 16), '02x') for c in commandvalue[2:4]]), 16) / 1000) + ' A '
        if hex(command) == '0x4':
            commandvalue = format(int(''.join([format(int(c, 16), '02x') for c in commandvalue[0:2]]), 16) / 1000) + ' A - ' + format(int(''.join([format(int(c, 16), '02x') for c in commandvalue[2:4]]), 16) / 1000) + ' A '
        if hex(command) == '0x10':
            commandvalue = format(int(commandvalue[0], 16))
        elif hex(command) == '0x11':
            if hex(sender) != '0x17':
                if hex(int(commandvalue[0], 16)) == '0x0': commandvalue = '0 - Get 12V Ports'
                elif hex(int(commandvalue[0], 16)) == '0x1': commandvalue = '1 - Get Dew Heater Ports'
            else:
                commandvalue = format(int(commandvalue[0], 16))
        elif hex(command) == '0x12':
            if hex(sender) != '0x17':
                if hex(int(commandvalue[0], 16)) == '0x0': commandvalue = '0 - Port 1'
                elif hex(int(commandvalue[0], 16)) == '0x1': commandvalue = '1 - Port 2'
            else:
                if hex(int(commandvalue[1], 16)) == '0x0': mode = 'Manual'
                elif hex(int(commandvalue[1], 16)) == '0x1': mode = 'Auto'
                if hex(int(commandvalue[2], 16)) == '0x0':
                    commandvalue = 'Port ' + format(int(commandvalue[0], 16)) + ' - ' + mode + ' - Power: Off ' + format(int(commandvalue[2], 16)) + ' - Agression ' + format(int(commandvalue[5], 16))
                else:
                    commandvaluehex = ''.join([format(int(c, 16), '02x') for c in commandvalue[6:10]])
                    temp = int(commandvaluehex, 16) / 1000
                    commandvalue = 'Port ' + format(int(commandvalue[0], 16)) + ' - ' + mode + ' - Power: On ' + format(int(commandvalue[2], 16)) + ' - Agression ' + format(int(commandvalue[5], 16)) + ' - Temp ' + format(temp)
        elif hex(command) == '0x18':
            commandvaluehex = ''.join([format(int(c, 16), '02x') for c in commandvalue[0:4]])
            temp = int(commandvaluehex, 16) / 1000
            commandvaluehex = ''.join([format(int(c, 16), '02x') for c in commandvalue[4:8]])
            dew = int(commandvaluehex, 16) / 1000
            commandvalue = 'Temp: ' + format(temp) + '°C Dew: ' + format(dew) + '°C Hum: ' + format(int(commandvalue[8], 16)) + '%'
    else:
        commandvaluehex = ''.join([format(int(c, 16), '02x') for c in commandvalue])
        commandvalue = commandvaluehex
    return commandvalue


def decode_command_value(sender, device, command, commandvalue):
    """Interpret a decoded command's payload bytes into a human-readable string.

    Falls back to a raw hex dump instead of raising if the payload is shorter
    or shaped differently than a given command normally expects (e.g. noise on
    the bus, or a command variant this table doesn't know about).
    """
    fallback = ''.join(format(int(c, 16), '02x') for c in commandvalue)
    try:
        return _decode_command_value(sender, device, command, commandvalue)
    except (IndexError, ValueError, KeyError):
        return fallback


def decode_starsense_star(msg, xfov=STARSENSE_XFOV, yfov=STARSENSE_YFOV):
    msg = "".join(reversed([msg[i:i + 2] for i in range(0, len(msg), 2)]))
    bx, by = int(msg[0:2], 16) - 64, int(msg[8:10], 16) - 64
    dx, mx, sx = decdeg2dms(twos_comp(int(msg[2:8], 16), 24) * xfov / pow(2, 24))
    dy, my, sy = decdeg2dms(twos_comp(int(msg[10:16], 16), 24) * yfov / pow(2, 24))
    px = format(dx) + DEGREE_SIGN + format(mx) + '\'' + format(sx) + '"'
    py = format(dy) + DEGREE_SIGN + format(my) + '\'' + format(sy) + '"'
    return str(bx) + " - " + "{:<11}".format(px) + " - " + str(by) + " - " + "{:<11}".format(py)


def starsense_pixel(msg, xres=STARSENSE_XRES, yres=STARSENSE_YRES, y_sign=STARSENSE_Y_SIGN):
    msg = "".join(reversed([msg[i:i + 2] for i in range(0, len(msg), 2)]))
    bx, by = int(msg[0:2], 16) - 64, int(msg[8:10], 16) - 64
    pixelx = int(xres / 2 + (twos_comp(int(msg[2:8], 16), 24) * xres / pow(2, 24)))
    pixely = int(yres / 2 + y_sign * (twos_comp(int(msg[10:16], 16), 24) * yres / pow(2, 24)))
    return pixelx, pixely, bx, by
