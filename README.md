# celestron-aux

Scanner, monitor and protocol toolkit for the Celestron AUX bus: discover the
devices on a mount, watch and decode their traffic, send commands and capture
StarSense star lists. It can also emulate a whole Celestron system (a mount with
its two motors, StarSense cameras, GPS, focuser, dew heater and a Wi-Fi dongle),
so apps such as CPWI can be tried, and their traffic studied, without a
telescope. See [Emulators](#emulators).

## Install and run

```bash
pip install .            # or: pip install -e ".[dev]" for development
celestron-aux wifi 1.2.3.4          # SkyPortal / Evolution Wi-Fi
celestron-aux serial COM3           # serial adapter on an AUX port
celestron-aux hc /dev/ttyUSB0       # through a hand controller
celestron-aux playback rawoutput.txt  # replay a recorded session
```

`python -m celestron_aux ...` works too. Run `celestron-aux -h` for every option
(`--csv`, `--raw`, `--no-scan`, `--listen-only`, `--keepalive`, `--image-dir`,
`-v`, ...). Inside the session, type `h` to list the interactive commands.

Use `--listen-only` (or `l` in the session) to watch a hand controller talking
to the mount without ever transmitting anything yourself. Type `u` to list the
traffic the catalog cannot name yet, with example payloads, and `m` to mark
what just happened (for example "pressed Align on the hand controller"): the
mark is saved in every log, next to the traffic it explains.

Names you give to devices and commands are kept in
`~/.celestron-aux/catalog.json`, or in the file you pass with `--catalog`.

### On Windows

Use `py` in place of `python` and `pip`:

```
py -m pip install .
py -m celestron_aux --web
py -m celestron_aux serial COM3
```

After `py -m` the name is written with an underscore, `celestron_aux`. With
the hyphen (`py -m celestron-aux`) Python reports "No module named".

`py -m celestron_aux` always works. The shorter `celestron-aux` command only
works once Python's Scripts folder is on your PATH; until then Windows says
"'celestron-aux' is not recognized". To add it, print the folder:

```
py -c "import sysconfig; print(sysconfig.get_path('scripts'))"
```

Then open the Start menu, search for "Edit environment variables for your
account", select **Path**, choose **Edit**, then **New**, paste the folder and
press **OK**. Open a new Command Prompt for the change to apply.

If `py` itself is not recognized, Python is not installed: get it from
python.org and tick "Add python.exe to PATH" on the first installer screen.

## Web interface

```bash
celestron-aux --web                           # http://localhost:8080, choose the connection in the page
celestron-aux wifi 1.2.3.4 --web              # connect at startup, then open the page
celestron-aux --web 0.0.0.0:8080              # reachable from a phone or tablet on your network
```

### Opening the page in your browser

1. Start the program with `--web`. It prints the address to open:

   ```
   celestron-aux 1.1.0
   Copyright © 2021-2026 Patricio Latini
   Web interface: http://localhost:8080/  (press Ctrl+C to stop)
   Open it and choose how to connect to the mount.
   ```

2. On the same computer, open any browser (Chrome, Edge, Safari or Firefox)
   and go to **http://localhost:8080**. `http://127.0.0.1:8080` works too.
3. Leave the terminal window open while you use the page: closing it, or
   pressing Ctrl+C in it, stops the web interface. Any mount movement is
   stopped at the same time.

If port 8080 is already in use, the program says so and exits. Pick another
port, for example `--web 8090`, and open `http://localhost:8090` instead.

To open the page from a phone or tablet, start with `--web 0.0.0.0:8080` and
browse to `http://<this computer's IP address>:8080` from a device on the same
network. To find the IP address, run `ipconfig` on Windows (look for "IPv4
Address") or `ipconfig getifaddr en0` on a Mac. Your firewall may ask whether
to allow the connection the first time. "Use this device's location" in GPS
emulation only works on the computer itself (`localhost`), because browsers
share the location only with secure or local pages.

With `--web` the console stays silent and every console command is available
as a button in the browser: scans, keep-alive, CSV and raw recording, GPS
emulation, StarSense captures (with the star field drawn live), and sending any
command to any detected device. A red night-vision theme keeps your eyes dark
adapted at the telescope. The page loads nothing from the internet, so it works
on a mount's own Wi-Fi network.

The page is laid out in the order you usually need it, one full-width row at a
time:

1. **Connection, Bus, Recording and Detected devices**, side by side.
2. **Traffic** and **Unknown traffic**: the decoded log, and the table of
   commands the catalog cannot name yet.
3. **Device control**: Send a command, Mount position, Focuser, GPS, Dew
   heater, StarSense AutoAlign V2 (plate solves) and StarSense capture. A box
   only appears when its device has been detected, real or emulated.
4. **Emulators**: one card per emulator. Click the title to fold it away; it
   still says how many emulators are running.
5. **Star field**, last, and only when a StarSense AutoAlign (V1) is detected.

Started without a mode and target, the page offers Wi-Fi, AUX port and hand
controller connections and lists the serial ports it finds. Because the page is
already open when the connection starts, it shows the traffic from the first
packet. Recording settings, listen-only mode and GPS emulation can be set
before connecting, and you can disconnect and reconnect without restarting;
the CSV log and raw capture stay open across connections instead of being
overwritten.

The page can also replay a raw capture you choose in the browser, keeping the
original timings, and it offers the CSV log, the raw capture and a summary of
unknown traffic as downloads. Unknown packets are highlighted in the traffic
log and collected in their own table with example payloads, to help decode new
devices.

The Emulators panel also has a **Wi-Fi dongle**: apps such as CPWI or
SkySafari can connect to this computer (port 2000, as for a real module) as to a
SkyPortal Wi-Fi module. With pass-through on, they control the telescope
through the scanner, which logs and decodes all their traffic; with it off,
they only see the emulated devices. The dongle listens on this computer's
network address, so any device on your network can connect to it; your
firewall may ask whether to allow it.

No telescope is needed to try the emulators: switching one on opens a local
bus where the emulated devices answer, the traffic is decoded and the scan
buttons work. Connecting to a telescope later replaces it.

There is no password: anyone who can open the page can control the mount. The
default address only accepts connections from the same computer; use
`0.0.0.0` only on a network you trust.

## Emulators

The **Emulators** panel of the web interface makes this computer answer on the
bus like Celestron devices do. Everything an emulator sends is decoded and
logged like real traffic, so you can try an app, or work out what it asks for,
without a telescope.

| Emulator | Answers as | Console command |
| --- | --- | --- |
| Telescope | the two motor controllers, AZM/RA (0x10) and ALT/Dec (0x11) of 12 mounts | web only |
| Wi-Fi dongle | a SkyPortal Wi-Fi module (0xb3), for CPWI and SkySafari | web only |
| StarSense AutoAlign V2 | the camera at 0xbd, with plate solves and polar alignment | `sv` |
| StarSense AutoAlign (V1) | the original camera at 0xb4 (discovery and star lists) | `s1` |
| GPS | the GPS module at 0xb0 | `g` |
| Focuser | the focus motor at 0x12 | `ef` |
| Dew heater controller | a Smart DewHeater Controller at 0x17 (and 0xbb) | `ed` |

Every emulator has its own on/off checkbox and an editable firmware version
that starts as the version of a real device. An emulator refuses to start when
a real device of that kind has already answered on the bus, because two devices
on one address would garble it. Emulated devices are labelled "(emulated)" in
the list of detected devices.

### Trying it without a telescope

Switch an emulator on while nothing is connected and the program opens a
**local bus**: a bus with no telescope on it, shown as "Local bus (no
telescope)". The emulated devices answer the scanner and any app that connects
through the Wi-Fi dongle, the traffic is decoded, and **Scan the bus** finds the
emulated devices. Connecting to a telescope later replaces the local bus; the
emulators and the dongle carry on there. **Close local bus** stops them.

### Telescope emulator

Choose the mount, then switch the checkbox on. The motors report that mount's
model and its latest known firmware (editable for each motor), and answer the
commands CPWI and the hand controller send: go-to, the direction buttons,
tracking, sync and the settings read on connecting.

| Mount | Type | Wedge | Latest motor firmware known |
| --- | --- | --- | --- |
| NexStar SLT | alt-az | no | 5.18 |
| NexStar 4/5SE | alt-az | optional | 6.19 |
| NexStar 6/8SE | alt-az | optional | 6.19 |
| CPC Deluxe | alt-az | optional | 6.19 |
| GT Series | alt-az | no | 6.19 |
| NexStar GPS | alt-az | optional | 6.19 |
| NexStar Evolution | alt-az | optional | 7.18.5028 |
| Origin | alt-az | optional | 7.22.4150 |
| Advanced GT | equatorial | always (polar axis) | 6.19 |
| AVX | equatorial | always (polar axis) | 7.15.8270 |
| CGX | equatorial | always (polar axis) | 7.18.2214 |
| CGX-L | equatorial | always (polar axis) | 7.18.5020 |

The versions marked in the page's tooltip as assumed are the newest legacy
version seen on real mounts; edit them to match yours.

**Setting it up**

- **Initial altitude and azimuth** are where the telescope points at power-on,
  when both motors read zero.
- **On a wedge** is offered for mounts that can take one and greyed out for the
  others. For an equatorial mount it is always on: the first axis is the polar
  axis. With a wedge, **Inclination** is the tilt of the polar axis (your
  latitude) and **Rotation** its azimuth: 0 towards north, 180 towards south.
- **Error in altitude and azimuth** is how badly the mount was placed. A real
  mount is never placed perfectly, so the polar axis really points at the
  inclination and rotation plus this error (by default +0.5 and -1.0 degrees).
  See [polar alignment](#polar-alignment-with-cpwi).
- The setup is fixed while the telescope runs, except Inclination and Rotation:
  those are the wedge's adjustment knobs.

**How it moves**

- A go-to runs at twice the mount's rate 9 speed, then a slow final approach.
  The direction buttons use the hand controller rates: 1 to 5 are small
  multiples of the sidereal rate (2, 4, 8, 16 and 32 times), 6 to 8 are 0.5, 1
  and 2 degrees a second, and 9 is the fastest. Tracking rates are accepted.
  A rate 0 move only stops the buttons: a go-to carries on, as on a real motor.
- A **sync** changes what a motor reports and never moves the telescope.
- **Slew limits** are obeyed once the app sets and enables them (CPWI
  does): the motor stops at the limit. There are none until then.
- The small **sky view** under the card shows where the telescope points,
  seen from above: zenith in the centre, horizon at the edge, rings every 30
  degrees of altitude, north up and east to the right. A dot marks the
  telescope (hollow below the horizon), a line its recent path and a ring the
  target of a go-to.

**Connection rules.** The emulated telescope runs on the local bus only.
Starting it while a real telescope is connected asks first, then disconnects
it. While it runs, connecting to a real telescope is refused.

### StarSense AutoAlign V2 emulator

The camera answers discovery, plate solves, calibration, the settings CPWI
sends it (location, time, mount information) and the polar alignment commands.
When the telescope emulator runs too, **a plate solve reports where that
telescope points**: converted to J2000 (precession included) with the location
and time CPWI sent the camera, with a solve error of a few arcseconds, and an
image rotation that follows the field on an alt-az mount. Aim the emulated
telescope somewhere else and the next solve follows it, so CPWI's alignment
agrees with the mount. Below 5 degrees of altitude the camera fails the solve,
as a real one looking at the ground would. Without a telescope emulator it
returns four recorded alignment points in turn.

CPWI sends the camera the location from its own site settings, so give CPWI's
latitude the right sign (negative in the southern hemisphere), or the sky the
camera "sees" will be the wrong one.

The **StarSense AutoAlign V2** box in Device control can also start a plate
solve by itself and shows the result, the telescope position, the image
rotation and whether the camera is calibrated.

### Polar alignment with CPWI

CPWI's All-Star Polar Alignment (ASPA) works with the emulators: it measures
the emulated polar axis, then guides you through correcting it.

1. Switch on the **Wi-Fi dongle** and the **StarSense AutoAlign V2**.
2. Set up the **Telescope**: an equatorial mount (for example CGX), or an
   alt-az mount with **On a wedge** ticked (for example CPC Deluxe).
   Inclination is your latitude (without its sign) and Rotation 0 in the
   northern hemisphere or 180 in the southern. Set the initial position on the celestial equator near
   the meridian, with open sky either side: altitude 90 minus your latitude,
   azimuth 180 in the northern hemisphere or 0 in the southern. A telescope
   pointing along the polar axis cannot measure it, and one low in the sky
   fails its solves. Then switch it on.
3. In CPWI choose a mount type that matches (equatorial, or on a wedge), make
   sure its location is right, and connect to this computer.
4. Start the polar alignment. CPWI solves, turns the RA axis about 5 degrees
   and repeats; the completion goes 0, 25, 50, 75 and 100 % over five solves
   while the uncertainty shrinks.
5. In the adjustment step CPWI shows the **altitude and azimuth errors** of the
   emulated polar axis. Change **Inclination** to remove the altitude error and
   **Rotation** to remove the azimuth error, on the Telescope card, while the
   telescope runs: type a value and press Enter, or use the arrow keys (0.05
   degrees a step). The mount tilts at once and CPWI's errors follow. With the
   default placement error, lowering Inclination by 0.5 and raising Rotation by
   1.0 cancels it.

The errors CPWI shows are the difference between where the emulated polar axis
points and the celestial pole, so they reach zero when the knobs compensate the
placement error.

### Wi-Fi dongle

Apps such as CPWI and SkySafari connect to this computer as to a SkyPortal
Wi-Fi module. The dongle listens on port 2000, as real modules do, on the
loopback address and on this computer's network address, and **announces
itself** the way a real module does, with a broadcast on UDP port 55555 once a
second, so apps can find it without typing an address (a checkbox turns this
off). It answers as a SkyPortal Wi-Fi module, address 0xb3, version 3.1.9120.

- With **pass-through** on, the apps reach the telescope's bus through the
  scanner, which logs and decodes all their traffic (marked "Wi-Fi" and
  "To Wi-Fi" in the log).
- With it off, the apps only reach the emulated devices, and nothing they send
  reaches the telescope.

The dongle listens on your network, so any device on it can connect; your
firewall may ask whether to allow it.

### GPS, focuser, dew heater and StarSense AutoAlign (V1)

- **GPS** answers the hand controller's questions at 0xb0 with the coordinates
  you give (or **Use this device's location**), this computer's UTC time and a
  good satellite fix (12 in view, 9 used). Changing the coordinates applies at
  once.
- **Focuser** moves over time: go-to, rates 1 to 9, stops at its travel limits
  and can be calibrated. The Focuser box in Device control works with it.
- **Dew heater controller** has sliders for the air temperature, humidity,
  input voltage (11 to 13 V) and each heater's temperature, starting at 5 °C,
  75 % and 12.7 V. The two heaters and the 12 V output start on; it remembers
  being switched on and off, and shows the dew point and an LED that glows at
  the brightness it was given. The Dew heater box in Device control reads and
  controls a real or an emulated controller.
- **StarSense AutoAlign (V1)** answers discovery at 0xb4, so scans and the hand
  controller find the camera. **Send sample star list** sends a recorded star
  list from it, and the Star field box shows it.

### Limits of the emulation

The emulators follow captures of real devices and community reverse
engineering of the AUX protocol, not Celestron documentation, and they are not
a simulation of the sky: refraction is not modelled, and the StarSense camera
sees the position of the emulated telescope, not stars. Where a command's
meaning is not known, the emulator stays silent, so the command shows up in
**Unknown traffic**. Captures of real devices are always welcome: they are how
the emulators get closer to the real thing.
