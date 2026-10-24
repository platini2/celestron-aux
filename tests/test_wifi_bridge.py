import io
import socket
import time

import pytest

from celestron_aux.app import AppConfig, ScannerApp
from celestron_aux.protocol import AuxPacket, FrameParser
from celestron_aux.wifi_bridge import ORIGIN_PREFIX, WifiBridge, main_ip_address

from .fakes import FakeMount, wait_until


class App:
    """A scanner connected to a simulated telescope, with its Wi-Fi dongle on loopback only."""

    def __init__(self, passthrough, **config):
        self.mount = FakeMount({0x10: b"\x07\x0b"})
        self.app = ScannerApp(self.mount, AppConfig(save_starsense_images=False, **config), stream=io.StringIO())
        self.app.bus._inter_frame_delay = 0
        self.app.wifi._find_address = lambda: None  # loopback only, whatever network the test runs on
        self.events = []
        self.app.bus.subscribe(self.events.append)
        self.app.start(initial_scan=False)
        self.app.start_wifi_dongle(0, passthrough)  # port 0: any free port
        self.port = self.app.wifi.port

    def close(self):
        self.app.close()

    def telescope_received(self):
        return [p.frame for chunk in self.mount.written for p in FrameParser().feed(chunk)]


class AppClient:
    """An app (like CPWI) connected to the dongle."""

    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        self.parser = FrameParser()
        self.received = []

    def send(self, packet):
        self.sock.sendall(packet.encode())

    def wait_for(self, condition, timeout=3.0):
        deadline = time.monotonic() + timeout
        self.sock.settimeout(0.1)
        while time.monotonic() < deadline:
            if any(condition(p) for p in self.received):
                return True
            try:
                data = self.sock.recv(4096)
            except socket.timeout:
                continue
            if not data:
                break
            self.received += [p.frame for p in self.parser.feed(data)]
        return any(condition(p) for p in self.received)

    def close(self):
        self.sock.close()


@pytest.fixture
def setup(request):
    made = []

    def make(passthrough, **config):
        env = App(passthrough, **config)
        client = AppClient(env.port)
        made.append((env, client))
        assert wait_until(lambda: env.app.wifi.snapshot()["clients"])
        return env, client

    yield make
    for env, client in made:
        client.close()
        env.close()


def test_without_pass_through_apps_only_reach_the_emulated_devices(setup):
    env, client = setup(passthrough=False)
    env.app.enable_gps_emulation(51.4779, -0.0015)
    client.send(AuxPacket(0x20, 0xB0, 0xFE))  # CPWI asks the GPS for its version
    assert client.wait_for(lambda p: p.source == 0xB0 and p.data == bytes((11, 1)))
    client.send(AuxPacket(0x20, 0x10, 0xFE))  # and the real motor: no answer, nothing sent to it
    time.sleep(0.4)
    assert not any(p.source == 0x10 for p in client.received)
    assert env.telescope_received() == []  # nothing reached the telescope at all
    origins = {e.origin for e in env.events}
    assert ORIGIN_PREFIX + client.sock.getsockname()[0] + ":" + str(client.sock.getsockname()[1]) in origins
    assert "emulated" in origins  # the GPS reply went to the app only


def test_with_pass_through_apps_talk_to_the_telescope(setup):
    env, client = setup(passthrough=True)
    client.send(AuxPacket(0x20, 0x10, 0xFE))
    assert client.wait_for(lambda p: p.source == 0x10 and p.data == b"\x07\x0b")  # the motor's real reply
    assert any(p.destination == 0x10 and p.source == 0x20 for p in env.telescope_received())
    assert not any(p.source == 0x20 for p in client.received)  # its own packet is not echoed back


def test_with_pass_through_emulated_devices_answer_too(setup):
    env, client = setup(passthrough=True)
    env.app.enable_emulation("focuser")
    client.send(AuxPacket(0x20, 0x12, 0x01))
    assert client.wait_for(lambda p: p.source == 0x12 and p.command == 0x01 and len(p.data) == 3)


def test_the_dongle_answers_as_a_skyportal_wifi_module(setup):
    env, client = setup(passthrough=False)
    client.send(AuxPacket(0x20, 0xB3, 0xFE))
    assert client.wait_for(lambda p: p.source == 0xB3 and p.data == bytes.fromhex("030123a0"))  # 3.1.9120
    assert (0xB3, "3.1.9120 (emulated)") in env.app.devices()
    env.app.stop_wifi_dongle()
    assert not env.app.registry.contains(0xB3)


def test_listen_only_keeps_app_commands_off_the_telescope(setup):
    env, client = setup(passthrough=True, listen_only=True)
    client.send(AuxPacket(0x20, 0x10, 0xFE))
    assert wait_until(lambda: any(e.origin and e.origin.startswith(ORIGIN_PREFIX) for e in env.events))  # logged
    time.sleep(0.3)
    assert env.telescope_received() == []


def test_a_busy_port_is_reported():
    holder = socket.socket()
    holder.bind(("127.0.0.1", 0))
    holder.listen()
    try:
        app = ScannerApp(FakeMount({}), AppConfig(save_starsense_images=False), stream=io.StringIO())
        bridge = WifiBridge(app.bus, address_finder=lambda: None)
        with pytest.raises(OSError, match="could not listen"):
            bridge.start(holder.getsockname()[1])
        assert not bridge.running
    finally:
        holder.close()


