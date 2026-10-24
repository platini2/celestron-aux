# Changelog

## 1.1.1

### Fixed

- The emulated StarSense camera raised "math domain error" when the emulated
  polar axis pointed exactly at the celestial pole, which is what a perfect
  polar alignment is, on Windows and macOS (and at some latitudes on Linux):
  rounding left the value an arcsine received a hair beyond 1. The conversion
  from altitude and azimuth to RA/Dec, and the offset of a solve, now clamp it.

## 1.1.0

Everything since 1.0.2: the changes made since 1.0.3 come first in each
section, followed by the 1.0.3 changes.

### Added

- Polar alignment you can correct on the emulated wedge or equatorial mount:
  its polar axis is placed with an error (by default +0.5 degrees in altitude
  and -1.0 in azimuth, editable), which CPWI's ASPA measures. Inclination and
  rotation act as the mount's adjustment knobs: they can be changed while the
  telescope runs, the mount tilts and the telescope with it, and the polar
  axis estimate (0x15) follows, so the error can be reduced to zero as CPWI
  guides.
- The emulated StarSense camera fails to solve (status 4) when the telescope
  points lower than 5 degrees above the horizon, as a real one would.
- All-Star Polar Alignment (ASPA) with the emulated StarSense AutoAlign V2,
  using the commands CPWI uses, now named and decoded: 0x13 SS2_SET_ASPA
  starts (01) or stops (00) it, and 04 goes on to the adjustment step,
  keeping the finished measurement; 0x15 SS2_GET_ASPA_POLE answers the estimated
  polar axis, a status and RA/Dec (here, where the emulated wedge's or
  equatorial mount's polar axis really points, in J2000; "no estimate" without
  one); 0x17 SS2_GET_ASPA_PROGRESS answers a status, the completion (one byte,
  0-100), the uncertainty in degrees (float32) and three bytes not yet
  understood. The completion grows as CPWI turns the RA axis between solves
  (about 5 degrees each), reaching 100 % at the 5th solve, and the uncertainty
  shrinks with it. In the adjustment step CPWI asks 0x16 SS2_GET_ASPA_ERROR:
  status 01, then the altitude error and the azimuth error of the polar axis
  in degrees (float32), worked out with CPWI's adjustment screen; 00 and zeros
  outside a measurement, as a real camera. The emulator answers with how far
  its polar axis is from the celestial pole, so turning the inclination and
  rotation knobs as CPWI guides brings both errors to zero.
- Telescope emulator, in the Emulators panel: the two motor controllers (AZM/RA
  0x10 and ALT/Dec 0x11) of a NexStar SLT, 4/5SE, 6/8SE, CPC Deluxe, GT Series,
  NexStar GPS, Evolution, Origin, Advanced GT, AVX, CGX or CGX-L. They answer
  go-to (fast, then a slow approach), the direction buttons at rates 1-9,
  tracking rates, sync and the settings CPWI reads when it connects, and move
  over time; replies match a real SLT's byte for byte. Each model reports its
  own mount model and latest known motor firmware (editable). Alt-az mounts
  that can take a wedge offer one (greyed for the SLT and GT); equatorial
  mounts always turn around a polar axis. The initial altitude and azimuth,
  and the wedge's inclination and rotation, set where it points. A small sky
  view shows where the emulated telescope points, its trail and its go-to
  target. It runs on the local bus only: starting it while a telescope is
  connected asks first, then disconnects it, and no telescope can be
  connected while it runs.
- Dew heater controller emulator controls: sliders for the air temperature,
  humidity, input voltage (11-13 V) and both heaters' temperatures, which the
  controller reports, with the resulting dew point; an LED that glows at the
  brightness the controller was given; and the state of each output.
- With both the telescope emulator and the StarSense AutoAlign V2 emulator
  running, a plate solve reports where the emulated telescope points when the
  solve starts: converted to J2000 (precession included) with the location and
  time CPWI sent the camera, with a solve error of about 12 arcsec per axis
  and the image rotation following the field on an alt-az mount. CPWI's
  StarSense alignment then agrees with the mount, with a small, non-zero
  error. Checked against astropy to within half an arcminute.
- Dew heater box in Device control, when a dew heater controller (0x17) is
  detected, real or emulated: input voltage and current, air temperature,
  humidity and dew point, and each heater's temperature, power and current,
  read every 2 seconds while switched on. Each heater and the 12 V output can
  be switched on and off, each heater set to automatic (aggressiveness 1-10)
  or manual (power %), and the LED brightness set. The controller does not
  report whether a heater is on, so the switches show the last command seen
  on the bus.
