"""Emulates a Wi-Fi dongle: apps such as CPWI or SkySafari connect over TCP.

The dongle listens on the loopback address and on this computer's main network
address. Apps send and receive plain AUX packets, as with a real SkyPortal or
Evolution Wi-Fi module.

* With pass-through on, an app's packets go onto the telescope's bus and
  everything on the bus (including the scanner's own packets and the emulated
  devices' replies) goes back to the app, like a real module bridging the two.
* With pass-through off, the app only reaches the emulated devices: its packets
  never reach the telescope, and the emulators' replies go only to the app.

Like a real SkyPortal module, it announces itself so apps can find it: once a
second it broadcasts the ASCII text {"AMW007"} (the module's Wi-Fi chip) to UDP
port 55555. Apps such as CPWI take the sender's address and connect to it.

Every packet from an app also enters the scanner's traffic stream, marked with
its origin, so it is logged and decoded like the rest.
"""

from __future__ import annotations

import logging
import socket
import threading
from typing import Callable, Dict, List, Optional, Set, Tuple

from .bus import AuxBus, BusEvent
from .protocol import AuxPacket, FrameParser, StarSenseFrame

log = logging.getLogger(__name__)

DEFAULT_PORT = 2000  # what real SkyPortal and Evolution Wi-Fi modules use
ANNOUNCEMENT = b'{"AMW007"}'  # what a SkyPortal module broadcasts, braces and quotes included
ANNOUNCE_TO = ("255.255.255.255", 55555)
ANNOUNCE_EVERY = 1.0  # seconds
_ACCEPT_POLL = 0.5  # seconds between checks for stop() while waiting for an app
ORIGIN_PREFIX = "wifi:"  # BusEvent.origin of packets that came from an app


