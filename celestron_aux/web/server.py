"""HTTP plumbing for the web interface (standard library only).

Routes:

* ``GET /`` serves the single-page UI.
* ``GET /api/state``, ``/api/ports``, ``/api/discoveries`` and
  ``/api/commands?device=N`` return JSON.
* ``GET /api/download/csv|raw|unknown`` downloads a log file.
* ``GET /api/events`` streams bus traffic as Server-Sent Events.
* ``POST /api/replay?name=...&speed=...`` replays a raw capture sent as the body.
* ``POST /api/...`` performs actions (see ``POST_ROUTES``).

POST requests must carry ``X-Requested-With: celestron-aux``; all but the
replay upload take a JSON body.
A browser only sends that header from this page itself, so other websites
cannot drive the mount through a visitor's browser.
"""

from __future__ import annotations

import json
import logging
import queue
import sys
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from typing import Any, Callable, Dict, Mapping, Optional, Tuple
from urllib.parse import parse_qs, urlsplit

from .api import ApiError, Json, WebApi
from .feed import CLOSED
from .session import WebSession

log = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8080
REQUIRED_HEADER = ("X-Requested-With", "celestron-aux")
_MAX_BODY = 64 * 1024
_MAX_UPLOAD = 50 * 1024 * 1024  # replay captures
_PING_INTERVAL = 15.0

POST_ROUTES: Dict[str, Callable[[WebApi, Mapping[str, Any]], Json]] = {
    "/api/connect": WebApi.connect,
    "/api/disconnect": WebApi.disconnect,
    "/api/scan": WebApi.scan,
    "/api/identify": WebApi.identify,
    "/api/settings": WebApi.settings,
    "/api/gps": WebApi.gps,
    "/api/send": WebApi.send,
    "/api/starsense/capture": WebApi.starsense_capture,
    "/api/starsense/sample": WebApi.starsense_sample,
    "/api/starsense/emulate": WebApi.starsense_emulation,
    "/api/emulate": WebApi.emulate,
    "/api/emulate/version": WebApi.emulated_version,
    "/api/wifi": WebApi.wifi,
    "/api/mount_emulator": WebApi.mount_emulator,
    "/api/mount_emulator/adjust": WebApi.mount_adjust,
    "/api/dew": WebApi.dew_conditions,
    "/api/dew/control": WebApi.dew_control,
    "/api/starsense/solve": WebApi.plate_solve,
    "/api/clock/reset": WebApi.reset_clock,
    "/api/motion/move": WebApi.move,
    "/api/motion/stop": WebApi.stop_motion,
    "/api/focuser/move": WebApi.focuser_move,
    "/api/focuser/stop": WebApi.focuser_stop,
    "/api/focuser/goto": WebApi.focuser_goto,
    "/api/discoveries/clear": WebApi.clear_discoveries,
    "/api/mark": WebApi.mark,
    "/api/catalog/name": WebApi.name,
    "/api/catalog/remove": WebApi.unname,
}


def parse_listen_address(text: str) -> Tuple[str, int]:
    """Parse ``PORT``, ``HOST:PORT`` or ``HOST`` into (host, port)."""
    host, separator, port_text = text.rpartition(":")
    if not separator:
        host, port_text = (DEFAULT_HOST, text) if text.isdigit() else (text, str(DEFAULT_PORT))
    host = host.strip("[]") or DEFAULT_HOST
    if not port_text.isdigit() or not 0 <= int(port_text) <= 65535:
        raise ValueError(f"invalid port in {text!r}")
    return host, int(port_text)


class _HttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        """Stay quiet when a browser goes away mid-request; report anything else."""
        error = sys.exc_info()[1]
        if isinstance(error, ConnectionError):
            # BrokenPipeError and ConnectionResetError on macOS and Linux,
            # ConnectionAbortedError (WinError 10053) on Windows.
            log.debug("browser %s disconnected: %s", client_address, error)
            return
        super().handle_error(request, client_address)


def _load_page() -> bytes:
    return resources.files(__package__).joinpath("static/index.html").read_bytes()


