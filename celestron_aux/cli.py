"""Command-line entry point and interactive console."""

from __future__ import annotations

import argparse
import logging
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Sequence

from . import __copyright__, __version__, catalog
from .app import AppConfig, ScannerApp
from .catalog import Addr
from .keepalive import DEFAULT_INTERVAL
from .protocol import AuxPacket
from .transport import Transport
from .transport.factory import MODES, create_transport

if TYPE_CHECKING:
    from .web import WebServer, WebSession


def make_transport(args: argparse.Namespace) -> Transport:
    return create_transport(args.mode, args.target, tcp_port=args.port, playback_speed=args.speed)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="celestron-aux",
        description="Scan, monitor and talk to devices on a Celestron AUX bus.",
    )
    parser.add_argument("mode", nargs="?", choices=MODES,
                        help="wifi: SkyPortal/Evolution Wi-Fi; serial: adapter on an AUX port; "
                             "hc: through a hand controller; playback: replay a raw capture. "
                             "Optional with --web: then you connect from the browser")
    parser.add_argument("target", nargs="?",
                        help="IP address (wifi), serial port (serial, hc) or capture file (playback)")
    parser.add_argument("--port", type=int, default=2000, help="TCP port in wifi mode (default: 2000)")
    parser.add_argument("--speed", type=float, default=1.0, help="playback speed multiplier (default: 1.0)")
    parser.add_argument("--no-scan", action="store_true", help="do not scan the bus on startup")
    parser.add_argument("--listen-only", action="store_true",
                        help="never transmit: no scan, keep-alive, GPS replies or commands")
    parser.add_argument("-v", "--verbose", action="store_true", help="show raw bytes of every frame")
    parser.add_argument("--keepalive", action=argparse.BooleanOptionalAction, default=None,
                        help="send keep-alive traffic (default: on for wifi, off otherwise)")
    parser.add_argument("--keepalive-interval", type=float, default=DEFAULT_INTERVAL, metavar="SECONDS")
    parser.add_argument("--csv", nargs="?", const="auxbuslog.csv", metavar="PATH",
                        help="log decoded traffic to CSV from the start (default path: auxbuslog.csv)")
    parser.add_argument("--raw", nargs="?", const="rawoutput.txt", metavar="PATH",
                        help="record raw traffic from the start (default path: rawoutput.txt)")
    parser.add_argument("--image-dir", type=Path, default=Path("."),
                        help="where StarSense images are saved (default: current directory)")
    parser.add_argument("--no-starsense-images", action="store_true",
                        help="do not save StarSense star lists as images")
    parser.add_argument("--catalog", type=Path, default=None, metavar="PATH",
                        help="your own device and command names (default: ~/.celestron-aux/catalog.json)")
    parser.add_argument("--web", nargs="?", const="127.0.0.1:8080", metavar="[HOST:]PORT",
                        help="run the browser interface instead of the console one; the console stays "
                             "silent. Without a mode and target you choose the connection in the browser "
                             "(default: 127.0.0.1:8080; use 0.0.0.0:8080 to allow other devices)")
    parser.add_argument("--log-level", default=None,
                        choices=("DEBUG", "INFO", "WARNING", "ERROR"),
                        help="diagnostic log level (default: INFO, or WARNING with --web)")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}. {__copyright__}.")
    return parser


def config_from_args(args: argparse.Namespace) -> AppConfig:
    config = AppConfig(
        quiet=bool(args.web),
        listen_only=args.listen_only,
        image_dir=args.image_dir,
        save_starsense_images=not args.no_starsense_images,
        verbose=args.verbose,
        keepalive=args.keepalive,
        keepalive_interval=args.keepalive_interval,
    )
    if args.csv:
        config.csv_path, config.log_csv = Path(args.csv), True
    if args.raw:
        config.raw_path, config.record_raw = Path(args.raw), True
    return config


@dataclass(frozen=True)
class _Command:
    key: str
    description: str
    run: Callable[[], bool]  # returns True to quit


