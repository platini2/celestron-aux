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


def test_starsense_capture_needs_a_detected_camera(web):
    session, base, _ = web
    mount = FakeMount({0xB4: b"\x01\x02"})
    session.connect_transport(mount, mode="wifi", scan=False)
    session.app.bus._inter_frame_delay = 0
    status, reply = post(base, "/api/starsense/capture")
    assert status == 409 and "No StarSense camera" in reply["error"]
    post(base, "/api/send", {"device": 0xB4, "command": 0xFE})
    assert wait_until(lambda: any(d["address"] == 0xB4 for d in state(base)["devices"]))
    assert post(base, "/api/starsense/capture")[0] == 200


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


def test_state_reports_version_and_copyright(web):
    from celestron_aux import __copyright__, __version__

    _, base, _ = web
    current = state(base)
    assert current["version"] == __version__ and current["copyright"] == __copyright__
    assert "Patricio Latini" in __copyright__ and "2021" in __copyright__


def test_page_is_served(web):
    _, base, _ = web
    status, body = get(base, "/")
    assert status == 200 and b"Connection" in body and b"/api/connect" in body


# --- listen-only, downloads, replay, unknown traffic ------------------------------

def test_listen_only_refuses_transmitting_actions(web, mount):
    session, base, _ = web
    assert post(base, "/api/settings", {"listen_only": True})[0] == 200
    assert connect(base, mount)[0] == 200
    assert state(base)["settings"]["listen_only"] is True
    for path, body in (("/api/scan", {}), ("/api/identify", {}), ("/api/send", {"device": 16, "command": 254}),
                       ("/api/starsense/capture", {}), ("/api/starsense/sample", {})):
        status, reply = post(base, path, body)
        assert status == 409 and "Listen-only" in reply["error"], path
    assert session.app.devices() == []  # the scan on connect was skipped
    assert post(base, "/api/settings", {"listen_only": False})[0] == 200
    assert post(base, "/api/send", {"device": 16, "command": 254})[0] == 200


def test_downloads(web, mount):
    session, base, tmp_path = web
    with pytest.raises(urllib.error.HTTPError) as missing:
        get(base, "/api/download/csv")
    assert missing.value.code == 404
    post(base, "/api/settings", {"csv": True, "raw": True})
    connect(base, mount, scan=False)
    speed_up(session)
    post(base, "/api/send", {"device": 16, "command": 254})
    assert wait_until(lambda: "MTR_GET_VERSION" in (tmp_path / "log.csv").read_text())
    with urllib.request.urlopen(base + "/api/download/csv", timeout=5) as response:
        assert 'filename="log.csv"' in response.headers["Content-Disposition"]
        assert b"MTR_GET_VERSION" in response.read()
    assert get(base, "/api/download/raw")[1].strip()
    assert get(base, "/api/download/unknown")[1].startswith(b"device_id,device,command_id")
    with pytest.raises(urllib.error.HTTPError):
        get(base, "/api/download/../../etc/passwd")


