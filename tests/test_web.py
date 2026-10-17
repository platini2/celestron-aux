import json
import socket
import urllib.error
import urllib.request

import pytest

from celestron_aux.app import AppConfig
from celestron_aux.cli import parse_args
from celestron_aux.web import WebServer, WebSession, parse_listen_address

from .fakes import FakeMount, FakeTcpMount, wait_until

HEADERS = {"Content-Type": "application/json", "X-Requested-With": "celestron-aux"}


@pytest.fixture
def mount():
    fake = FakeTcpMount({0x10: b"\x07\x0b", 0x11: b"\x07\x0b"}, model=b"\x14\x85")
    yield fake
    fake.close()


@pytest.fixture
def web(tmp_path):
    """A running web interface that is not connected to anything yet."""
    config = AppConfig(image_dir=tmp_path, csv_path=tmp_path / "log.csv", raw_path=tmp_path / "raw.txt",
                       keepalive=False)
    session = WebSession(config)
    server = WebServer(session, "127.0.0.1", 0)
    server.start_in_background()
    host, port = server.address
    try:
        yield session, f"http://{host}:{port}", tmp_path
    finally:
        server.close()
        session.close()


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=5) as response:
        return response.status, response.read()


def post(base, path, body=None, headers=HEADERS):
    request = urllib.request.Request(base + path, data=json.dumps(body or {}).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def state(base):
    return json.loads(get(base, "/api/state")[1])


def connect(base, mount, scan=True):
    return post(base, "/api/connect", {"mode": "wifi", "target": "127.0.0.1", "tcp_port": mount.port, "scan": scan})


def speed_up(session):
    app = session.app
    app.bus._inter_frame_delay = 0
    app.scanner._reply_wait = app.scanner._mount_wait = 0.05


def read_events(base, count):
    items = []
    with urllib.request.urlopen(base + "/api/events", timeout=5) as stream:
        while len(items) < count:
            line = stream.readline().decode()
            if line.startswith("data: "):
                items.append(json.loads(line[6:]))
    return items


# --- command line -------------------------------------------------------------

def test_web_without_mode_is_allowed_and_console_mode_needs_one():
    assert parse_args(["--web"]).mode is None
    assert parse_args(["wifi", "1.2.3.4", "--web", "9000"]).listen == ("127.0.0.1", 9000)
    with pytest.raises(SystemExit):
        parse_args([])
    with pytest.raises(SystemExit):
        parse_args(["wifi"])


def test_listen_address_parsing():
    assert parse_listen_address("8080") == ("127.0.0.1", 8080)
    assert parse_listen_address("0.0.0.0:9000") == ("0.0.0.0", 9000)
    assert parse_listen_address("[::1]:8080") == ("::1", 8080)
    with pytest.raises(ValueError):
        parse_listen_address("host:notaport")


# --- before connecting ----------------------------------------------------------

def test_starts_disconnected_and_mount_actions_are_refused(web):
    _, base, _ = web
    current = state(base)
    assert current["session"] is None and current["connected"] is False and current["devices"] == []
    for path in ("/api/scan", "/api/identify", "/api/starsense/capture", "/api/clock/reset"):
        status, body = post(base, path, {})
        assert status == 409 and "Connect" in body["error"]
    assert post(base, "/api/send", {"device": 16, "command": 5})[0] == 409
    assert post(base, "/api/disconnect")[0] == 409


def test_serial_ports_are_listed(web):
    _, base, _ = web
    status, body = get(base, "/api/ports")
    assert status == 200 and isinstance(json.loads(body)["ports"], list)


def test_connect_validation_and_failure(web):
    session, base, _ = web
    assert post(base, "/api/connect", {"mode": "telnet", "target": "x"})[0] == 400
    assert post(base, "/api/connect", {"mode": "wifi", "target": ""})[0] == 400
    with socket.socket() as probe:  # find a port nobody listens on
        probe.bind(("127.0.0.1", 0))
        closed_port = probe.getsockname()[1]
    status, body = post(base, "/api/connect", {"mode": "wifi", "target": "127.0.0.1", "tcp_port": closed_port})
    assert status == 502 and "Could not connect" in body["error"]
    assert state(base)["session"] is None


def test_settings_and_gps_made_before_connecting_apply_on_connect(web, mount):
    session, base, tmp_path = web
    assert post(base, "/api/settings", {"csv": True, "raw": True})[0] == 200
    assert post(base, "/api/gps", {"enabled": True, "latitude": -34.6, "longitude": -58.4})[0] == 200
    assert connect(base, mount, scan=False)[0] == 200
    assert session.app.gps.position == (-34.6, -58.4)
    assert session.app.csv_log is session.csv_log
    post(base, "/api/send", {"device": 16, "command": 0xFE})
    assert wait_until(lambda: "MTR_GET_VERSION" in (tmp_path / "log.csv").read_text())
    assert (tmp_path / "raw.txt").read_text().strip()


# --- connected --------------------------------------------------------------------

def test_connect_scan_disconnect_and_reconnect_without_losing_logs(web, mount):
    session, base, tmp_path = web
    post(base, "/api/settings", {"csv": True})
    status, body = connect(base, mount, scan=False)
    assert status == 200 and state(base)["connected"] is True
    speed_up(session)
    assert connect(base, mount)[0] == 409  # already connected

    assert post(base, "/api/scan", {"full": False})[0] == 200
    assert wait_until(lambda: {d["address"] for d in state(base)["devices"]} == {0x10, 0x11})
    assert wait_until(lambda: state(base)["mount"] == "AVX")
    assert wait_until(lambda: not state(base)["scanning"])

    assert post(base, "/api/disconnect")[0] == 200
    assert state(base)["session"] is None
    rows_after_first = (tmp_path / "log.csv").read_text().count("\n")

    assert connect(base, mount, scan=False)[0] == 200
    speed_up(session)
    post(base, "/api/send", {"device": 17, "command": 0xFE})
    assert wait_until(lambda: (tmp_path / "log.csv").read_text().count("\n") > rows_after_first)
    assert (tmp_path / "log.csv").read_text().count("timestamp,sender") == 1  # never truncated


def test_event_stream_has_connection_notes_and_traffic(web, mount):
    session, base, _ = web
    connect(base, mount, scan=False)
    speed_up(session)
    post(base, "/api/send", {"device": 16, "command": 0xFE})
    assert wait_until(lambda: session.app.devices() == [(0x10, "7.11")])
    items = read_events(base, 4)
    notes = [item["text"] for item in items if item["kind"] == "note"]
    assert notes[0].startswith("Connecting: wifi 127.0.0.1") and notes[1].startswith("Connected")
    assert any(item.get("command") == "MTR_GET_VERSION" for item in items)


def test_star_lists_reach_the_page(web):
    session, base, _ = web
    session.connect_transport(FakeMount({}), mode="wifi", scan=False)
    assert post(base, "/api/starsense/sample")[0] == 200
    assert wait_until(lambda: state(base)["stars"] is not None)
    assert len(state(base)["stars"]["stars"]) == 99


def test_post_requires_the_page_header_and_unknown_routes_404(web):
    _, base, _ = web
    assert post(base, "/api/settings", {"csv": True}, headers={"Content-Type": "application/json"})[0] == 403
    assert post(base, "/api/nope")[0] == 404
    with pytest.raises(urllib.error.HTTPError) as error:
        get(base, "/nope")
    assert error.value.code == 404


def test_page_is_served(web):
    _, base, _ = web
    status, body = get(base, "/")
    assert status == 200 and b"Connection" in body and b"/api/connect" in body