- StarSense AutoAlign V2 box in Device control (also for the StarSense
  AutoGuider): "Take a plate solve" starts a solve, follows it to the end and
  shows the RA/Dec the camera sees, the telescope position, the image
  rotation and whether the camera is calibrated.
- StarSense AutoAlign (V1) emulator: the former "StarSense star list" card is
  now an emulator with an on/off switch and a firmware version field (default
  1.2.13343, as a real camera reported). While on, it answers discovery at
  0xb4, so scans and the hand controller find the camera; "Send sample star
  list" sends the recorded star list from it, and is available only while it
  is on.
- Focuser emulator (0x12, console command `ef`): moves over time with go-to and
  rates 1-9, stops at its travel limits (1000-60000), reports slew done and
  can be calibrated, following how the INDI Celestron focuser driver talks to
  the real focus motor. Its speeds are invented. The Focuser panel works with
  it.
- Dew heater controller emulator (0x17 and its detection address 0xbb,
  console command `ed`): two heaters, a 12 V port, input power and limits,
  LED brightness, temperature, humidity and dew point, with the replies CPWI
  expects from the real controller.
- All emulators (GPS, StarSense AutoAlign V2, focuser, dew heater controller
  and the StarSense sample star list) are in one "Emulators" panel at the
  bottom of the controls. Each device emulator refuses to start if a real
  device answered at one of its addresses, and emulated devices, the GPS
  included, are always listed as "(emulated)", even after a scan, and leave
  the device list when switched off.
- Local bus: switching on the Wi-Fi dongle or any emulator with no telescope
  connected opens a local bus ("Local bus (no telescope)"), where the
  emulated devices answer the scanner and Wi-Fi apps. Traffic is decoded and
  logged as usual and the scan buttons work, so the emulators and Wi-Fi apps
  can be tried without a telescope. Connecting to a telescope replaces the
  local bus; the emulators and the Wi-Fi dongle carry on there.
- Wi-Fi dongle emulator, in the Emulators panel: apps such as CPWI or
  SkySafari connect to this computer as to a SkyPortal Wi-Fi module, on the
  loopback address and on the computer's network address, on port 2000 like
  a real module. With pass-through on, the apps reach the
  telescope's bus through the scanner and receive all its traffic, so the
  scanner logs and decodes everything between them. With pass-through off,
  only the emulated devices answer and nothing the apps send reaches the
  telescope. Like a real SkyPortal module it announces itself so apps can
  find it without typing its address: once a second it broadcasts
  {"AMW007"} to UDP port 55555 (a checkbox turns this off). The dongle also
  answers as a SkyPortal Wi-Fi module (0xb3,
  version 3.1.9120 as reported by a real one) unless a real one is on the bus. Packets from apps
  are marked "Wi-Fi" in the traffic log, and replies that went only to the
  apps "To Wi-Fi". Listen-only mode keeps app commands off the telescope.
- GPS emulation has a box like the other emulators: a checkbox switches it on
  and off (instead of the Start button), and changing the latitude or
  longitude while it is on applies them at once.
- Each emulator has its own box with a firmware version field, filled in with
  the version of a real device (GPS 11.1, StarSense AutoAlign V2 35.12.5,
  focuser 7.16.9300, dew heater controller 1.0 and 1.1.1270 at 0xbb, Wi-Fi
  dongle 3.1.9120). A new
  version (major.minor or major.minor.build) takes effect at once, even while
  emulating, shows in the device list and is kept across connections.
- StarSense AutoAlign V2 emulator: a switch in the Emulators panel
  (console command `sv`) makes the scanner answer CPWI as a camera at 0xbd.
  Plate solves return four alignment points in turn, kept at the hour angles
  and Decs of a real alignment so they sit in the same place relative to the
  meridian whatever the time; telescope position, image rotation and
  calibration behave like the real camera. Every reply has the same length as
  a real camera's, checked by replaying CPWI's requests from a real session.
  It refuses to start when a real StarSense AutoAlign V2 has answered, and it
  is for testing: the positions are not the real sky.

### Changed

- On the telescope emulator card, the initial altitude and azimuth come before
  the wedge setting.
- The Star field box at the bottom of the page only appears when a StarSense
  AutoAlign (V1) camera is detected, real or emulated.
- The emulated telescope slews twice as fast: go-to, the slow final approach
  and rate 9.
