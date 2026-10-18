# Changelog

## Unreleased

### Changed

- Web interface layout: detected devices at the top, then the traffic log, then
  the star field.
- The traffic log shows the newest message on top and follows new messages
  while you are at the top. Scrolled down, the messages you are reading stay
  in place and a button counts the newer ones; scrolling back to the top (or
  pressing the button) resumes following.
- Each traffic message stays on one line.

## 1.0.0

First release of the rewritten AUXBUS scanner, which replaces the original
single-file `celestron.py` (0.9.36). Still in initial development: expect
changes between releases.

### Added

- Package structure with separate layers for the wire protocol, device
  catalog, payload decoding, transports, bus engine and features.
- Connections over Wi-Fi (SkyPortal / Evolution), a serial adapter on an AUX
  port, a hand controller, and playback of recorded raw captures.
- Browser interface (`--web`): every console command as a button, live traffic
  log, StarSense star field, device list and a red night-vision theme. Started
  without a mode and target, it lets you choose the connection in the page,
  lists serial ports, and can disconnect and reconnect without restarting.
- Settings, CSV log, raw capture and GPS emulation can be set up before
  connecting and apply from the first packet.
- Device catalog entry for the StarSense Camera V2 (`0xbd`).
- Test suite, including a StarSense frame captured from hardware and
  end-to-end tests against simulated mounts.

### Changed

- StarSense star lists are decoded as little-endian float32 pixel coordinates
  on a 1280 x 960 sensor; images are drawn at that size.
- Tracking-rate commands (`MTR_PTRACK` / `MTR_NTRACK`) are shown in arcsec/s.
- Raw recording moved from console command `9` to `w`; playback is now a
  connection mode instead of command `8`.
- Serial mode shows our own transmitted packets, taken from the bus echo.

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