class Repl:
    """Line-based interactive console on top of a running ScannerApp."""

    prompt = "Enter command: "

    def __init__(self, app: ScannerApp, read: Callable[[str], str] = input,
                 write: Callable[[str], None] = print) -> None:
        self.app = app
        self._read = read
        self._write = write
        commands = [
            _Command("d", "show detected devices", self._show_devices),
            _Command("c", "send a command to a device", self._send_command),
            _Command("s", "rescan the bus (known addresses)", lambda: self._scan(full=False)),
            _Command("a", "rescan the bus (all addresses)", lambda: self._scan(full=True)),
            _Command("i", "identify the mount", self._identify_mount),
            _Command("l", "toggle listen-only mode (never transmit)", self._toggle_listen_only),
            _Command("u", "show unknown traffic", self._show_unknown),
            _Command("m", "mark what just happened (a note in every log)", self._mark),
            _Command("p", "show the mount axis positions", self._show_positions),
            _Command("x", "stop both motors", self._stop_motors),
            _Command("k", "toggle keep-alive", self._toggle_keepalive),
            _Command("g", "toggle GPS emulation", self._toggle_gps),
            _Command("v", "toggle verbose output", self._toggle_verbose),
            _Command("f", "toggle CSV log", self._toggle_csv),
            _Command("w", "toggle raw capture recording", self._toggle_raw),
            _Command("ss", "toggle saving StarSense images", self._toggle_images),
            _Command("4", "trigger a StarSense capture", self._starsense_capture),
            _Command("3", "send the sample StarSense star list (testing)", self._send_sample),
            _Command("r", "reset the packet timer", self._reset_clock),
            _Command("o", "show the out-of-frame byte counter", self._out_of_frame),
            _Command("h", "show this help", self._help),
            _Command("q", "quit", lambda: True),
        ]
        self._commands: Dict[str, _Command] = {command.key: command for command in commands}

    def run(self) -> None:
        self._write("Type 'h' for help.")
        while True:
            try:
                key = self._read(self.prompt).strip().lower()
            except (EOFError, KeyboardInterrupt):
                self._write("")
                return
            if not key:
                continue
            command = self._commands.get(key)
            if command is None:
                self._write(f"Unknown command {key!r}. Type 'h' for help.")
            elif command.run():
                return

    # --- commands -----------------------------------------------------------

    def _help(self) -> bool:
        self.app.console.banner("Commands")
        self.app.console.lines([f"{c.key:>3}) {c.description}" for c in self._commands.values()])
        return False

    def _show_devices(self) -> bool:
        self.app.show_devices()
        return False

    def _blocked(self) -> bool:
        if self.app.listen_only:
            self._write("Listen-only mode is on; press 'l' to allow sending.")
            return True
        return False

    def _send_command(self) -> bool:
        if self._blocked():
            return False
        devices = self.app.devices()
        if not devices:
            self._write("No devices detected yet; run a scan first ('s').")
            return False
        self.app.show_devices()
        index = self._ask_index("Device number: ", len(devices))
        if index is None:
            return False
        address = devices[index][0]
        commands = catalog.commands_for(address)
        for number, (command, name) in enumerate(commands):
            self._write(f"{number:3d}) 0x{command:02x}  {name}")
        command_id = self._ask_command(commands)
        if command_id is None:
            return False
        payload = self._ask_payload()
        if payload is None:
            return False
        self.app.send(AuxPacket(Addr.SCANNER, address, command_id, payload))
        return False

    def _scan(self, full: bool) -> bool:
        if self._blocked():
            return False
        if not self.app.start_scan(full):
            self._write("A scan is already running.")
        return False

    def _identify_mount(self) -> bool:
        if self._blocked():
            return False
        if not self.app.identify_mount():
            self._write("No azimuth motor detected; run a scan first.")
        return False

    def _toggle_listen_only(self) -> bool:
        self.app.listen_only = not self.app.listen_only
        self._write("Listen-only mode on: nothing will be transmitted." if self.app.listen_only
                    else "Listen-only mode off.")
        return False

    def _show_unknown(self) -> bool:
        items = self.app.discovery.snapshot()
        self.app.console.banner(f"Unknown traffic ({len(items)})")
        if not items:
            self.app.console.message("(none so far)")
        for d in items:
            peers = ", ".join(f"0x{p:02x}" for p in sorted(d.peers))
            self.app.console.message(
                f"{catalog.device_name(d.device):<21} (0x{d.device:02x}) cmd 0x{d.command:02x}"
                f"  x{d.count}  with {peers}"
                + (f"  sent: {' '.join(d.requests)}" if d.requests else "")
                + (f"  replied: {' '.join(d.replies)}" if d.replies else ""))
        return False

    def _mark(self) -> bool:
        text = self._read("Mark (what just happened?): ")
        try:
            self.app.mark(text)
        except ValueError:
            self._write("Nothing marked: the mark was empty.")
        return False

    def _show_positions(self) -> bool:
        if not self.app.listen_only:
            self.app.motion.request_positions()
            time.sleep(0.5)  # give the motors time to answer
        axes = self.app.motion.snapshot()["axes"]
        for axis in axes.values():
            age = "" if axis["age"] is None else f"  ({axis['age']} s ago)"
            self._write(f"{axis['label']:<20} {axis['dms'] or 'unknown'}{age}")
        return False

    def _stop_motors(self) -> bool:
        if self._blocked():
            return False
        self.app.motion.stop()
        self._write("Stop sent to both motors.")
        return False

    def _toggle_keepalive(self) -> bool:
        keepalive = self.app.keepalive
        keepalive.enabled = not keepalive.enabled
        self._write(f"Keep-alive {'enabled' if keepalive.enabled else 'disabled'}.")
        return False

    def _toggle_gps(self) -> bool:
        if self.app.gps.enabled:
            self.app.disable_gps_emulation()
            self._write("GPS emulation disabled.")
            return False
        try:
            latitude = float(self._read("Latitude (decimal degrees, north positive): "))
            longitude = float(self._read("Longitude (decimal degrees, east positive): "))
            self.app.enable_gps_emulation(latitude, longitude)
        except ValueError as exc:
            self._write(f"GPS emulation not enabled: {exc}")
            return False
        self._write("GPS emulation enabled.")
        return False

    def _toggle_verbose(self) -> bool:
        console = self.app.console
        console.verbose = not console.verbose
        self._write(f"Verbose output {'on' if console.verbose else 'off'}.")
        return False

    def _toggle_csv(self) -> bool:
        if self.app.toggle_csv_log():
            self._write(f"Logging to {self.app.csv_log.path}.")
        else:
            self._write("CSV logging stopped.")
        return False

    def _toggle_raw(self) -> bool:
        if self.app.toggle_raw_recording():
            self._write(f"Recording raw traffic to {self.app.raw_recorder.path}.")
        else:
            self._write("Raw recording stopped.")
        return False

    def _toggle_images(self) -> bool:
        images = self.app.images
        images.enabled = not images.enabled
        self._write(f"StarSense image saving {'on' if images.enabled else 'off'}.")
        return False

    def _starsense_capture(self) -> bool:
        if self._blocked():
            return False
        self.app.start_starsense_capture()
        self._write("StarSense capture started (takes about 8 seconds).")
        return False

    def _send_sample(self) -> bool:
        if self._blocked():
            return False
        self.app.send_sample_star_list()
        return False

    def _reset_clock(self) -> bool:
        self.app.clock.reset()
        return False

    def _out_of_frame(self) -> bool:
        self._write(f"Out-of-frame bytes: {self.app.out_of_frame_bytes}")
        return False

    # --- input helpers --------------------------------------------------------

    def _ask_index(self, prompt: str, count: int) -> Optional[int]:
        answer = self._read(prompt).strip()
        if answer.isdigit() and int(answer) < count:
            return int(answer)
        self._write(f"Expected a number from 0 to {count - 1}.")
        return None

    def _ask_command(self, commands: Sequence[tuple]) -> Optional[int]:
        answer = self._read("Command number, or a hex code such as 0x05: ").strip().lower()
        try:
            if answer.startswith("0x"):
                value = int(answer, 16)
                if 0 <= value <= 0xFF:
                    return value
            elif answer.isdigit() and int(answer) < len(commands):
                return commands[int(answer)][0]
        except ValueError:
            pass
        self._write("Invalid command.")
        return None

    def _ask_payload(self) -> Optional[bytes]:
        answer = self._read("Payload in hex (blank for none): ").strip()
        try:
            return bytes.fromhex(answer)
        except ValueError:
            self._write("Invalid hex payload.")
            return None


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.mode and not args.target:
        parser.error(f"{args.mode} mode needs a target (IP address, serial port or capture file)")
    if not args.mode and not args.web:
        parser.error("give a mode and target, e.g. 'wifi 1.2.3.4', or use --web to connect from the browser")
    args.listen = None
    if args.web:
        from .web import parse_listen_address
        try:
            args.listen = parse_listen_address(args.web)
        except ValueError as exc:
            parser.error(f"--web: {exc}")
    return args


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    log_level = args.log_level or ("WARNING" if args.listen else "INFO")
    logging.basicConfig(level=log_level, format="%(levelname)s: %(message)s", stream=sys.stderr)
    user_catalog = catalog.UserCatalog(args.catalog or catalog.DEFAULT_USER_CATALOG)
    user_catalog.load()
    catalog.use_user_catalog(user_catalog)
    if args.listen:
        return _run_web(args)
    return _run_console(args)