- The emulated motors follow the slew limits the app sets: when CPWI sets a
  user limit minimum and maximum (0x1a, 0x1b) and enables them (0x1f), that
  motor stops at the limit, as a real one does, and reading them back (0x1c,
  0x1d, 0x1e) returns what was set. Limits are off until then. The two-byte
  limit is taken to be a 16-bit position, like a short go-to.
- The emulated mount answers the custom rate 9 (0x20-0x23) and autoguide rate
  (0x47, 50 %) the way a real CPC does: CPWI asks these of every mount except
  the SLT, and without them it did not recognise the emulated CPC Deluxe.
- New page layout. A top row holds Connection, Bus, Recording and Detected
  devices; then Traffic and Unknown traffic at full width; then a full-width
  Device control box with Send a command, Mount position, Focuser, GPS, Dew
  heater and the StarSense boxes side by side; then a full-width Emulators
  box with every emulator as a card, which can be folded away and shows how
  many emulators are running; and the StarSense star field at the bottom. On
  a phone it is one column in the same order.
- The dew heater controller emulator starts at 5 °C, 75 % humidity and
  12.7 V, with both heaters on and automatic (aggressiveness 5) and the 12 V
  output on. It remembers outputs being switched on and off (0x14): ports 0
  and 1 are taken to be the heaters and port 2 the 12 V output. A heater that
  is off reports no power, and the 12 V output reports the input voltage when
  on and 0 V when off. The conditions set on the page are kept across
  connections.
- The focuser emulator acknowledges go-to and moves with 01, as real motor
  controllers do.
- StarSense AutoAlign V2 command 0x07 is named `SS2_IS_CALIBRATED` and decoded
  as Yes/No: a real camera answered 00 before any calibration and 01 after.

### Fixed

- A sync (MTR_SET_POSITION) moved the emulated telescope: it should only
  change what the motor reports, as on a real mount. CPWI starts ASPA by
  setting both motors to 90 degrees, which swung the emulated telescope to a
  new direction, for example below the horizon.
- The StarSense box asked the camera its own follow-up questions (telescope
  position, image rotation) after every plate solve on the bus, including
  CPWI's during an ASPA, adding traffic to CPWI's alignment. It now only
  follows up its own solves; CPWI's results are still shown.
- The web page fell behind under heavy traffic (an app such as CPWI on the
  bus sends hundreds of packets a second): it measured the page layout for
  every traffic row, so its backlog of unprocessed messages kept growing and
  the browser tab used a lot of CPU and hundreds of MB of memory. Rows are
  now added in batches, at most once per screen refresh, with one layout
  measurement per batch, and waiting rows are capped like the table. Under
  CPWI-like load the page's memory now stays flat (about 2 MB of script
  memory and 2000 table rows).
- The "newer messages" button no longer makes the traffic toolbar wrap when
  it appears.
- The emulated GPS did not answer GPS_GET_SAT_INFO (0x07), which a real GPS
  answers with the satellites in view and used: it now reports 12 in view, 9
  used. It also answers the hardware version (0x55) with 0xab, like Celestron
  GPS units.
- The emulated telescope ignored go-tos when CPWI followed them with "move at
  rate 0" (stop the direction buttons), which it does in its normal traffic:
  a rate-0 move cancelled the go-to. StarSense Auto then solved the same spot
  again and again and reported errors of many degrees. Rate 0 now stops only
  button motion, as on a real motor.
- CPWI lost the mount and focuser when connected through the Wi-Fi dongle
  with pass-through. Its packets were sent with the scanner's own 150 ms
  pacing, so they could not go faster than about 6.7 a second; CPWI sends
  from several tasks at once and gives up on a reply after about 220 ms, so
  its requests queued up, timed out ("No response received") and CPWI
  declared the connection lost. Packets relayed for Wi-Fi apps and emulator
  replies are now sent at once, as by a real module, and the pacing of the
  scanner's own requests no longer holds up anyone else's traffic.
- The Wi-Fi dongle frees its port as soon as it stops, so it can start again
  straight away (for example when a telescope replaces the local bus).

## 1.0.3

### Added

