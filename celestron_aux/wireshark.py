"""Wireshark support: raw captures as pcap files, and a Lua dissector for them.

A raw capture (see ``transport.playback``) holds the bytes as they arrived, in
arbitrary chunks. ``capture_to_pcap`` re-frames them, so every pcap record is
one complete frame:

* an AUX packet (``3B ...``) or a StarSense bulk frame (``3C ...``), byte for
  byte as on the bus, corrupt frames included;
* a mark, as the text ``#MARK <text>``.

Records use link type USER0 (147), the one Wireshark keeps for private
protocols. ``dissector_lua`` writes a dissector registered on USER0, and on TCP
port 2000 for live captures of a SkyPortal or Evolution Wi-Fi module (with
reassembly, since frames can span TCP segments). It names devices and commands
from the catalog (built-in names and your own), checks checksums and decodes
payload values like AUXBUS Scanner does: every decoder in ``decoding.py`` has a
Lua port in ``wireshark_decoders.lua``, and a test checks that both give the
same text on thousands of payloads.
"""

from __future__ import annotations

import re
import struct
from importlib import resources
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple, Union

from . import __version__, catalog, decoding
from .protocol.framing import FrameParser
from .transport.playback import load_capture
from .transport.tcp import DEFAULT_PORT as DEFAULT_TCP_PORT

LINKTYPE_USER0 = 147
MARK_RECORD_PREFIX = b"#MARK "
_SNAPLEN = 0x40000

_Entry = Tuple[Optional[float], Union[bytes, str]]


# --- pcap ---------------------------------------------------------------------

def frames_from_capture(entries: Iterable[_Entry]) -> List[Tuple[float, bytes]]:
    """Turn raw capture entries into (timestamp, record) pairs, one per frame or mark.

    A frame gets the timestamp of the chunk that completed it. Lines without a
    timestamp take the previous one (0 at the start). Bytes that belong to no
    frame are dropped, as the scanner drops them.
    """
    parser = FrameParser()
    records: List[Tuple[float, bytes]] = []
    last = 0.0
    for timestamp, item in entries:
        if timestamp is not None:
            last = timestamp
        if isinstance(item, str):
            records.append((last, MARK_RECORD_PREFIX + item.encode("utf-8")))
            continue
        records.extend((last, parsed.raw) for parsed in parser.feed(item))
    return records


def pcap_bytes(records: Iterable[Tuple[float, bytes]]) -> bytes:
    """A classic pcap file (microsecond timestamps, little-endian) holding ``records``."""
    out = bytearray(struct.pack("<IHHiIII", 0xA1B2C3D4, 2, 4, 0, 0, _SNAPLEN, LINKTYPE_USER0))
    for timestamp, data in records:
        micros = max(0, round(timestamp * 1_000_000))
        seconds, fraction = divmod(micros, 1_000_000)
        out += struct.pack("<IIII", seconds, fraction, len(data), len(data))
        out += data
    return bytes(out)


def capture_to_pcap(capture: Path, destination: Path) -> int:
    """Convert a raw capture file to a pcap file. Returns the number of records."""
    records = frames_from_capture(load_capture(capture))
    destination.write_bytes(pcap_bytes(records))
    return len(records)


# --- Lua dissector ------------------------------------------------------------

