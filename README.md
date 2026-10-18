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
   celestron-aux 1.0.1
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

There is no password: anyone who can open the page can control the mount. The
default address only accepts connections from the same computer; use
`0.0.0.0` only on a network you trust.
