"""The operations the browser can perform, independent of HTTP.

Every method takes plain Python values (a parsed JSON body) and returns a
JSON-ready dict, or raises ApiError with an HTTP status and a message the UI
can show as-is.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

from .. import __copyright__, __version__, catalog
from ..app import ScannerApp
from ..catalog import Addr
from ..motion import Axis, MotionRefused
from ..protocol import AuxPacket
from ..transport.factory import LIVE_MODES
from ..transport.tcp import DEFAULT_PORT
from .session import AlreadyConnected, WebSession

Json = Dict[str, Any]
Download = Tuple[str, str, bytes]  # file name, content type, body

MAX_REPLAY_SPEED = 1000.0
LISTEN_ONLY_MESSAGE = "Listen-only mode is on. Turn it off to send anything to the mount."


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class WebApi:
    def __init__(self, session: WebSession) -> None:
        self.session = session

    def _app(self) -> ScannerApp:
        app = self.session.app
        if app is None:
            raise ApiError(409, "Not connected. Connect to the mount first.")
        return app

    def _transmitting_app(self) -> ScannerApp:
        """The connected app, for actions that send; refused in listen-only mode."""
        app = self._app()
        if app.listen_only:
            raise ApiError(409, LISTEN_ONLY_MESSAGE)
        return app

    # --- reads --------------------------------------------------------------

    def state(self) -> Json:
        session = self.session
        app = session.app
        model = app.registry.mount_model if app else None
        position = session.gps_position
        config = session.config
        return {
            "version": __version__,
            "copyright": __copyright__,
            "session": None if app is None else {
                "connection": app.transport.description,
                "mode": session.mode,
                "open": app.bus.is_open,
            },
            "connected": app is not None and app.bus.is_open,
            "scanning": app is not None and app.scanning,
            "mount": catalog.mount_name(model),
            "mount_model": None if model is None else f"0x{model:04x}",
            "devices": [] if app is None else [
                {"address": address, "name": catalog.device_name(address), "version": version}
                for address, version in app.devices()
            ],
            "settings": {
                "keepalive": session.keepalive_enabled(),
                "csv": session.csv_log.active,
                "csv_path": str(config.csv_path),
                "raw": session.raw_recorder.active,
                "raw_path": str(config.raw_path),
                "images": config.save_starsense_images,
                "image_dir": str(config.image_dir),
                "listen_only": session.listen_only,
                "motion_enabled": config.motion_enabled,
                "gps_polling": config.gps_polling,
                "focuser_polling": config.focuser_polling,
            },
            "motion": None if app is None else app.motion.snapshot(),
            "gps_status": None if app is None else {"detected": app.registry.contains(Addr.GPS),
                                                    **app.gps_monitor.snapshot()},
            "focuser": None if app is None else {"detected": app.registry.contains(Addr.FOCUSER),
                                                 **app.focuser.snapshot()},
            "unknown_count": len(session.discovery),
            "gps": {
                "enabled": position is not None,
                "latitude": position[0] if position else None,
                "longitude": position[1] if position else None,
            },
            "out_of_frame_bytes": app.out_of_frame_bytes if app else 0,
            "catalog": {"names": len(catalog.user_catalog().entries()),
                        "load_error": catalog.user_catalog().load_error},
            "stars": session.feed.latest_stars,
        }

    def commands(self, device: int) -> Json:
        return {
            "device": device,
            "commands": [{"id": cid, "name": name} for cid, name in catalog.commands_for(device)],
        }

    def ports(self) -> Json:
        try:
            from ..transport.serial_port import list_serial_ports
            ports = list_serial_ports()
        except Exception as exc:  # pyserial missing or the OS refused to enumerate
            raise ApiError(500, f"Could not list serial ports: {exc}") from exc
        return {"ports": [{"device": device, "description": description} for device, description in ports]}

    def discoveries(self) -> Json:
        return {"items": [item.as_dict() for item in self.session.discovery.snapshot()]}

    def download(self, kind: str) -> Download:
        """The CSV log, the raw capture, or the unknown-traffic summary as a file."""
        config = self.session.config
        if kind == "unknown":
            return "unknown-traffic.csv", "text/csv; charset=utf-8", self.session.discovery.to_csv().encode("utf-8")
        if kind == "csv":
            return _read_log(Path(config.csv_path), "text/csv; charset=utf-8",
                             "No CSV log yet. Turn on CSV log first.")
        if kind == "raw":
            return _read_log(Path(config.raw_path), "text/plain; charset=ascii",
                             "No raw capture yet. Turn on raw capture first.")
        raise ApiError(404, "Not found.")

    # --- connection ---------------------------------------------------------

    def connect(self, body: Mapping[str, Any]) -> Json:
        mode = body.get("mode")
        if mode not in LIVE_MODES:
            raise ApiError(400, "Choose Wi-Fi, AUX port or hand controller.")
        target = str(body.get("target") or "").strip()
        if not target:
            raise ApiError(400, "Enter the mount's IP address." if mode == "wifi" else "Choose a serial port.")
        tcp_port = _int(body, "tcp_port", 1, 65535, default=DEFAULT_PORT)
        scan = _bool(body, "scan", default=True)
        try:
            description = self.session.connect(mode, target, tcp_port=tcp_port, scan=scan)
        except AlreadyConnected as exc:
            raise ApiError(409, str(exc)) from exc
        except (OSError, ValueError) as exc:
            raise ApiError(502, f"Could not connect to {target}: {exc}") from exc
        return {"message": f"Connected: {description}." + (" Scanning the bus." if scan else "")}

    def replay(self, capture: bytes, name: str, speed: float) -> Json:
        if not capture.strip():
            raise ApiError(400, "The capture file is empty.")
        if not 0 < speed <= MAX_REPLAY_SPEED:
            raise ApiError(400, f"Speed must be between 0 and {MAX_REPLAY_SPEED:g}.")
        try:
            self.session.replay(capture, name or "capture", speed)
        except AlreadyConnected as exc:
            raise ApiError(409, str(exc)) from exc
        except ValueError as exc:
            raise ApiError(400, f"That file is not a raw capture: {exc}") from exc
        return {"message": f"Replaying {name or 'capture'}."}

    def disconnect(self, body: Mapping[str, Any]) -> Json:
        if not self.session.disconnect():
            raise ApiError(409, "Not connected.")
        return {"message": "Disconnected."}

    # --- actions on the mount -----------------------------------------------

    def scan(self, body: Mapping[str, Any]) -> Json:
        full = _bool(body, "full", default=False)
        if not self._transmitting_app().start_scan(full):
            raise ApiError(409, "A scan is already running.")
        return {"message": "Scanning all addresses (about 40 seconds)." if full else "Scanning the bus."}

    def identify(self, body: Mapping[str, Any]) -> Json:
        if not self._transmitting_app().identify_mount():
            raise ApiError(409, "No azimuth motor detected yet. Scan the bus first.")
        return {"message": "Asked the azimuth motor for the mount model."}

    def send(self, body: Mapping[str, Any]) -> Json:
        app = self._transmitting_app()
        device = _int(body, "device", 0, 0xFF)
        command = _int(body, "command", 0, 0xFF)
        payload_text = str(body.get("payload") or "").replace(" ", "")
        try:
            payload = bytes.fromhex(payload_text)
        except ValueError as exc:
            raise ApiError(400, "Payload must be hex, for example 03e800.") from exc
        if not app.send(AuxPacket(Addr.SCANNER, device, command, payload)):
            raise ApiError(503, "The connection to the mount is closed.")
        name = catalog.command_name(device, command)
        return {"message": f"Sent {name} (0x{command:02x}) to {catalog.device_name(device)}."}

    def starsense_capture(self, body: Mapping[str, Any]) -> Json:
        app = self._transmitting_app()
        if not app.registry.contains(Addr.STARSENSE_CAMERA):
            raise ApiError(409, "No StarSense camera detected. Scan the bus first.")
        app.start_starsense_capture()
        return {"message": "StarSense capture started. The star list arrives in about 8 seconds."}

    def starsense_sample(self, body: Mapping[str, Any]) -> Json:
        if not self._transmitting_app().send_sample_star_list():
            raise ApiError(503, "The connection to the mount is closed.")
        return {"message": "Sent the sample star list."}

    def move(self, body: Mapping[str, Any]) -> Json:
        """Start or continue a held move. The page repeats this while the button is held."""
        app = self._transmitting_app()
        axis = _axis(body)
        direction = _int(body, "direction", -1, 1)
        rate = _int(body, "rate", 1, 9)
        if direction == 0:
            raise ApiError(400, "'direction' must be 1 or -1.")
        try:
            app.motion.move(axis, direction, rate)
        except MotionRefused as exc:
            raise ApiError(503, str(exc)) from exc
        return {"moving": True}

    def stop_motion(self, body: Mapping[str, Any]) -> Json:
        app = self._transmitting_app()
        axis = _axis(body) if body.get("axis") else None
        if not app.motion.stop(axis):
            raise ApiError(503, "The stop could not be sent; the connection may be closed.")
        return {"message": f"Stopped the {axis.label.lower()}." if axis else "Stopped both axes."}

    def mark(self, body: Mapping[str, Any]) -> Json:
        app = self._app()
        try:
            text = app.mark(str(body.get("text") or ""))
        except ValueError as exc:
            raise ApiError(400, "Type what just happened first.") from exc
        return {"message": f"Marked: {text}"}

    # --- your names for devices and commands -----------------------------------

    def catalog(self) -> Json:
        user = catalog.user_catalog()
        entries = []
        for entry in user.entries():
            entries.append({**entry, "device_label": catalog.device_name(entry["device"])})
        return {"path": None if user.path is None else str(user.path), "load_error": user.load_error,
                "entries": entries}

    def name(self, body: Mapping[str, Any]) -> Json:
        """Give a name to a device and/or one of its commands, and save it."""
        user = catalog.user_catalog()
        device = _int(body, "device", 0, 0xFF)
        device_name = str(body.get("device_name") or "").strip()
        command_name = str(body.get("command_name") or "").strip()
        command = _int(body, "command", 0, 0xFF) if command_name else None
        if not device_name and not command_name:
            raise ApiError(400, "Type a name first.")
        try:
            if device_name:
                user.set_device(device, device_name)
            if command is not None:
                user.set_command(device, command, command_name)
            user.save()
        except catalog.CatalogError as exc:
            user.load()  # drop the unsaved change
            raise ApiError(400 if "name" in str(exc).lower() else 500, str(exc)) from exc
        self.session.discovery.prune()
        named = [n for n in (device_name, command_name) if n]
        return {"message": f"Saved {' and '.join(named)} to {user.path}."}

    def unname(self, body: Mapping[str, Any]) -> Json:
        """Forget a name you gave (the built-in name, if any, comes back)."""
        user = catalog.user_catalog()
        device = _int(body, "device", 0, 0xFF)
        command = None if body.get("command") is None else _int(body, "command", 0, 0xFF)
        try:
            if command is None:
                user.set_device(device, None)
            else:
                user.set_command(device, command, None)
            user.save()
        except catalog.CatalogError as exc:
            raise ApiError(500, str(exc)) from exc
        return {"message": "Name removed."}

    def reset_clock(self, body: Mapping[str, Any]) -> Json:
        self._app().clock.reset()
        return {"message": "Packet timer reset to zero."}

    # --- settings (allowed before connecting; they apply on connect) ----------

    def settings(self, body: Mapping[str, Any]) -> Json:
        session = self.session
        changed = []
        if "listen_only" in body:
            session.set_listen_only(_bool(body, "listen_only"))
            changed.append("Listen-only mode on: nothing will be transmitted" if session.listen_only
                           else "Listen-only mode off")
        if "motion_enabled" in body:
            session.set_motion_enabled(_bool(body, "motion_enabled"))
            changed.append(f"Mount control {'on' if session.config.motion_enabled else 'off'}")
        if "gps_polling" in body:
            session.set_gps_polling(_bool(body, "gps_polling"))
            changed.append(f"GPS updates {'on' if session.config.gps_polling else 'off'}")
        if "focuser_polling" in body:
            session.set_focuser_polling(_bool(body, "focuser_polling"))
            changed.append(f"Focuser updates {'on' if session.config.focuser_polling else 'off'}")
        if "keepalive" in body:
            session.set_keepalive(_bool(body, "keepalive"))
            changed.append(f"Keep-alive {'on' if session.keepalive_enabled() else 'off'}")
        if "csv" in body:
            session.set_csv_log(_bool(body, "csv"))
            changed.append(f"CSV log {'on' if session.csv_log.active else 'off'}")
        if "raw" in body:
            session.set_raw_recording(_bool(body, "raw"))
            changed.append(f"Raw capture {'on' if session.raw_recorder.active else 'off'}")
        if "images" in body:
            session.set_images(_bool(body, "images"))
            changed.append(f"StarSense images {'on' if session.config.save_starsense_images else 'off'}")
        if not changed:
            raise ApiError(400, "Nothing to change.")
        return {"message": ". ".join(changed) + "."}

    def clear_discoveries(self, body: Mapping[str, Any]) -> Json:
        self.session.discovery.clear()
        return {"message": "Unknown-traffic list cleared."}

    def gps(self, body: Mapping[str, Any]) -> Json:
        if not _bool(body, "enabled"):
            self.session.set_gps(None)
            return {"message": "GPS emulation stopped."}
        latitude = _float(body, "latitude")
        longitude = _float(body, "longitude")
        try:
            self.session.set_gps((latitude, longitude))
        except ValueError as exc:
            raise ApiError(400, f"{str(exc).capitalize()}.") from exc
        when = "" if self.session.app else " It starts answering as soon as you connect."
        return {"message": f"GPS emulation set to {latitude:.5f}, {longitude:.5f}.{when}"}


def _axis(body: Mapping[str, Any]) -> Axis:
    try:
        return Axis(body.get("axis"))
    except ValueError as exc:
        raise ApiError(400, "'axis' must be \"azm\" or \"alt\".") from exc


def _read_log(path: Path, content_type: str, missing: str) -> Download:
    try:
        return path.name, content_type, path.read_bytes()
    except FileNotFoundError as exc:
        raise ApiError(404, missing) from exc


# --- body validation --------------------------------------------------------

def _bool(body: Mapping[str, Any], key: str, default: Optional[bool] = None) -> bool:
    value = body.get(key, default)
    if not isinstance(value, bool):
        raise ApiError(400, f"'{key}' must be true or false.")
    return value


def _float(body: Mapping[str, Any], key: str) -> float:
    value = body.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ApiError(400, f"'{key}' must be a number.")
    try:
        return float(value)
    except ValueError as exc:
        raise ApiError(400, f"'{key}' must be a number.") from exc


def _int(body: Mapping[str, Any], key: str, low: int, high: int, default: Optional[int] = None) -> int:
    value = body.get(key, default)
    message = f"'{key}' must be a number from {low} to {high}."
    try:
        if isinstance(value, str):
            number = int(value, 0)
        elif isinstance(value, int) and not isinstance(value, bool):
            number = value
        else:
            raise ValueError
    except ValueError as exc:
        raise ApiError(400, message) from exc
    if not low <= number <= high:
        raise ApiError(400, message)
    return number
