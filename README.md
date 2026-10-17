# celestron-aux

Scanner, monitor and protocol toolkit for the Celestron AUX bus: discover the
devices on a mount, watch and decode their traffic, send commands, emulate a GPS
module and capture StarSense star lists.

## Install and run

```bash
pip install .            # or: pip install -e ".[dev]" for development
celestron-aux wifi 1.2.3.4          # SkyPortal / Evolution Wi-Fi
celestron-aux serial COM3           # serial adapter on an AUX port
celestron-aux hc /dev/ttyUSB0       # through a hand controller
celestron-aux playback rawoutput.txt  # replay a recorded session
```

`python -m celestron_aux ...` works too. Run `celestron-aux -h` for every option
(`--csv`, `--raw`, `--no-scan`, `--keepalive`, `--image-dir`, `-v`, ...).
Inside the session, type `h` to list the interactive commands.

## Web interface

```bash
celestron-aux --web                           # http://localhost:8080, choose the connection in the page
celestron-aux wifi 1.2.3.4 --web              # connect at startup, then open the page
celestron-aux --web 0.0.0.0:8080              # reachable from a phone or tablet on your network
```

With `--web` the console stays silent and every console command is available
as a button in the browser: scans, keep-alive, CSV and raw recording, GPS
emulation, StarSense captures (with the star field drawn live), and sending any
command to any detected device. A red night-vision theme keeps your eyes dark
adapted at the telescope. The page loads nothing from the internet, so it works
on a mount's own Wi-Fi network.

Started without a mode and target, the page offers Wi-Fi, AUX port and hand
controller connections and lists the serial ports it finds. Because the page is
already open when the connection starts, it shows the traffic from the first
packet. Recording settings and GPS emulation can be set before connecting, and
you can disconnect and reconnect without restarting; the CSV log and raw
capture stay open across connections instead of being overwritten.

There is no password: anyone who can open the page can control the mount. The
default address only accepts connections from the same computer; use
`0.0.0.0` only on a network you trust.

## Layout

| Module | Responsibility |
|---|---|
| `protocol/packets.py` | Frame types, binary encoding, checksum, `3B 02 02` escaping |
| `protocol/framing.py` | Incremental stream parser, resync, out-of-frame counter |
| `protocol/units.py` | Binary angles, DMS and version formatting |
| `catalog.py` | Addresses, device/command names, mount models (data only) |
| `decoding.py` | Payload decoders registered per (device, command) |
| `transport/` | Wi-Fi, serial bus, hand controller and playback transports |
| `bus.py` | Receive and dispatch threads, paced sending, local echo |
| `registry.py` | Devices discovered and mount model |
| `scanner.py` | Active device discovery |
| `keepalive.py` | Idle-connection keep-alive |
| `gps_emulator.py` | Replies to GPS queries with a fixed position and UTC time |
| `starsense.py` | Star-list decoding, PNG rendering, capture sequence |
| `output.py` | Console view, CSV logger, raw recorder |
| `app.py` | Composition root: wires everything, exposes operations |
| `cli.py` | Argument parsing and the interactive console |
| `transport/factory.py` | Builds a transport from a mode and target |
| `web/session.py` | Connect, disconnect and reconnect; settings that outlive a connection |
| `web/api.py` | Browser operations as plain functions (no HTTP) |
| `web/feed.py` | Live traffic feed with backlog for each browser |
| `web/server.py` | Standard-library HTTP server, JSON API and event stream |
| `web/static/index.html` | The single-page interface |

Dependencies point one way: `protocol` knows nothing about devices; features
listen to the `AuxBus`; only `app.py` knows about all of them; `cli.py` and
`web/` are two interchangeable front ends on top of `app.py`.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

The suite includes a real StarSense frame captured from hardware
(`tests/fixtures/starsense_frame.hex`) and an end-to-end scan against a
simulated mount.
