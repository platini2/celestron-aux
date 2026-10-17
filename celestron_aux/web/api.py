"""The operations the browser can perform, independent of HTTP.

Every method takes plain Python values (a parsed JSON body) and returns a
JSON-ready dict, or raises ApiError with an HTTP status and a message the UI
can show as-is.
"""

from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

from .. import __version__, catalog
from ..app import ScannerApp
from ..catalog import Addr
from ..protocol import AuxPacket
from .feed import TrafficFeed

Json = Dict[str, Any]


class ApiError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class WebApi:
    def __init__(self, app: ScannerApp, feed: TrafficFeed) -> None:
        self.app = app
        self.feed = feed

    # --- reads --------------------------------------------------------------

    def state(self) -> Json:
        app = self.app
        model = app.registry.mount_model
        position = app.gps.position
        return {
            "version": __version__,
            "connection": app.transport.description,
            "connected": app.bus.is_open,
            "scanning": app.scanning,
            "mount": catalog.mount_name(model),
            "mount_model": None if model is None else f"0x{model:04x}",
            "devices": [
                {"address": address, "name": catalog.device_name(address), "version": version}
                for address, version in app.devices()
            ],
            "settings": {
                "keepalive": app.keepalive.enabled,
                "csv": app.csv_log.active,
                "csv_path": str(app.config.csv_path),
                "raw": app.raw_recorder.active,
                "raw_path": str(app.config.raw_path),
                "images": app.images.enabled,
                "image_dir": str(app.config.image_dir),
            },
            "gps": {
                "enabled": position is not None,
                "latitude": position[0] if position else None,
                "longitude": position[1] if position else None,
            },
            "out_of_frame_bytes": app.out_of_frame_bytes,
            "stars": self.feed.latest_stars,
        }

    def commands(self, device: int) -> Json:
        return {
            "device": device,
            "commands": [{"id": cid, "name": name} for cid, name in catalog.commands_for(device)],
        }

    # --- actions ------------------------------------------------------------

    def scan(self, body: Mapping[str, Any]) -> Json:
        full = _bool(body, "full", default=False)
        if not self.app.start_scan(full):
            raise ApiError(409, "A scan is already running.")
        return {"message": "Scanning all addresses (about 40 seconds)." if full else "Scanning the bus."}

    def identify(self, body: Mapping[str, Any]) -> Json:
        if not self.app.identify_mount():
            raise ApiError(409, "No azimuth motor detected yet. Scan the bus first.")
        return {"message": "Asked the azimuth motor for the mount model."}

    def settings(self, body: Mapping[str, Any]) -> Json:
        app = self.app
        changed = []
        if "keepalive" in body:
            app.keepalive.enabled = _bool(body, "keepalive")
            changed.append(f"Keep-alive {'on' if app.keepalive.enabled else 'off'}")
        if "csv" in body:
            app.set_csv_log(_bool(body, "csv"))
            changed.append(f"CSV log {'on' if app.csv_log.active else 'off'}")
        if "raw" in body:
            app.set_raw_recording(_bool(body, "raw"))
            changed.append(f"Raw recording {'on' if app.raw_recorder.active else 'off'}")
        if "images" in body:
            app.images.enabled = _bool(body, "images")
            changed.append(f"StarSense images {'on' if app.images.enabled else 'off'}")
        if not changed:
            raise ApiError(400, "Nothing to change.")
        return {"message": ". ".join(changed) + "."}

    def gps(self, body: Mapping[str, Any]) -> Json:
        if not _bool(body, "enabled"):
            self.app.disable_gps_emulation()
            return {"message": "GPS emulation stopped."}
        latitude = _float(body, "latitude")
        longitude = _float(body, "longitude")
        try:
            self.app.enable_gps_emulation(latitude, longitude)
        except ValueError as exc:
            raise ApiError(400, f"{str(exc).capitalize()}.") from exc
        return {"message": f"GPS emulation started at {latitude:.5f}, {longitude:.5f}."}

    def send(self, body: Mapping[str, Any]) -> Json:
        device = _byte(body, "device")
        command = _byte(body, "command")
        payload_text = str(body.get("payload") or "").replace(" ", "")
        try:
            payload = bytes.fromhex(payload_text)
        except ValueError as exc:
            raise ApiError(400, "Payload must be hex, for example 03e800.") from exc
        if not self.app.send(AuxPacket(Addr.SCANNER, device, command, payload)):
            raise ApiError(503, "The connection to the mount is closed.")
        name = catalog.command_name(device, command)
        return {"message": f"Sent {name} (0x{command:02x}) to {catalog.device_name(device)}."}

    def starsense_capture(self, body: Mapping[str, Any]) -> Json:
        self.app.start_starsense_capture()
        return {"message": "StarSense capture started. The star list arrives in about 8 seconds."}

    def starsense_sample(self, body: Mapping[str, Any]) -> Json:
        if not self.app.send_sample_star_list():
            raise ApiError(503, "The connection to the mount is closed.")
        return {"message": "Sent the sample star list."}

    def reset_clock(self, body: Mapping[str, Any]) -> Json:
        self.app.clock.reset()
        return {"message": "Packet timer reset to zero."}


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


def _byte(body: Mapping[str, Any], key: str) -> int:
    value = body.get(key)
    try:
        if isinstance(value, str):
            number = int(value, 0)
        elif isinstance(value, int) and not isinstance(value, bool):
            number = value
        else:
            raise ValueError
    except ValueError as exc:
        raise ApiError(400, f"'{key}' must be a number from 0 to 255.") from exc
    if not 0 <= number <= 0xFF:
        raise ApiError(400, f"'{key}' must be a number from 0 to 255.")
    return number