def _lua_string(text: str) -> str:
    """A Lua string literal. Catalog names are printable, so quotes and backslashes are enough."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _lua_set(values: Iterable[int]) -> str:
    return "{" + ", ".join(f"[0x{v:02x}]=true" for v in sorted(values)) + "}"


def _lua_names(names: Iterable[Tuple[int, str]], indent: str) -> str:
    return "".join(f"{indent}[0x{key:02x}] = {_lua_string(name)},\n" for key, name in names)


def _decoders_source() -> str:
    return resources.files(__package__).joinpath("wireshark_decoders.lua").read_text(encoding="utf-8")


def lua_decoder_names() -> List[str]:
    """The decoders ported to Lua, by their Python name."""
    return re.findall(r"^D\.(\w+) = function", _decoders_source(), flags=re.MULTILINE)


def unported_decoders() -> List[str]:
    """Python payload decoders without a Lua port (their payloads stay hex in Wireshark)."""
    ported = set(lua_decoder_names())
    return sorted({fn.__name__ for fn in decoding._DECODERS.values()} - ported)


def _lua_decoders() -> str:
    """The mount tables, the ported decoders and which decoder each (device, command) uses."""
    ported = set(lua_decoder_names())
    by_device: Dict[Optional[int], List[Tuple[int, str]]] = {}
    for (device, command), decoder in sorted(decoding._DECODERS.items(), key=lambda i: (i[0][0] or -1, i[0][1])):
        if decoder.__name__ in ported:
            by_device.setdefault(device, []).append((command, decoder.__name__))
    def entries(items: List[Tuple[int, str]], indent: str) -> str:
        return "".join(f"{indent}[0x{c:02x}] = D.{name},\n" for c, name in items)
    table = "".join(f"  [0x{d:02x}] = {{\n{entries(items, '    ')}  }},\n"
                    for d, items in sorted((d, i) for d, i in by_device.items() if d is not None))
    return (
        "local MOUNT_MODELS = {\n" + "".join(f"  [0x{m:04x}] = {_lua_string(n)},\n"
                                         for m, n in sorted(catalog.MOUNT_MODELS.items())) + "}\n"
        + "local MOUNT_FAMILIES = {\n" + _lua_names(sorted(catalog.MOUNT_FAMILIES.items()), "  ") + "}\n\n"
        + _decoders_source()
        + "\n-- Which decoder each (device, command) uses, from decoding.py's registry.\n"
        + "local DECODERS = {\n" + table + "}\n"
        + "local ANY_DECODERS = {\n" + entries(by_device.get(None, []), "  ") + "}\n"
    )


def dissector_lua() -> str:
    """The Lua source of a Wireshark dissector for pcap files made by ``capture_to_pcap``."""
    devices = [(address, catalog.device_name(address)) for address in catalog.known_addresses()]
    commands = ""
    for address in range(0x100):
        names = [(c, n) for c, n in catalog.commands_for(address) if c not in catalog.GENERIC_COMMANDS
                 or n != catalog.GENERIC_COMMANDS[c]]
        if names:
            commands += f"  [0x{address:02x}] = {{\n{_lua_names(names, '    ')}  }},\n"
    generic = _lua_names(sorted(catalog.GENERIC_COMMANDS.items()), "  ")
    return _LUA_TEMPLATE.format(
        version=__version__,
        devices=_lua_names(devices, "  "),
        commands=commands,
        generic=generic,
        controllers=_lua_set(catalog.CONTROLLERS),
        motor_clients=_lua_set(catalog.MOTOR_CLIENTS),
        motors=_lua_set(catalog.MOTORS),
        scanner=f"0x{catalog.Addr.SCANNER:02x}",
        linktype=LINKTYPE_USER0,
        decoders=_lua_decoders(),
        tcp_port=DEFAULT_TCP_PORT,
    )


def write_dissector(destination: Path) -> None:
    destination.write_text(dissector_lua(), encoding="utf-8")


# Braces that belong to Lua are doubled for str.format.
_LUA_TEMPLATE = """\
-- Wireshark dissector for the Celestron AUX bus.
-- Generated by AUXBUS Scanner {version} (celestron-aux --export-dissector), with
-- the device and command names known when it was made, your own included.
--
-- Install: copy this file into Wireshark's personal Lua plugins folder
-- (Help > About Wireshark > Folders > Personal Lua Plugins; on a Mac,
-- Wireshark > About Wireshark), then restart Wireshark or reload the Lua plugins
-- (Ctrl+Shift+L, or Cmd+Shift+L on a Mac). It decodes:
--   * pcap files from AUXBUS Scanner's --export-pcap (link type USER0, {linktype});
--   * live Wi-Fi traffic to a SkyPortal or Evolution module, TCP port {tcp_port}
--     (change it under Preferences > Protocols > CELESTRON_AUX).
-- Filter with "celestron_aux", for example celestron_aux.cmd_name == "MTR_GOTO_FAST".

local aux = Proto("celestron_aux", "Celestron AUX bus")

local DEVICES = {{
{devices}}}

-- Commands are named by the device the packet is about (see subject_device).
local COMMANDS = {{
{commands}}}

-- Commands every device answers, unless its own table names them.
local GENERIC = {{
{generic}}}

local CONTROLLERS = {controllers}
local MOTOR_CLIENTS = {motor_clients}
local MOTORS = {motors}
local SCANNER = {scanner}

local function device_name(address)
  return DEVICES[address] or "Unknown"