- Telescope emulator, in the Emulators panel: the two motor controllers (AZM/RA
  0x10 and ALT/Dec 0x11) of a NexStar SLT, 4/5SE, 6/8SE, CPC Deluxe, GT Series,
  NexStar GPS, Evolution, Origin, Advanced GT, AVX, CGX or CGX-L. They answer
  go-to (fast, then a slow approach), the direction buttons at rates 1-9,
  tracking rates, sync and the settings CPWI reads when it connects, and move
  over time; replies match a real SLT's byte for byte. Each model reports its
  own mount model and latest known motor firmware (editable). Alt-az mounts
  that can take a wedge offer one (greyed for the SLT and GT); equatorial
  mounts always turn around a polar axis. The initial altitude and azimuth,
  and the wedge's inclination and rotation, set where it points. A small sky
  view shows where the emulated telescope points, its trail and its go-to
  target. It runs on the local bus only: starting it while a telescope is
  connected asks first, then disconnects it, and no telescope can be
  connected while it runs.

- Dew heater controller emulator controls: sliders for the air temperature,
  humidity, input voltage (11-13 V) and both heaters' temperatures, which the
  controller reports, with the resulting dew point; an LED that glows at the
  brightness the controller was given; and the state of each output.

- With both the telescope emulator and the StarSense AutoAlign V2 emulator
  running, a plate solve reports where the emulated telescope points when the
  solve starts: converted to J2000 (precession included) with the location and
  time CPWI sent the camera, with a solve error of about 12 arcsec per axis
  and the image rotation following the field on an alt-az mount. CPWI's
  StarSense alignment then agrees with the mount, with a small, non-zero
  error. Checked against astropy to within half an arcminute.

- Dew heater box in Device control, when a dew heater controller (0x17) is
  detected, real or emulated: input voltage and current, air temperature,
  humidity and dew point, and each heater's temperature, power and current,
  read every 2 seconds while switched on. Each heater and the 12 V output can
  be switched on and off, each heater set to automatic (aggressiveness 1-10)
  or manual (power %), and the LED brightness set. The controller does not
  report whether a heater is on, so the switches show the last command seen
  on the bus.
- StarSense AutoAlign V2 box in Device control (also for the StarSense
  AutoGuider): "Take a plate solve" starts a solve, follows it to the end and
  shows the RA/Dec the camera sees, the telescope position, the image
  rotation and whether the camera is calibrated.

- StarSense AutoAlign (V1) emulator: the former "StarSense star list" card is
  now an emulator with an on/off switch and a firmware version field (default
  1.2.13343, as a real camera reported). While on, it answers discovery at
  0xb4, so scans and the hand controller find the camera; "Send sample star
  list" sends the recorded star list from it, and is available only while it
  is on.
- Focuser emulator (0x12, console command `ef`): moves over time with go-to and
  rates 1-9, stops at its travel limits (1000-60000), reports slew done and
  can be calibrated, following how the INDI Celestron focuser driver talks to
  the real focus motor. Its speeds are invented. The Focuser panel works with
  it.
- Dew heater controller emulator (0x17 and its detection address 0xbb,
  console command `ed`): two heaters, a 12 V port, input power and limits,
  LED brightness, temperature, humidity and dew point, with the replies CPWI
  expects from the real controller.
- All emulators (GPS, StarSense AutoAlign V2, focuser, dew heater controller
  and the StarSense sample star list) are in one "Emulators" panel at the
  bottom of the controls. Each device emulator refuses to start if a real
  device answered at one of its addresses, and emulated devices, the GPS
  included, are always listed as "(emulated)", even after a scan, and leave
  the device list when switched off.
- Local bus: switching on the Wi-Fi dongle or any emulator with no telescope
  connected opens a local bus ("Local bus (no telescope)"), where the
  emulated devices answer the scanner and Wi-Fi apps. Traffic is decoded and
  logged as usual and the scan buttons work, so the emulators and Wi-Fi apps
  can be tried without a telescope. Connecting to a telescope replaces the
  local bus; the emulators and the Wi-Fi dongle carry on there.
- Wi-Fi dongle emulator, in the Emulators panel: apps such as CPWI or
  SkySafari connect to this computer as to a SkyPortal Wi-Fi module, on the
  loopback address and on the computer's network address, on port 2000 like
  a real module. With pass-through on, the apps reach the
  telescope's bus through the scanner and receive all its traffic, so the
  scanner logs and decodes everything between them. With pass-through off,
  only the emulated devices answer and nothing the apps send reaches the
  telescope. Like a real SkyPortal module it announces itself so apps can
  find it without typing its address: once a second it broadcasts
  {"AMW007"} to UDP port 55555 (a checkbox turns this off). The dongle also
  answers as a SkyPortal Wi-Fi module (0xb3,
  version 3.1.9120 as reported by a real one) unless a real one is on the bus. Packets from apps
  are marked "Wi-Fi" in the traffic log, and replies that went only to the
  apps "To Wi-Fi". Listen-only mode keeps app commands off the telescope.
