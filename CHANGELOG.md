# Changelog

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