def _run_console(args: argparse.Namespace) -> int:
    app = ScannerApp(make_transport(args), config_from_args(args))
    try:
        app.start(initial_scan=not args.no_scan and args.mode != "playback")
    except (OSError, ValueError) as exc:
        logging.error("could not start: %s", exc)
        return 2
    try:
        Repl(app).run()
    finally:
        app.close()
    return 0


def _run_web(args: argparse.Namespace) -> int:
    from .web import WebServer, WebSession

    session = WebSession(config_from_args(args))
    try:
        web = WebServer(session, *args.listen)  # bind first, so a busy port fails fast
    except OSError as exc:
        logging.error("could not start the web interface: %s", exc)
        session.close()
        return 2
    try:
        if args.mode:
            session.connect_transport(make_transport(args), mode=args.mode,
                                      scan=not args.no_scan and args.mode != "playback")
        _serve_web(web, session)
    except (OSError, ValueError) as exc:
        logging.error("could not connect: %s", exc)
        return 2
    finally:
        web.close()
        session.close()
    return 0


def _serve_web(web: "WebServer", session: "WebSession") -> None:
    host, port = web.address
    shown = "localhost" if host in ("127.0.0.1", "::1") else host
    print(f"celestron-aux {__version__}\n{__copyright__}", flush=True)
    print(f"Web interface: http://{shown}:{port}/  (press Ctrl+C to stop)", flush=True)
    if session.app is None:
        print("Open it and choose how to connect to the mount.", flush=True)
    if host in ("0.0.0.0", "::"):
        print("Any device on your network can open it and control the mount; there is no password.",
              flush=True)
    try:
        web.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