class WebServer:
    """Serves the UI and API for a WebSession. Closing it does not close the session."""

    def __init__(self, session: WebSession, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
        self.feed = session.feed
        self.api = WebApi(session)
        self._page = _load_page()
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._httpd = _HttpServer((host, port), _make_handler(self))

    @property
    def address(self) -> Tuple[str, int]:
        host, port = self._httpd.server_address[:2]
        return str(host), int(port)

    def serve_forever(self) -> None:
        """Serve until ``close`` is called from another thread or Ctrl+C."""
        self._httpd.serve_forever(poll_interval=0.5)

    def start_in_background(self) -> None:
        self._thread = threading.Thread(target=self.serve_forever, name="web-server", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stopping.set()
        self.feed.close()
        if self._thread is not None:
            self._httpd.shutdown()
            self._thread.join()
        self._httpd.server_close()


def _make_handler(server: WebServer) -> type:
    class Handler(BaseHTTPRequestHandler):
        server_version = "celestron-aux"

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - stdlib signature
            log.debug("%s - %s", self.address_string(), format % args)

        # --- GET --------------------------------------------------------------

        def do_GET(self) -> None:
            url = urlsplit(self.path)
            if url.path in ("/", "/index.html"):
                self._send_bytes(HTTPStatus.OK, server._page, "text/html; charset=utf-8")
            elif url.path == "/api/state":
                self._send_json(HTTPStatus.OK, server.api.state())
            elif url.path == "/api/ports":
                self._ports()
            elif url.path == "/api/mount_emulator/status":
                self._send_json(HTTPStatus.OK, server.api.mount_status())
            elif url.path == "/api/discoveries":
                self._send_json(HTTPStatus.OK, server.api.discoveries())
            elif url.path == "/api/catalog":
                self._send_json(HTTPStatus.OK, server.api.catalog())
            elif url.path.startswith("/api/download/"):
                self._download(url.path.rsplit("/", 1)[-1])
            elif url.path == "/api/commands":
                self._commands(parse_qs(url.query))
            elif url.path == "/api/events":
                self._events()
            else:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})

        def _ports(self) -> None:
            try:
                self._send_json(HTTPStatus.OK, server.api.ports())
            except ApiError as exc:
                self._send_json(exc.status, {"error": exc.message, **exc.extra})

        def _download(self, kind: str) -> None:
            try:
                name, content_type, body = server.api.download(kind)
            except ApiError as exc:
                self._send_json(exc.status, {"error": exc.message, **exc.extra})
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _commands(self, query: Dict[str, Any]) -> None:
            try:
                device = int(query.get("device", [""])[0], 0)
                if not 0 <= device <= 0xFF:
                    raise ValueError
            except ValueError:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": "'device' must be a number from 0 to 255."})
                return
            self._send_json(HTTPStatus.OK, server.api.commands(device))

        def _events(self) -> None:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            subscriber, backlog = server.feed.subscribe()
            try:
                for item in backlog:
                    self._write_event(item)
                while not server._stopping.is_set():
                    try:
                        update = subscriber.get(timeout=_PING_INTERVAL)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")  # keeps proxies and browsers from timing out
                        self.wfile.flush()
                        continue
                    if update is CLOSED:
                        break
                    self._write_event(update)
            except ConnectionError:
                pass  # the browser went away (tab closed or reloaded, computer asleep)
            finally:
                server.feed.unsubscribe(subscriber)

        def _write_event(self, item: Json) -> None:
            data = json.dumps(item, separators=(",", ":"))
            self.wfile.write(f"id: {item['seq']}\ndata: {data}\n\n".encode("utf-8"))
            self.wfile.flush()

        # --- POST -------------------------------------------------------------

        def do_POST(self) -> None:
            url = urlsplit(self.path)
            route = POST_ROUTES.get(url.path)
            if route is None and url.path != "/api/replay":
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found."})
                return
            name, value = REQUIRED_HEADER
            if self.headers.get(name) != value:
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "Missing request header."})
                return
            try:
                if route is None:
                    result = self._replay(parse_qs(url.query))
                else:
                    result = route(server.api, self._read_json())
            except ApiError as exc:
                self._send_json(exc.status, {"error": exc.message, **exc.extra})
                return
            except Exception:
                log.exception("request %s failed", self.path)
                self._send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": "Internal error; see the log."})
                return
            self._send_json(HTTPStatus.OK, result)

        def _replay(self, query: Dict[str, Any]) -> Json:
            length = int(self.headers.get("Content-Length") or 0)
            if length > _MAX_UPLOAD:
                raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                               f"Capture files are limited to {_MAX_UPLOAD // (1024 * 1024)} MB.")
            capture = self.rfile.read(length) if length else b""
            try:
                speed = float(query.get("speed", ["1"])[0])
            except ValueError as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, "Speed must be a number.") from exc
            name = query.get("name", ["capture"])[0][:200]
            return server.api.replay(capture, name, speed)

        def _read_json(self) -> Mapping[str, Any]:
            length = int(self.headers.get("Content-Length") or 0)
            if length > _MAX_BODY:
                raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "Request body too large.")
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw or b"{}")
            except ValueError as exc:
                raise ApiError(HTTPStatus.BAD_REQUEST, "Request body must be JSON.") from exc
            if not isinstance(body, dict):
                raise ApiError(HTTPStatus.BAD_REQUEST, "Request body must be a JSON object.")
            return body

        # --- responses ----------------------------------------------------------

        def _send_json(self, status: int, payload: Json) -> None:
            body = json.dumps(payload).encode("utf-8")
            self._send_bytes(status, body, "application/json")

        def _send_bytes(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return Handler
