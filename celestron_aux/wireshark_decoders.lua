-- Payload decoders: a port of celestron_aux/decoding.py, one function per Python
-- decoder and with the same name. Each gets the payload as a Lua string, whether
-- the packet is a reply from the device, and a context for decoders that keep
-- state. It returns the same text as Python, or raises an error where Python's
-- decoder would fail ("unexpected format"). tests/test_wireshark.py runs both on
-- the same payloads and compares them. Lua 5.2 (Wireshark 3.x) and 5.4 (4.x).

local DEG = "\194\176"  -- the degree sign, UTF-8
local RAD_TO_DEG = 180.0 / math.pi  -- as Python's math.degrees
local EPOCH_2000 = 946684800  -- 2000-01-01 00:00:00 UTC in Unix seconds
local MAX_TIMESTAMP = 253402300800  -- year 10000
local MAX_ANGLE = 1e9  -- larger angles are garbage, as in format_dms

local function unexpected() error("unexpected format", 0) end

-- data[i] (0-based), failing past the end like Python's IndexError.
local function at(s, i)
  local value = s:byte(i + 1)
  if value == nil then unexpected() end
  return value
end

local function all_zero(s)
  for i = 1, #s do if s:byte(i) ~= 0 then return false end end
  return true
end

local function need(s, size) if #s < size then unexpected() end end