end

local function subject_device(source, destination)
  if source == SCANNER then return destination end
  if destination == SCANNER then return source end
  if MOTOR_CLIENTS[source] and MOTORS[destination] then return destination end
  if CONTROLLERS[source] then return destination end
  return source
end

local function command_name(device, command)
  local names = COMMANDS[device]
  return (names and names[command]) or GENERIC[command] or "Unknown"
end

{decoders}
local function aux_checksum(tvb, first, count)
  local sum = 0
  for i = first, first + count - 1 do sum = sum + tvb(i, 1):uint() end
  return (256 - sum % 256) % 256
end

local f = aux.fields
f.preamble = ProtoField.uint8("celestron_aux.preamble", "Preamble", base.HEX)
f.length = ProtoField.uint8("celestron_aux.length", "Length", base.DEC)
f.src = ProtoField.uint8("celestron_aux.src", "Source", base.HEX)
f.src_name = ProtoField.string("celestron_aux.src_name", "Source name")
f.dst = ProtoField.uint8("celestron_aux.dst", "Destination", base.HEX)
f.dst_name = ProtoField.string("celestron_aux.dst_name", "Destination name")
f.cmd = ProtoField.uint8("celestron_aux.cmd", "Command", base.HEX)
f.cmd_name = ProtoField.string("celestron_aux.cmd_name", "Command name")
f.data = ProtoField.bytes("celestron_aux.data", "Data")
f.value = ProtoField.string("celestron_aux.value", "Value")
f.unexpected = ProtoField.bool("celestron_aux.unexpected_format", "Unexpected data format")
f.checksum = ProtoField.uint8("celestron_aux.checksum", "Checksum", base.HEX)
f.checksum_ok = ProtoField.bool("celestron_aux.checksum_ok", "Checksum valid")
f.ss_length = ProtoField.uint32("celestron_aux.starsense.length", "StarSense payload length", base.DEC)
f.ss_stars = ProtoField.uint32("celestron_aux.starsense.stars", "Stars", base.DEC)
f.ss_data = ProtoField.bytes("celestron_aux.starsense.data", "StarSense frame (escaped)")
f.mark = ProtoField.string("celestron_aux.mark", "Mark")

local bad_checksum = ProtoExpert.new("celestron_aux.bad_checksum", "Bad checksum",
  expert.group.CHECKSUM, expert.severity.ERROR)
local malformed = ProtoExpert.new("celestron_aux.malformed", "Malformed frame",
  expert.group.MALFORMED, expert.severity.ERROR)
local unexpected_format = ProtoExpert.new("celestron_aux.unexpected_format",
  "Data does not fit the command's known format", expert.group.MALFORMED, expert.severity.WARN)
aux.experts = {{ bad_checksum, malformed, unexpected_format }}

function aux.init()
  calibration.last = nil
  calibration.by_frame = {{}}
end

-- Each dissect_* gets one whole frame and returns its Info column text.
-- The decoded value of a payload, as AUXBUS Scanner shows it: (text, unexpected).
local function decode_value(payload, device, command, is_reply, context)
  if #payload == 0 then return "", false end
  local decoder = (DECODERS[device] and DECODERS[device][command]) or ANY_DECODERS[command]
  if decoder == nil then return "", false end
  local ok, result = pcall(decoder, payload, is_reply, context)
  if ok then return result, false end
  return "", true
end

