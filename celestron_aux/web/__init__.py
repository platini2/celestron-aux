"""Browser interface: a JSON API, a live traffic stream and a single-page UI."""

from .server import WebServer, parse_listen_address

__all__ = ["WebServer", "parse_listen_address"]