def main_ip_address() -> Optional[str]:
    """This computer's address on its main network, or None (no network)."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(("192.0.2.1", 9))  # a documentation address: nothing is sent, it only picks a route
        address = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    return None if address.startswith("127.") or address == "0.0.0.0" else address


class _Client:
    def __init__(self, sock: socket.socket, peer: Tuple[str, int]) -> None:
        self.sock = sock
        self.name = f"{peer[0]}:{peer[1]}"
        self.origin = ORIGIN_PREFIX + self.name
        self.lock = threading.Lock()

    def send(self, data: bytes) -> bool:
        with self.lock:
            try:
                self.sock.sendall(data)
                return True
            except OSError:
                return False

    def close(self) -> None:
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()


class WifiBridge:
    """The dongle's TCP server and the routing between apps, the bus and the emulators."""

    def __init__(self, bus: AuxBus, *, passthrough: bool = False,
                 address_finder: Callable[[], Optional[str]] = main_ip_address,
                 announce_to: Tuple[str, int] = ANNOUNCE_TO) -> None:
        self._bus = bus
        self._find_address = address_finder
        self._announce_to = announce_to
        self._lock = threading.Lock()
        self._passthrough = passthrough
        self._announce = True
        self.announcements = 0  # broadcasts sent since the dongle started
        self._servers: List[socket.socket] = []
        self._clients: List[_Client] = []
        self._remote_addresses: Set[int] = set()  # bus addresses the apps use, e.g. 0x20 for CPWI
        self._stop = threading.Event()
        self._accept_threads: List[threading.Thread] = []
        self.port: Optional[int] = None
        self.listening: List[str] = []

    # --- control --------------------------------------------------------------------

    @property
    def running(self) -> bool:
        with self._lock:
            return bool(self._servers)

    @property
    def announce(self) -> bool:
        with self._lock:
            return self._announce

    @announce.setter
    def announce(self, value: bool) -> None:
        with self._lock:
            self._announce = value

    @property
    def passthrough(self) -> bool:
        with self._lock:
            return self._passthrough

    @passthrough.setter
    def passthrough(self, value: bool) -> None:
        with self._lock:
            self._passthrough = value

    def start(self, port: int = DEFAULT_PORT) -> List[str]:
        """Listen on loopback and the main address. Returns where it listens; OSError if neither works."""
        self.stop()
        hosts = ["127.0.0.1"]
        main = self._find_address()
        if main:
            hosts.append(main)
        servers, listening, errors = [], [], []
        for host in hosts:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                server.bind((host, port))
                server.listen()
                server.settimeout(_ACCEPT_POLL)  # so the accept loop notices stop() and frees the port
            except OSError as exc:
                server.close()
                errors.append(f"{host}:{port}: {exc.strerror or exc}")
                continue
            servers.append(server)
            listening.append(f"{host}:{server.getsockname()[1]}")
        if not servers:
            raise OSError("could not listen on " + "; ".join(errors))
        self._stop.clear()
        with self._lock:
            self._servers, self.listening = servers, listening
            self.port = servers[0].getsockname()[1]
        threads = [threading.Thread(target=self._accept_loop, args=(server,), name="wifi-accept", daemon=True)
                   for server in servers]
        threads.append(threading.Thread(target=self._announce_loop, name="wifi-announce", daemon=True))
        self.announcements = 0
        self._accept_threads = threads
        for thread in threads:
            thread.start()
        if errors:
            log.warning("Wi-Fi dongle: %s", "; ".join(errors))
        return listening

    def stop(self) -> None:
        """Stop listening and disconnect the apps. The port is free again when this returns."""
        self._stop.set()
        with self._lock:
            servers, self._servers = self._servers, []
            clients, self._clients = self._clients, []
            threads, self._accept_threads = self._accept_threads, []
            self.listening = []
        for thread in threads:  # a thread waiting in accept() keeps the port bound until it returns
            thread.join(timeout=2 * _ACCEPT_POLL)
        for server in servers:
            server.close()
        for client in clients:
            client.close()

    def snapshot(self) -> Dict[str, object]:
        with self._lock:
            return {"running": bool(self._servers), "passthrough": self._passthrough, "announce": self._announce,
                    "announcements": self.announcements, "port": self.port,
                    "listening": list(self.listening), "clients": [c.name for c in self._clients]}

    # --- routing ------------------------------------------------------------------------

    def wants_virtual(self, packet: AuxPacket) -> bool:
        """Should this emulator reply go only to the apps (pass-through off, addressed to an app)?"""
        with self._lock:
            return bool(self._servers) and not self._passthrough and packet.destination in self._remote_addresses

    def deliver(self, data: bytes) -> None:
        """Send bytes to every connected app."""
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            if not client.send(data):
                self._drop(client)

    def __call__(self, event: BusEvent) -> None:
        """Bus listener: with pass-through on, everything on the bus goes to the apps."""
        if not isinstance(event.frame, (AuxPacket, StarSenseFrame)) or not event.raw:
            return
        with self._lock:
            if not self._servers or not self._passthrough:
                return
            clients = [c for c in self._clients if c.origin != event.origin]  # not back to its sender
        for client in clients:
            if not client.send(event.raw):
                self._drop(client)

    # --- threads ------------------------------------------------------------------------

    def _announce_loop(self) -> None:
        """Broadcast the module's announcement every second while announcing is on."""
        sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        warned = False
        try:
            while not self._stop.is_set():
                if self.announce:
                    try:
                        sender.sendto(ANNOUNCEMENT, self._announce_to)
                        self.announcements += 1
                    except OSError as exc:  # e.g. no network: try again next second, warn once
                        if not warned:
                            log.warning("Wi-Fi dongle: could not announce itself: %s", exc)
                            warned = True
                self._stop.wait(ANNOUNCE_EVERY)
        finally:
            sender.close()

    def _accept_loop(self, server: socket.socket) -> None:
        while not self._stop.is_set():
            try:
                sock, peer = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            sock.settimeout(None)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            client = _Client(sock, peer)
            with self._lock:
                self._clients.append(client)
            log.info("Wi-Fi dongle: %s connected", client.name)
            threading.Thread(target=self._client_loop, args=(client,), name="wifi-client", daemon=True).start()

    def _client_loop(self, client: _Client) -> None:
        parser = FrameParser()
        while not self._stop.is_set():
            try:
                data = client.sock.recv(4096)
            except OSError:
                break
            if not data:
                break
            for parsed in parser.feed(data):
                self._from_app(client, parsed.frame, parsed.raw)
        self._drop(client)

    def _from_app(self, client: _Client, frame: object, raw: bytes) -> None:
        if not isinstance(frame, (AuxPacket, StarSenseFrame)):
            log.debug("Wi-Fi dongle: dropped a corrupted frame from %s", client.name)
            return
        if isinstance(frame, AuxPacket):
            with self._lock:
                self._remote_addresses.add(frame.source)
        if self.passthrough:
            if not self._bus.send(frame, origin=client.origin, pace=False):  # refused in listen-only mode
                self._bus.inject(frame, raw, origin=client.origin)  # still logged and seen by the emulators
        else:
            self._bus.inject(frame, raw, origin=client.origin)  # only the emulators and the logs see it

    def _drop(self, client: _Client) -> None:
        with self._lock:
            if client not in self._clients:
                return
            self._clients.remove(client)
        client.close()
        log.info("Wi-Fi dongle: %s disconnected", client.name)