def test_it_listens_on_loopback_even_if_the_main_address_cannot_be_used():
    app = ScannerApp(FakeMount({}), AppConfig(save_starsense_images=False), stream=io.StringIO())
    bridge = WifiBridge(app.bus, address_finder=lambda: "203.0.113.7")  # an address this computer does not have
    try:
        listening = bridge.start(0)
        assert len(listening) == 1 and listening[0].startswith("127.0.0.1:")
    finally:
        bridge.stop()


def test_the_main_address_is_found_or_none():
    address = main_ip_address()
    assert address is None or (address.count(".") == 3 and not address.startswith("127."))


def test_the_port_is_free_again_as_soon_as_the_dongle_stops():
    app = ScannerApp(FakeMount({}), AppConfig(save_starsense_images=False), stream=io.StringIO())
    bridge = WifiBridge(app.bus, address_finder=lambda: None)
    port = int(bridge.start(0)[0].rsplit(":", 1)[1])
    bridge.stop()
    again = WifiBridge(app.bus, address_finder=lambda: None)
    try:
        assert again.start(port) == [f"127.0.0.1:{port}"]  # no "address already in use"
    finally:
        again.stop()


def listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    sock.settimeout(3)
    return sock


def test_it_announces_itself_like_a_skyportal_module():
    from celestron_aux.wifi_bridge import ANNOUNCE_TO, ANNOUNCEMENT

    assert ANNOUNCEMENT == b'{"AMW007"}' and ANNOUNCE_TO == ("255.255.255.255", 55555)
    app = ScannerApp(FakeMount({}), AppConfig(save_starsense_images=False), stream=io.StringIO())
    ear = listener()
    bridge = WifiBridge(app.bus, address_finder=lambda: None, announce_to=ear.getsockname())
    try:
        bridge.start(0)
        payload, sender = ear.recvfrom(64)
        assert payload == b'{"AMW007"}'
        payload, _ = ear.recvfrom(64)  # and again a second later
        assert payload == b'{"AMW007"}'
        assert wait_until(lambda: bridge.announcements >= 2)  # counted just after each send
    finally:
        bridge.stop()
        ear.close()


def test_announcing_can_be_switched_off():
    app = ScannerApp(FakeMount({}), AppConfig(save_starsense_images=False), stream=io.StringIO())
    ear = listener()
    ear.settimeout(1.5)
    bridge = WifiBridge(app.bus, address_finder=lambda: None, announce_to=ear.getsockname())
    bridge.announce = False
    try:
        bridge.start(0)
        with pytest.raises(socket.timeout):
            ear.recvfrom(64)
        assert bridge.announcements == 0
    finally:
        bridge.stop()
        ear.close()


def test_pass_through_forwards_a_burst_without_the_scanners_pacing():
    """CPWI sends from several tasks at once and waits about 220 ms for each reply; a real
    module forwards immediately. With the scanner's 150 ms pacing applied to pass-through,
    a burst queued up and CPWI reported "No response received" and lost the mount."""
    mount = FakeMount({0x10: b"\x05\x12", 0x11: b"\x05\x12", 0x12: b"\x07\x10"},
                      positions={0x10: b"\x01\x02\x03", 0x11: b"\x04\x05\x06"})
    app = ScannerApp(mount, AppConfig(save_starsense_images=False), stream=io.StringIO())
    assert app.bus._inter_frame_delay == 0.15  # the scanner's normal pacing, left in place
    app.wifi._find_address = lambda: None
    app.start(initial_scan=False)
    try:
        app.start_wifi_dongle(0, passthrough=True)
        client = AppClient(app.wifi.port)
        requests = [AuxPacket(0x20, device, command) for _ in range(4)
                    for device, command in ((0x10, 0x01), (0x11, 0x01), (0x12, 0xFE), (0x10, 0xFE), (0x11, 0xFE))]
        started = time.monotonic()
        client.sock.sendall(b"".join(r.encode() for r in requests))  # a burst, as CPWI's tasks produce
        client.wait_for(lambda p: sum(1 for q in client.received if q.destination == 0x20) == len(requests),
                        timeout=0.6)
        replies = [p for p in client.received if p.destination == 0x20]
        elapsed = time.monotonic() - started
        assert len(replies) == len(requests), f"{len(replies)} of {len(requests)} replies within {elapsed:.2f} s"
        client.close()
    finally:
        app.close()


def test_the_scanners_own_sends_are_still_paced_but_never_block_other_traffic():
    import threading

    app = ScannerApp(FakeMount({}), AppConfig(save_starsense_images=False), stream=io.StringIO())
    app.start(initial_scan=False)
    try:
        started = time.monotonic()
        app.bus.send(AuxPacket(0x2F, 0x10, 0xFE))  # a scan probe: paced
        assert time.monotonic() - started >= 0.1  # 0.15 s pause; Windows sleeps can return ~15 ms early
        relay_done = []
        scan = threading.Thread(target=app.bus.send, args=(AuxPacket(0x2F, 0x11, 0xFE),))
        scan.start()
        time.sleep(0.02)  # the scanner is now waiting out its pacing
        started = time.monotonic()
        app.bus.send(AuxPacket(0x20, 0x10, 0x01), pace=False)  # relayed for a Wi-Fi app
        relay_done.append(time.monotonic() - started)
        scan.join()
        assert relay_done[0] < 0.05  # not held up by the scanner's pause
    finally:
        app.close()