local function dissect_aux(tvb, tree, context)
  local length = tvb(1, 1):uint()
  local src, dst, cmd = tvb(2, 1):uint(), tvb(3, 1):uint(), tvb(4, 1):uint()
  local device = subject_device(src, dst)
  local name = command_name(device, cmd)
  tree:append_text(string.format(", %s -> %s, %s", device_name(src), device_name(dst), name))
  tree:add(f.preamble, tvb(0, 1))
  tree:add(f.length, tvb(1, 1))
  tree:add(f.src, tvb(2, 1)):append_text(" (" .. device_name(src) .. ")")
  tree:add(f.src_name, tvb(2, 1), device_name(src))
  tree:add(f.dst, tvb(3, 1)):append_text(" (" .. device_name(dst) .. ")")
  tree:add(f.dst_name, tvb(3, 1), device_name(dst))
  tree:add(f.cmd, tvb(4, 1)):append_text(" (" .. name .. ")")
  tree:add(f.cmd_name, tvb(4, 1), name)
  local data_length = length - 3
  local payload = data_length > 0 and tvb(5, data_length):raw() or ""
  local value, unexpected = decode_value(payload, device, cmd, src == device, context)
  if data_length > 0 then
    tree:add(f.data, tvb(5, data_length))
    if value ~= "" then tree:add(f.value, tvb(5, data_length), value) end
    if unexpected then
      tree:add(f.unexpected, tvb(5, data_length), true):set_generated()
      tree:add_proto_expert_info(unexpected_format)
    end
  end
  local position = 5 + data_length
  local expected = aux_checksum(tvb, 1, length + 1)
  local item = tree:add(f.checksum, tvb(position, 1))
  local ok = tvb(position, 1):uint() == expected
  tree:add(f.checksum_ok, tvb(position, 1), ok):set_generated()
  if not ok then
    item:add_proto_expert_info(bad_checksum, string.format("Expected 0x%02x", expected))
  end
  -- As AUXBUS Scanner's own log: the decoded value, else the data in hex.
  local shown = value
  if shown == "" and #payload > 0 then
    shown = hex(payload) .. (unexpected and " (unexpected format)" or "")
  end
  if value ~= "" then tree:append_text(", " .. value) end
  return string.format("%s -> %s  %s (0x%02x)%s%s", device_name(src), device_name(dst),
    name, cmd, shown ~= "" and (" " .. shown) or "", ok and "" or "  [bad checksum]")
end

-- Reads one logical byte of a StarSense frame at index, undoing 3B 02 02 escapes.
-- Returns the byte and the next index, or nil when the buffer ends first.
local function starsense_byte(tvb, index)
  if index >= tvb:len() then return nil end
  local byte = tvb(index, 1):uint()
  if byte ~= 0x3b then return byte, index + 1 end
  if index + 2 >= tvb:len() then return nil end
  if tvb(index + 1, 1):uint() == 2 and tvb(index + 2, 1):uint() == 2 then return byte, index + 3 end
  return byte, index + 1
end

-- The checksum a whole frame at offset should end with: the two's complement of
-- the sum of its bytes after the preamble (unescaped, for a StarSense frame).
local function expected_checksum(tvb, offset, size)
  if tvb(offset, 1):uint() == 0x3b then return aux_checksum(tvb, offset + 1, size - 2) end
  local sum, index, byte = 0, offset + 1, 0
  while index < offset + size - 1 do
    byte, index = starsense_byte(tvb, index)
    if byte == nil then break end
    sum = sum + byte
  end
  return (256 - sum % 256) % 256
end

local function checksum_ok(tvb, offset, size)
  return tvb(offset + size - 1, 1):uint() == expected_checksum(tvb, offset, size)
end

local function dissect_starsense(tvb, tree)
  local size = tvb:len()
  tree:add(f.preamble, tvb(0, 1))
  local value, index = 0, 1
  for _ = 1, 4 do
    local byte
    byte, index = starsense_byte(tvb, index)
    value = value * 256 + byte
  end
  local stars = math.floor(value / 8)
  tree:append_text(string.format(", StarSense star list, %d stars", stars))
  tree:add(f.ss_length, tvb(1, index - 1), value)
  tree:add(f.ss_stars, tvb(1, index - 1), stars):set_generated()
  if size - 1 > index then tree:add(f.ss_data, tvb(index, size - 1 - index)) end
  local expected = expected_checksum(tvb, 0, size)
  local ok = tvb(size - 1, 1):uint() == expected
  local item = tree:add(f.checksum, tvb(size - 1, 1))
  tree:add(f.checksum_ok, tvb(size - 1, 1), ok):set_generated()
  if not ok then
    item:add_proto_expert_info(bad_checksum, string.format("Expected 0x%02x", expected))
  end
  return string.format("StarSense star list, %d stars%s", stars, ok and "" or "  [bad checksum]")
end

