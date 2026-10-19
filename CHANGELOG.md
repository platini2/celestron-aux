# Changelog

## 1.0.2

### Changed

- Right ascension in StarSense plate-solve and calibration values is shown in
  hours, minutes and seconds (e.g. RA 19h50m48.11s), as in CPWI and star
  catalogs, instead of degrees. Dec stays in degrees.
- The mounting flag in StarSense mount info (0x30) is shown as "mount mode N"
  instead of "alt-az" / "equatorial": a CPC on an equatorial wedge sends 1,
  which another source reads as alt-az, so its meaning is not settled.

- StarSense plate-solve and calibration status 4 is shown as "Failed (status
  4)" instead of a position of 0h/0 deg: real failed solves send status 4 with
  zeros.

### Added

- StarSense command 0x45 named `SS2_GET_IMAGE_ROTATION` (AutoAlign V2) and
  `SSAG_GET_IMAGE_ROTATION` (AutoGuider), decoded as an angle, e.g.
  "Rotation 47.86 deg": the camera image's rotation on the sky. Across eight
  real plate solves it turned degree for degree with the 0x4b offset
  (correlation 0.993).
- StarSense AutoAlign V2 command 0x08 `SS2_GET_CALIBRATION_DATA`: the stored
  calibration, read one slot at a time. Requests and replies name the slot
  (offset position RA/Dec, calibration star RA/Dec, image rotation,
  calibration time) and replies show its value in the matching format, e.g.
  "Slot 2: calibration star RA, 19h50m48.11s (297.7005 deg)"; "0 (empty)" when
  nothing is stored. Replies carry no slot number, so the slot is taken from
  the request just before.
- StarSense AutoAlign V2 command 0x4b `SS2_GET_TELESCOPE_POSITION`, decoded
  like a plate solve (status, RA/Dec): the solve shifted by the calibrated
  camera-to-telescope offset, read as where the telescope points. The
  direction of the shift is not confirmed yet.

## 1.0.1

### Changed

- Motor commands 0x02 and 0x17 are named `MTR_GOTO_FAST` and `MTR_GOTO_SLOW`
  (were `MTR_GOTO` and `MTR_GOTO2`).
- Motor command 0x49 is named `MTR_GET_SWITCH_CALIBRATION`; it was listed as a
  second `MTR_SET_SWITCH_CALIBRATION`, which is 0x48.
- Battery command 0x18 is named `BAT_GET_MAX_CURRENT` (was `BAT_GET_CURRENT`).

### Added

- Motor firmware-programming commands 0x81-0x84: `MTR_PROGRAM_ENTER`,
  `MTR_PROGRAM_INIT`, `MTR_PROGRAM_DATA` and `MTR_PROGRAM_END`.
- GPS command 0x55 `GPS_GET_HW_VER`, decoded (0xab Celestron GPS, 0xeb
  non-Celestron GPS).
- Evolution WiFi command 0x10 `WIFI_SET_ENABLE` (on/off).
- StarSense AutoGuider (0xb9) with its commands and decoders: plate solve and
  calibration status with RA/Dec, calibration x/y, guiding on/off, mount info,
  geolocation and time. Its guiding commands to the motors are shown with the
  motor command names. Names and payload layouts come from community reverse
  engineering and were checked against published examples.
- Focuser controls in the Focuser panel: hold-to-move − and + buttons with
  speeds 1-9, a Stop button, and Go to position (checked against the travel
  limits when known). They work only while the panel is switched on. As with
  the mount, a held move stops by itself if the page stops confirming it, and
  the focuser is stopped when the panel is switched off, before listen-only
  mode takes effect, and on disconnect.
- Mount models Origin (0x1d8a) and Advanced GT (0x0682, from the
  INDI celestronaux driver).
- Mount families: a mount whose exact model id is unknown is recognised by the
  first byte of the id and shown as, for example, "CGE Pro family (0x0d12)".
  The families come from community reverse engineering, plus one for each
  known exact model, and the tests check that every exact model agrees with
  its family.
- The mount-info command (0x30) of the StarSense AutoAlign V2 and AutoGuider
  names the mount family of its model byte, e.g. "Model 0x11 (CPC Deluxe
  family)".
- Dew heater controller (0x17) commands, from community reverse engineering, decoded:
  LED brightness (get 0x02, set 0x20), input current limit 0x03, enable
  port 0x14, auto aggressiveness 0x16, manual power 0x17, recalibrate sensor
  0x19 and calibrating status 0x1a; plus 0x21, still unknown.
- StarSense AutoAlign V2 (0xbd, previously named "Starsense Camera V2") uses
  the AutoGuider's command set except its guiding commands (0x2x): plate
  solve, calibrate, 0x07, mount info, location, time and 0x45, named `SS2_*`
  and decoded with the same decoders. Captures from real hardware confirmed
  the location (degrees) and time formats.