def upload(base, path, data):
    request = urllib.request.Request(base + path, data=data, method="POST",
                                     headers={"Content-Type": "text/plain", "X-Requested-With": "celestron-aux"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def test_replay_an_uploaded_capture(web):
    from celestron_aux.protocol import AuxPacket

    session, base, _ = web
    lines = [
        f"0.0 {AuxPacket(0x10, 0x2F, 0xFE, bytes((7, 11))).encode().hex()}",
        f"0.01 {AuxPacket(0x0D, 0x10, 0x77, bytes((1,))).encode().hex()}",
    ]  # replayed 1000x faster, but shown with the recorded times
    status, reply = upload(base, "/api/replay?name=night1.txt&speed=1000", "\n".join(lines).encode())
    assert status == 200 and "night1.txt" in reply["message"]
    current = state(base)
    assert current["session"]["mode"] == "playback" and "night1.txt" in current["session"]["connection"]
    assert wait_until(lambda: state(base)["session"]["open"] is False)  # replay finished
    assert wait_until(lambda: state(base)["devices"] == [{"address": 16, "name": "AZM MC", "version": "7.11"}])
    assert state(base)["unknown_count"] == 1
    items = json.loads(get(base, "/api/discoveries")[1])["items"]
    assert items[0]["command"] == 0x77 and items[0]["requests"] == ["01"]
    assert items[0]["last_seen"] == 0.01  # the time in the recording, not the replay
    times = [item["t"] for item in read_events(base, 4) if item["kind"] == "packet"]
    assert times == [0.0, 0.01]
    replay_file = session._replay_file
    assert replay_file is not None and replay_file.exists()
    assert post(base, "/api/disconnect")[0] == 200
    assert not replay_file.exists()  # the uploaded copy is cleaned up
    assert post(base, "/api/discoveries/clear")[0] == 200 and state(base)["unknown_count"] == 0


def test_replay_rejects_bad_files(web):
    _, base, _ = web
    assert upload(base, "/api/replay?name=x", b"")[0] == 400
    status, reply = upload(base, "/api/replay?name=x", b"this is not hex\n")
    assert status == 400 and "not a raw capture" in reply["error"]
    assert upload(base, "/api/replay?name=x&speed=0", b"3b032f10fec0")[0] == 400
    assert state(base)["session"] is None


def test_replay_requires_the_page_header(web):
    _, base, _ = web
    request = urllib.request.Request(base + "/api/replay", data=b"3b032f10fec0", method="POST")
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request, timeout=5)
    assert error.value.code == 403


def test_motion_endpoints(web):
    session, base, _ = web
    mount = FakeMount({0x10: b"\x07\x0b"}, positions={0x10: bytes.fromhex("400000")})
    session.connect_transport(mount, mode="wifi", scan=False)
    session.app.bus._inter_frame_delay = 0
    status, reply = post(base, "/api/motion/move", {"axis": "azm", "direction": 1, "rate": 5})
    assert status == 503 and "Mount control is off" in reply["error"]
    assert post(base, "/api/settings", {"motion_enabled": True})[0] == 200
    assert wait_until(lambda: state(base)["motion"]["axes"]["azm"]["degrees"] == 90.0)
    assert post(base, "/api/motion/move", {"axis": "azm", "direction": 1, "rate": 5})[0] == 200
    assert state(base)["motion"]["axes"]["azm"]["direction"] == 1
    assert post(base, "/api/motion/stop", {"axis": "azm"})[0] == 200
    assert state(base)["motion"]["axes"]["azm"]["direction"] == 0
    assert post(base, "/api/motion/move", {"axis": "sideways", "direction": 1, "rate": 5})[0] == 400
    assert post(base, "/api/motion/move", {"axis": "alt", "direction": 0, "rate": 5})[0] == 400
    assert post(base, "/api/motion/move", {"axis": "alt", "direction": 1, "rate": 12})[0] == 400
    assert post(base, "/api/settings", {"listen_only": True})[0] == 200
    status, reply = post(base, "/api/motion/move", {"axis": "alt", "direction": 1, "rate": 5})
    assert status == 409 and "Listen-only" in reply["error"]


def test_gps_and_focuser_panels_follow_detection_and_their_switches(web):
    session, base, _ = web
    mount = FakeMount({0x12: b"\x07\x03"}, replies={(0x12, 0x01): (777).to_bytes(3, "big")})
    session.connect_transport(mount, mode="wifi", scan=False)
    session.app.bus._inter_frame_delay = 0
    current = state(base)
    assert current["gps_status"]["detected"] is False and current["focuser"]["detected"] is False
    post(base, "/api/send", {"device": 0x12, "command": 0xFE})  # a scan would find it the same way
    assert wait_until(lambda: state(base)["focuser"]["detected"] is True)
    assert state(base)["focuser"]["enabled"] is False
    assert post(base, "/api/settings", {"focuser_polling": True})[0] == 200
    assert wait_until(lambda: state(base)["focuser"]["position"] == 777)
    assert state(base)["settings"]["focuser_polling"] is True


def test_feed_items_carry_decoded_value_and_payload_hex(web):
    from celestron_aux.protocol import AuxPacket

    session, base, _ = web
    mount = FakeMount({})
    session.connect_transport(mount, mode="wifi", scan=False)
    mount.inject(AuxPacket(0x10, 0x0D, 0xFE, bytes((7, 11))).encode())
    mount.inject(AuxPacket(0x10, 0x0D, 0x77, bytes((1, 2))).encode())
    packets = [item for item in read_events(base, 4) if item["kind"] == "packet"]
    assert (packets[0]["value"], packets[0]["payload"]) == ("7.11", "070b")
    assert (packets[1]["value"], packets[1]["payload"]) == ("0102", "0102")  # undecoded: hex in both


def test_marks_from_the_page(web):
    session, base, _ = web
    assert post(base, "/api/mark", {"text": "too early"})[0] == 409  # not connected
    session.connect_transport(FakeMount({}), mode="wifi", scan=False)
    assert post(base, "/api/mark", {"text": "  "})[0] == 400
    status, reply = post(base, "/api/mark", {"text": "pressed Align"})
    assert status == 200 and "pressed Align" in reply["message"]
    marks = [item for item in read_events(base, 3) if item["kind"] == "mark"]
    assert marks and marks[0]["text"] == "pressed Align" and marks[0]["t"] is not None


def test_naming_from_the_page(web, isolated_user_catalog):
    from celestron_aux.protocol import AuxPacket

    session, base, _ = web
    mount = FakeMount({})
    session.connect_transport(mount, mode="wifi", scan=False)
    mount.inject(AuxPacket(0x0D, 0x10, 0x77, b"\x01").encode())
    mount.inject(AuxPacket(0x0D, 0x99, 0x05).encode())
    assert wait_until(lambda: state(base)["unknown_count"] == 2)

    assert post(base, "/api/catalog/name", {"device": 0x10, "command": 0x77})[0] == 400  # no name typed
    assert post(base, "/api/catalog/name", {"device": 0x10, "command": 0x77, "command_name": "x" * 41})[0] == 400
    status, reply = post(base, "/api/catalog/name", {"device": 0x10, "command": 0x77, "command_name": "MTR_GET_TEMP"})
    assert status == 200 and "MTR_GET_TEMP" in reply["message"]
    assert state(base)["unknown_count"] == 1  # that entry is no longer unknown
    assert post(base, "/api/catalog/name", {"device": 0x99, "command": 0x05,
                                            "device_name": "Polar finder", "command_name": "PF_GET_STATE"})[0] == 200
    assert state(base)["unknown_count"] == 0 and state(base)["catalog"]["names"] == 3

    names = json.loads(get(base, "/api/catalog")[1])
    assert names["path"] == str(isolated_user_catalog.path) and len(names["entries"]) == 3
    assert json.loads(isolated_user_catalog.path.read_text())["devices"] == {"0x99": "Polar finder"}

    mount.inject(AuxPacket(0x0D, 0x99, 0x05).encode())  # new traffic now carries the names
    packets = [i for i in read_events(base, 5) if i["kind"] == "packet" and i["receiver_id"] == 0x99]
    assert packets[-1]["receiver"] == "Polar finder" and packets[-1]["command"] == "PF_GET_STATE"

    assert post(base, "/api/catalog/remove", {"device": 0x99, "command": None})[0] == 200
    assert state(base)["catalog"]["names"] == 2


def test_catalog_command_line_option(tmp_path):
    assert parse_args(["--web", "--catalog", str(tmp_path / "mine.json")]).catalog == tmp_path / "mine.json"


def test_listen_only_command_line_flag():
    assert parse_args(["wifi", "1.2.3.4", "--listen-only"]).listen_only is True