-- Size of the frame starting at offset: a number of bytes, nil when more bytes
-- are needed, or 0 when no frame can start there (as AUXBUS Scanner's own parser).
local function frame_length(tvb, offset)
  local available = tvb:len() - offset
  local first = tvb(offset, 1):uint()
  if first == 0x3b then
    if available < 2 then return nil end
    local length = tvb(offset + 1, 1):uint()
    if length < 3 then return 0 end
    if available < length + 3 then return nil end
    return length + 3
  end
  if first == 0x3c then
    local value, index, byte = 0, offset + 1, 0
    for _ = 1, 4 do
      byte, index = starsense_byte(tvb, index)
      if byte == nil then return nil end
      value = value * 256 + byte
    end
    if value > 0xffff then return 0 end
    for _ = 1, value do
      byte, index = starsense_byte(tvb, index)
      if byte == nil then return nil end
    end
    if index >= tvb:len() then return nil end
    return index + 1 - offset
  end
  return 0
end

local function is_preamble(byte)
  return byte == 0x3b or byte == 0x3c
end

-- Dissects every frame in tvb. Over TCP a frame can span segments: then it asks
-- Wireshark for more data (can_desegment), as the bus is a plain byte stream.
local function dissect_frames(tvb, pinfo, root, can_desegment)
  local offset, infos = 0, {{}}
  while offset < tvb:len() do
    local size = frame_length(tvb, offset)
    if size == nil and can_desegment then
      pinfo.desegment_offset = offset
      pinfo.desegment_len = DESEGMENT_ONE_MORE_SEGMENT
      break
    end
    if size == nil then
      local tree = root:add(aux, tvb(offset))
      tree:add_proto_expert_info(malformed, "Frame cut short")
      infos[#infos + 1] = "Frame cut short"
      offset = tvb:len()
    elseif size == 0 then
      local stop = offset + 1
      while stop < tvb:len() and not is_preamble(tvb(stop, 1):uint()) do stop = stop + 1 end
      local tree = root:add(aux, tvb(offset, stop - offset))
      tree:append_text(", bytes outside any frame")
      tree:add_proto_expert_info(malformed, "Bytes outside any frame")
      infos[#infos + 1] = "Bytes outside any frame"
      offset = stop
    else
      local frame = tvb(offset, size):tvb()
      local tree = root:add(aux, tvb(offset, size))
      if frame(0, 1):uint() == 0x3b then
        local context = {{ visited = pinfo.visited, key = pinfo.number .. ":" .. offset }}
        infos[#infos + 1] = dissect_aux(frame, tree, context)
      else
        infos[#infos + 1] = dissect_starsense(frame, tree)
      end
      -- After a bad checksum, move on one byte only, as AUXBUS Scanner's own parser
      -- does: the "frame" may be noise whose length byte swallowed real frames,
      -- which are found again by resynchronising.
      if checksum_ok(tvb, offset, size) then offset = offset + size else offset = offset + 1 end
    end
  end
  if #infos > 0 then
    pinfo.cols.protocol = "Celestron AUX"
    pinfo.cols.info = table.concat(infos, " | ")
  end
end

function aux.dissector(tvb, pinfo, root)
  if tvb:len() == 0 then return 0 end
  -- The Protocol and Info columns are set only for complete frames: a TCP segment
  -- holding just the start of a frame stays "TCP" until the frame is reassembled.
  if tvb:len() >= 6 and tvb(0, 6):string() == "#MARK " then
    local text = tvb:len() > 6 and tvb(6):string(ENC_UTF_8) or ""
    root:add(aux, tvb()):add(f.mark, tvb(), text)
    pinfo.cols.protocol = "MARK"
    pinfo.cols.info = "MARK: " .. text
    return tvb:len()
  end
  dissect_frames(tvb, pinfo, root, pinfo.can_desegment > 0)
  return tvb:len()
end

-- pcap files from AUXBUS Scanner's --export-pcap: one frame or mark per record.
local encap = (wtap_encaps and wtap_encaps.USER0) or wtap.USER0
DissectorTable.get("wtap_encap"):add(encap, aux)

-- Live traffic: a SkyPortal or Evolution Wi-Fi module (or AUXBUS Scanner's Wi-Fi
-- dongle emulator) carries the bus as a TCP byte stream, on port {tcp_port} by default.
aux.prefs.tcp_port = Pref.uint("TCP port", {tcp_port}, "TCP port of the Wi-Fi module's AUX stream")
local registered_port = 0
local function register_tcp_port()
  local tcp = DissectorTable.get("tcp.port")
  if registered_port ~= 0 then tcp:remove(registered_port, aux) end
  registered_port = aux.prefs.tcp_port
  if registered_port ~= 0 then tcp:add(registered_port, aux) end
end
function aux.prefs_changed() register_tcp_port() end
register_tcp_port()
"""
