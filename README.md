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

Dependencies point one way: `protocol` knows nothing about devices; features
listen to the `AuxBus`; only `app.py` knows about all of them; only `cli.py`
reads user input.

## Tests

```bash
pip install -e ".[dev]"
pytest
```

The suite includes a real StarSense frame captured from hardware
(`tests/fixtures/starsense_frame.hex`) and an end-to-end scan against a
simulated mount.