-- int.from_bytes(data[first:last], "big"): a short slice simply gives fewer bytes.
local function uint(s, first, last)
  first = first or 0
  last = math.min(last or #s, #s)
  local value = 0
  for i = first + 1, last do value = value * 256 + s:byte(i) end
  return value
end

local function hex(s)
  return (s:gsub(".", function(c) return string.format("%02x", c:byte()) end))
end

-- Python's round(): halves go to the even neighbour; NaN and infinity fail.
local function round_even(x)
  if x ~= x or x == math.huge or x == -math.huge then unexpected() end
  local whole = math.floor(x)
  local fraction = x - whole
  if fraction > 0.5 or (fraction == 0.5 and whole % 2 == 1) then whole = whole + 1 end
  return whole
end

-- A big-endian IEEE 754 float32 at byte i, as Python's struct.unpack(">f").
local function f32(s, i)
  need(s, i + 4)
  local b1, b2, b3, b4 = s:byte(i + 1, i + 4)
  local sign = b1 >= 128 and -1.0 or 1.0
  local exponent = (b1 % 128) * 2 + math.floor(b2 / 128)
  local mantissa = ((b2 % 128) * 256 + b3) * 256 + b4
  if exponent == 255 then
    if mantissa == 0 then return sign * math.huge end
    return 0 / 0
  end
  if exponent == 0 then return sign * mantissa * 2.0 ^ -149 end
  return sign * (1 + mantissa / 8388608) * 2.0 ^ (exponent - 127)
end

local function int(value) return string.format("%d", value) end

-- f"{x:+.2f}": Python writes +nan where C writes nan.
local function signed2(x)
  if x ~= x then return "+nan" end
  return string.format("%+.2f", x)
end

-- _labelled: "value - label", or just the value.
local function labelled(value, labels)
  local label = labels[value]
  if label then return string.format("%d - %s", value, label) end
  return int(value)
end

-- labels.get(value, str(value))
local function label_or_number(labels, value)
  return labels[value] or int(value)
end

-- protocol/units.py ------------------------------------------------------------

-- bam_to_degrees(data[:3]): a signed big-endian binary angle of 1-3 bytes.
local function bam_degrees(s)
  local raw = s:sub(1, 3)
  if #raw == 0 then unexpected() end
  local bits = 8 * #raw
  local value = uint(raw)
  if value >= 2 ^ (bits - 1) then value = value - 2 ^ bits end
  return value * 360.0 / 2 ^ bits
end

local function dms(degrees)
  if degrees ~= degrees or math.abs(degrees) > MAX_ANGLE then unexpected() end
  local sign = degrees < 0 and "-" or ""
  local hundredths = round_even(math.abs(degrees) * 360000)
  local whole = math.floor(hundredths / 360000)
  local rest = hundredths - whole * 360000
  local minutes = math.floor(rest / 6000)
  rest = rest - minutes * 6000
  return string.format("%s%d%s%02d'%05.2f\"", sign, whole, DEG, minutes, rest / 100)
end

local function hms(degrees)
  local wrapped = math.fmod(degrees, 360)
  if wrapped < 0 then wrapped = wrapped + 360 end  -- Python's %: the sign of the divisor
  local hundredths = round_even(wrapped / 15 * 360000) % 8640000
  local hours = math.floor(hundredths / 360000)
  local rest = hundredths - hours * 360000
  local minutes = math.floor(rest / 6000)
  rest = rest - minutes * 6000
  return string.format("%dh%02dm%05.2fs", hours, minutes, rest / 100)
end

local function version(s)
  if #s < 4 then
    local parts = {}
    for i = 1, #s do parts[#parts + 1] = int(s:byte(i)) end
    return table.concat(parts, ".")
  end
  return string.format("%d.%d.%d", at(s, 0), at(s, 1), uint(s, 2, 4))
end

local function mount_name(model)
  local name = MOUNT_MODELS[model]
  if name then return name end
  local family = model > 0xff and MOUNT_FAMILIES[math.floor(model / 256)] or nil
  if family then return string.format("%s family (0x%04x)", family, model) end
  return string.format("Unknown (0x%04x)", model)
end

local function ra_dec(s)
  need(s, 8)
  local ra, dec = f32(s, 0) * RAD_TO_DEG, f32(s, 4) * RAD_TO_DEG
  return "RA " .. hms(ra) .. ", Dec " .. dms(dec)
end

local function utc(seconds, pattern)
  local text = os.date("!" .. pattern, seconds)
  if type(text) ~= "string" then unexpected() end
  return text
end

-- decoders --------------------------------------------------------------------

local D = {}
local ACK = {[1] = "ACK"}
local YES_NO = {[0] = "No", [1] = "Yes"}
local ON_OFF = {[0] = "Off", [1] = "On"}
local IN_PROGRESS, SOLVED, FAILED = 0, 1, 4

D._version = function(s) return version(s) end

D._motor_position = function(s) return dms(bam_degrees(s)) end

D._motor_goto2 = function(s, reply)
  if reply then return labelled(at(s, 0), ACK) end
  return dms(bam_degrees(s))
end

D._motor_track_rate = function(s, reply)
  if reply then return labelled(at(s, 0), ACK) end
  return string.format("%.4f arcsec/s", uint(s, 0, 3) / 1024)
end

D._motor_model = function(s)
  local model = uint(s)
  return string.format("0x%04x (%s)", model, mount_name(model))
end

D._motor_switch_over = function(s) return labelled(at(s, 0), {[0] = "False", [1] = "True"}) end
D._motor_goto_over = function(s) return labelled(at(s, 0), {[0] = "Not done", [1] = "Check", [0xff] = "Done"}) end
D._motor_custom_rate_enabled = function(s) return labelled(at(s, 0), {[0] = "Disabled", [1] = "Enabled"}) end
D._motor_single_byte = function(s) return int(at(s, 0)) end
D._motor_cordwrap_on = function(s) return labelled(at(s, 0), {[0] = "Disabled", [0xff] = "Enabled"}) end
D._motor_autoguide_rate = function(s) return string.format("%g%%", 100 * at(s, 0) / 256) end
D._motor_approach_direction = function(s) return labelled(at(s, 0), {[0] = "Positive", [1] = "Negative"}) end

D._gps_coordinate = function(s) return dms(bam_degrees(s)) end

D._gps_date = function(s)
  need(s, 2)
  return string.format("%d/%d", at(s, 0), at(s, 1))
end

D._gps_year = function(s) return int(uint(s)) end

D._gps_satellites = function(s)
  need(s, 2)
  return string.format("%d - %d", at(s, 0), at(s, 1))
end

D._gps_time = function(s)
  need(s, 3)
  return string.format("%02d:%02d:%02d", at(s, 0), at(s, 1), at(s, 2))
end

D._gps_hardware_version = function(s)
  local value = at(s, 0)
  return string.format("0x%02x", value) .. (value == 0xab and " (Celestron GPS)" or "")
end

D._gps_flag = function(s) return labelled(at(s, 0), YES_NO) end

D._starsense_align_center = function(s)
  if #s == 8 then
    local x = ((s:byte(4) * 256 + s:byte(3)) * 256 + s:byte(2)) * 256 + s:byte(1)
    local y = ((s:byte(8) * 256 + s:byte(7)) * 256 + s:byte(6)) * 256 + s:byte(5)
    return string.format("%d - %d", x, y)
  end
  return int(at(s, 0))
end

D._starsense_status = function(s)
  local status = labelled(at(s, 0), {[1] = "Error", [3] = "Ready", [9] = "Finished"})
  if #s > 1 then return status .. " - Stars: " .. int(at(s, 1)) end
  return status
end

D._ssag_plate_status = function(s)
  local status = at(s, 0)
  if status == IN_PROGRESS then return "In progress" end
  if status == FAILED then return string.format("Failed (status %d)", status) end
  if status == SOLVED then return "Solved, " .. ra_dec(s:sub(2)) end
  return string.format("Status %d, ", status) .. ra_dec(s:sub(2))
end

D._ssag_start_calibrate = function(s) return ra_dec(s) end

D._ssag_calibrate_status = function(s)
  local status = at(s, 0)
  if status == IN_PROGRESS then return "In progress" end
  if status == FAILED then return string.format("Failed (status %d)", status) end
  need(s, 5)
  local state = status == 1 and "Solved" or string.format("Status %d", status)
  return string.format("%s, x %d, y %d", state, uint(s, 1, 3), uint(s, 3, 5))
end

D._ssag_set_guiding = function(s)
  local flags = at(s, 0)
  if flags == 0 then return "Disabled" end
  return string.format("Enabled, flags 0x%02x", flags)
end

D._ss2_is_calibrated = function(s, reply)
  if reply then return label_or_number(YES_NO, at(s, 0)) end
  return hex(s)
end

D._ss2_set_aspa = function(s, reply)
  local value = at(s, 0)
  if reply then return value == 0 and "OK" or hex(s) end
  return ({[1] = "Start", [0] = "Stop", [4] = "Adjust"})[value] or hex(s)
end

D._ss2_aspa_pole = function(s)
  local status = at(s, 0)
  if status == SOLVED and #s >= 9 then return "Polar axis " .. ra_dec(s:sub(2)) end
  if status == FAILED then return "No estimate yet" end
  return string.format("Status %d", status)
end

D._ss2_aspa_error = function(s)
  local status = at(s, 0)
  if status ~= SOLVED or #s < 9 then
    if all_zero(s) then return "No measurement" end
    return string.format("Status %d", status)
  end
  local alt, azm = f32(s, 1), f32(s, 5)
  return "Altitude error " .. signed2(alt * 60) .. "', azimuth error " .. signed2(azm * 60) .. "'"
end

D._ss2_aspa_progress = function(s)
  need(s, 6)
  local text = string.format("Completion %d %%, uncertainty %.2f%s", at(s, 1), f32(s, 2), DEG)
  if at(s, 0) == SOLVED then return text end
  return "Waiting (" .. text .. ")"
end

D._ssag_image_rotation = function(s)
  need(s, 4)
  return string.format("Rotation %.2f%s", f32(s, 0), DEG)
end

-- The stored calibration, one slot per request. A reply does not say which slot it
-- holds, so the slot requested last is remembered, per frame once dissected, since
-- Wireshark dissects frames again in any order.
local CALIBRATION_SLOTS = {
  [0] = "offset position RA", [1] = "offset position Dec", [2] = "calibration star RA",
  [3] = "calibration star Dec", [4] = "image rotation", [5] = "calibration time",
}
local calibration = {last = nil, by_frame = {}}

local function slot_name(slot)
  return string.format("Slot %d: %s", slot, CALIBRATION_SLOTS[slot] or "unknown")
end

D._ss2_calibration_data = function(s, reply, context)
  if not reply then
    local slot = at(s, 0)
    if not context.visited then calibration.last = slot end
    return slot_name(slot)
  end
  need(s, 4)
  local value = f32(s, 0)
  local slot
  if context.visited then
    slot = calibration.by_frame[context.key]
  else
    slot = calibration.last
    calibration.by_frame[context.key] = slot
  end
  local text
  if value == 0 then
    text = "0 (empty)"
  elseif slot == 5 or (slot == nil and value > 1e9) then
    if not (value >= 0 and value < MAX_TIMESTAMP) then unexpected() end
    text = utc(math.floor(value), "%Y-%m-%d %H:%M") .. string.format(" UTC (%.0f)", value)
  elseif slot == 0 or slot == 2 then
    text = hms(value) .. string.format(" (%.4f%s)", value, DEG)
  elseif slot == 1 or slot == 3 then
    text = dms(value)
  elseif slot == 4 then
    text = string.format("%.2f%s", value, DEG)
  else
    text = string.format("%.6g", value)
  end
  if slot == nil then return text end
  return slot_name(slot) .. ", " .. text
end

D._ssag_mount_info = function(s)
  need(s, 4)
  local family = MOUNT_FAMILIES[at(s, 0)]
  local model = string.format("Model 0x%02x", at(s, 0)) .. (family and (" (" .. family .. " family)") or "")
  return string.format("%s, mount mode %d, AZM firmware %d.%d", model, at(s, 1), at(s, 2), at(s, 3))
end

D._ssag_geolocation = function(s)
  need(s, 8)
  return string.format("Lat %.4f, Lon %.4f", f32(s, 0), f32(s, 4))
end

D._ssag_time = function(s)
  need(s, 4)
  return utc(EPOCH_2000 + uint(s, 0, 4), "%Y-%m-%d %H:%M:%S UTC")
end

D._dew_power = function(s)
  need(s, 4)
  return string.format("%g V - %g A", uint(s, 0, 2) / 1000, uint(s, 2, 4) / 1000)
end

D._dew_power_limit = function(s)
  need(s, 4)
  return string.format("%g A - %g A", uint(s, 0, 2) / 1000, uint(s, 2, 4) / 1000)
end

D._dew_power_ports = function(s) return int(at(s, 0)) end

D._dew_ports = function(s, reply)
  if reply then return int(at(s, 0)) end
  return labelled(at(s, 0), {[0] = "Get 12V ports", [1] = "Get dew heater ports"})
end

D._dew_status = function(s, reply)
  if not reply then return labelled(at(s, 0), {[0] = "Port 1", [1] = "Port 2"}) end
  need(s, 6)
  local mode = label_or_number({[0] = "Manual", [1] = "Auto"}, at(s, 1))
  local power = at(s, 2)
  local text = string.format("Port %d - %s - Power: %s %d - Aggression %d", at(s, 0), mode,
    power ~= 0 and "On" or "Off", power, at(s, 5))
  if power ~= 0 then
    need(s, 10)
    text = text .. string.format(" - Temp %g%sC", uint(s, 6, 10) / 1000, DEG)
  end
  return text
end

D._dew_led_brightness = function(s) return "Brightness " .. int(at(s, 0)) end

D._dew_set_input_limit = function(s)
  need(s, 2)
  return string.format("%g A", uint(s, 0, 2) / 1000)
end

D._dew_enable_port = function(s)
  need(s, 2)
  return string.format("Port %d %s", at(s, 0), label_or_number(ON_OFF, at(s, 1)):lower())
end

D._dew_set_aggression = function(s)
  need(s, 2)
  return string.format("Heater %d, aggression %d", at(s, 0) + 1, at(s, 1))
end

D._dew_set_manual_power = function(s)
  need(s, 2)
  return string.format("Heater %d, power %d%%", at(s, 0) + 1, round_even(at(s, 1) / 2.55))
end

D._dew_recalibrate = function(s) return at(s, 0) ~= 0 and "Start" or "Stop" end
D._dew_is_calibrating = function(s) return at(s, 0) ~= 0 and "Calibrating" or "Not calibrating" end

D._dew_ambient = function(s)
  need(s, 9)
  return string.format("Temp: %g%sC Dew: %g%sC Hum: %d%%", uint(s, 0, 4) / 1000, DEG,
    uint(s, 4, 8) / 1000, DEG, at(s, 8))
end

D._battery_status = function(s, reply)
  if not reply then return hex(s) end
  need(s, 6)
  local level = ({[0] = "low", [1] = "medium", [2] = "high"})[at(s, 1)]
  local status = string.format("status %d", at(s, 1)) .. (level and (" (" .. level .. ")") or "")
  local charging = at(s, 0) ~= 0 and "charging" or "not charging"
  return string.format("%.3f V, %s, %s", uint(s, 2, 6) / 1000000, status, charging)
end

D._wifi_enable = function(s) return label_or_number(ON_OFF, at(s, 0)) end

D._battery_current = function(s)
  need(s, 2)
  return string.format("%d mA", uint(s, 0, 2))
end

D._charger_mode = function(s, reply)
  local state = label_or_number(ON_OFF, at(s, 0)):lower()
  if reply then return "Charging " .. state end
  return "Set charging " .. state
end

local LIGHTS = {[0] = "tray", [1] = "logo", [2] = "Wi-Fi"}

D._light_level = function(s, reply)
  if reply then return "Level " .. int(at(s, 0)) end
  local light = LIGHTS[at(s, 0)] or ("light " .. int(at(s, 0)))
  if #s >= 2 then return string.format("Set %s light to %d", light, at(s, 1)) end
  return light:sub(1, 1):upper() .. light:sub(2) .. " light"
end

D._focuser_position = function(s)
  need(s, 3)
  return int(uint(s, 0, 3))
end

D._focuser_goto = function(s, reply)
  if reply then return labelled(at(s, 0), ACK) end
  need(s, 3)
  return "To " .. int(uint(s, 0, 3))
end

D._focuser_slew_done = function(s) return labelled(at(s, 0), {[0] = "Moving", [0xff] = "Done"}) end

D._focuser_move_rate = function(s, reply)
  if reply then return labelled(at(s, 0), ACK) end
  return "Rate " .. int(at(s, 0))
end

D._focuser_limits = function(s)
  need(s, 8)
  return string.format("Limits %d to %d", uint(s, 0, 4), uint(s, 4, 8))
end