- GPS emulation has a box like the other emulators: a checkbox switches it on
  and off (instead of the Start button), and changing the latitude or
  longitude while it is on applies them at once.
- Each emulator has its own box with a firmware version field, filled in with
  the version of a real device (GPS 11.1, StarSense AutoAlign V2 35.12.5,
  focuser 7.16.9300, dew heater controller 1.0 and 1.1.1270 at 0xbb, Wi-Fi
  dongle 3.1.9120). A new
  version (major.minor or major.minor.build) takes effect at once, even while
  emulating, shows in the device list and is kept across connections.
- StarSense AutoAlign V2 emulator: a switch in the Emulators panel
  (console command `sv`) makes the scanner answer CPWI as a camera at 0xbd.
  Plate solves return four alignment points in turn, kept at the hour angles
  and Decs of a real alignment so they sit in the same place relative to the
  meridian whatever the time; telescope position, image rotation and
  calibration behave like the real camera. Every reply has the same length as
  a real camera's, checked by replaying CPWI's requests from a real session.
  It refuses to start when a real StarSense AutoAlign V2 has answered, and it
  is for testing: the positions are not the real sky.

### Changed

- The Star field box at the bottom of the page only appears when a StarSense
  AutoAlign (V1) camera is detected, real or emulated.
- The emulated telescope slews twice as fast: go-to, the slow final approach
  and rate 9.
- The emulated motors follow the slew limits the app sets: when CPWI sets a
  user limit minimum and maximum (0x1a, 0x1b) and enables them (0x1f), that
  motor stops at the limit, as a real one does, and reading them back (0x1c,
  0x1d, 0x1e) returns what was set. Limits are off until then. The two-byte
  limit is taken to be a 16-bit position, like a short go-to.
- The emulated mount answers the custom rate 9 (0x20-0x23) and autoguide rate
  (0x47, 50 %) the way a real CPC does: CPWI asks these of every mount except
  the SLT, and without them it did not recognise the emulated CPC Deluxe.
- New page layout. A top row holds Connection, Bus, Recording and Detected
  devices; then Traffic and Unknown traffic at full width; then a full-width
  Device control box with Send a command, Mount position, Focuser, GPS, Dew
  heater and the StarSense boxes side by side; then a full-width Emulators
  box with every emulator as a card, which can be folded away and shows how
  many emulators are running; and the StarSense star field at the bottom. On
  a phone it is one column in the same order.
- The dew heater controller emulator starts at 5 °C, 75 % humidity and
  12.7 V, with both heaters on and automatic (aggressiveness 5) and the 12 V
  output on. It remembers outputs being switched on and off (0x14): ports 0
  and 1 are taken to be the heaters and port 2 the 12 V output. A heater that
  is off reports no power, and the 12 V output reports the input voltage when
  on and 0 V when off. The conditions set on the page are kept across
  connections.
- The focuser emulator acknowledges go-to and moves with 01, as real motor
  controllers do.
- StarSense AutoAlign V2 command 0x07 is named `SS2_IS_CALIBRATED` and decoded
  as Yes/No: a real camera answered 00 before any calibration and 01 after.

### Fixed

- The emulated GPS did not answer GPS_GET_SAT_INFO (0x07), which a real GPS
  answers with the satellites in view and used: it now reports 12 in view, 9
  used. It also answers the hardware version (0x55) with 0xab, like Celestron
  GPS units.
- The emulated telescope ignored go-tos when CPWI followed them with "move at
  rate 0" (stop the direction buttons), which it does in its normal traffic:
  a rate-0 move cancelled the go-to. StarSense Auto then solved the same spot
  again and again and reported errors of many degrees. Rate 0 now stops only
  button motion, as on a real motor.
- CPWI lost the mount and focuser when connected through the Wi-Fi dongle
  with pass-through. Its packets were sent with the scanner's own 150 ms
  pacing, so they could not go faster than about 6.7 a second; CPWI sends
  from several tasks at once and gives up on a reply after about 220 ms, so
  its requests queued up, timed out ("No response received") and CPWI
  declared the connection lost. Packets relayed for Wi-Fi apps and emulator
  replies are now sent at once, as by a real module, and the pacing of the
  scanner's own requests no longer holds up anyone else's traffic.
- The Wi-Fi dongle frees its port as soon as it stops, so it can start again
  straight away (for example when a telescope replaces the local bus).

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