### Fixed

- The web interface no longer prints a traceback when a browser goes away
  mid-request (tab closed or reloaded, computer asleep). On Windows this showed
  up as `ConnectionAbortedError: [WinError 10053]`. Real errors are still
  reported.

## 1.0.0

First release of the rewritten AUXBUS scanner, which replaces the original
single-file `celestron.py` (0.9.36). Still in initial development: expect
changes between releases.

### Added

- Package structure with separate layers for the wire protocol, device
  catalog, payload decoding, transports, bus engine and features, and a test
  suite that includes a StarSense frame captured from hardware and end-to-end
  tests against simulated mounts.
- Connections over Wi-Fi (SkyPortal / Evolution), a serial adapter on an AUX
  port, a hand controller, and playback of recorded raw captures.
- Browser interface (`--web`) with every console command as a button and a
  red night-vision theme. It loads nothing from the internet, so it works on a
  mount's own Wi-Fi network.
  - Connect from the page: Wi-Fi, AUX port (with a list of the serial ports
    found) or hand controller. Disconnect and reconnect without restarting;
    the CSV log and raw capture stay open instead of being overwritten.
  - Settings, logging, listen-only mode and GPS emulation can be set before
    connecting and apply from the first packet.
  - Detected devices, a live traffic log (newest on top, following new
    messages only while you are at the top), unknown traffic, and the
    StarSense star field. Optional "Value (hex)" and "Raw bytes" columns.
  - Replay a raw capture chosen in the browser, keeping its original times.
  - Downloads for the CSV log, the raw capture and the unknown-traffic summary.
- Mount position panel: live axis angles and a hold-to-move pad for both axes
  with speeds 1-9, arrow keys and a Stop button. Off by default; off means no
  polling and no moves. A move lasts only while it is held: the page
  re-confirms it several times a second and the server stops the axis on its
  own if confirmations stop. Motors are also stopped on disconnect, on quit,
  when the panel is switched off and before listen-only mode takes effect.
  Console commands `p` (positions) and `x` (stop both motors).
- GPS panel (when a GPS is detected): link status, time valid, latitude,
  longitude, UTC date and time, and satellite counts, polled every 5 seconds
  while switched on.
- Focuser panel (when a focuser is detected): position, moving or stopped,
  and travel limits, polled every second while switched on.
- Mount, GPS and focuser readings are also picked up from the hand
  controller's own traffic, even with polling off or in listen-only mode.
- StarSense panel (when a StarSense camera, 0xb4, is detected) with the
  capture button, and a separate StarSense emulation panel that sends a
  sample star list.
- Listen-only mode (`--listen-only`, console command `l`, a switch in the web
  page): nothing is ever transmitted, not even scans, keep-alive or GPS
  replies.
- Unknown-traffic discovery: packets whose device or command is not in the
  catalog are highlighted and grouped with counts, peers and example payloads
  in each direction. Console command `u`.
- Your own names for devices and commands, from "Name it" on unknown traffic.
  Saved to `~/.celestron-aux/catalog.json` (or `--catalog PATH`) and layered
  over the built-in catalog, so upgrades never overwrite them. A catalog file
  that cannot be read is reported and never overwritten.
- Marks: notes such as "pressed Align on the hand controller", placed in the
  traffic stream from the web page (press M) or console command `m`, and
  recorded in every log. A replay brings them back at their original times.
- Decoders for the battery, charger, mount lights and focuser. These formats
  come from community reverse engineering; payloads of an unexpected length
  are shown as hex.
- Command 0xFE is named GET_VERSION on every device without a more specific
  name.
- Device catalog entry for the StarSense Camera V2 (`0xbd`).
- `payload_hex` column in the CSV log.
- Version and copyright notice in the console, the web page footer and
  `--version`.

### Changed

- StarSense star lists are decoded as little-endian float32 pixel coordinates
  on a 1280 x 960 sensor; images are drawn at that size.
- Tracking-rate commands (`MTR_PTRACK` / `MTR_NTRACK`) are shown in arcsec/s.
- Charger command `0x10` is named `CHG_MODE` and light command `0x10`
  `LIGHT_LEVEL`, since each both reads and sets.
- Raw recording moved from console command `9` to `w`; playback is a
  connection mode instead of command `8`.
- Our own transmitted packets are shown in the traffic for every connection
  type, without relying on the bus echoing them (which jammed the bus on real
  hardware).

### Fixed

- Hand-controller mode called `ser.close` / `ser.open` without parentheses.
- Scanning all addresses crashed on unknown devices.
- Start-up race in the keep-alive thread.
- The send-command menu ran out of letters after 26 commands.
- A corrupted frame no longer swallows the valid frames that follow it.
- Negative angles near zero kept their minus sign.
- Log files are no longer reopened for every packet.

### Removed

- The unused `keyboard` dependency.
