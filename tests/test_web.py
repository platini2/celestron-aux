import io
import json
import urllib.error
import urllib.request

import pytest

from celestron_aux.app import AppConfig, ScannerApp
from celestron_aux.web import WebServer, parse_listen_address

from .fakes import FakeMount, wait_until

HEADERS = {"Content-Type": "application/json", "X-Requested-With": "celestron-aux"}


@pytest.fixture
def running(tmp_path):
    mount = FakeMount({0x10: b"\x07\x0b", 0x11: b"\x07\x0b"}, model=b"\x14\x85")
    out = io.StringIO()
    app = ScannerApp(mount, AppConfig(quiet=True, image_dir=tmp_path, csv_path=tmp_path / "log.csv"), stream=out)
    app.bus._inter_frame_delay = 0
    app.scanner._reply_wait = app.scanner._mount_wait = 0.05
    web = WebServer(app, "127.0.0.1", 0)
    app.start(initial_scan=False)
    web.start_in_background()
    host, port = web.address
    try:
        yield app, f"http://{host}:{port}", out, tmp_path
    finally:
        web.close()
        app.close()


def get(base, path):
    with urllib.request.urlopen(base + path, timeout=5) as response:
        return response.status, response.read()


def post(base, path, body=None, headers=HEADERS):
    request = urllib.request.Request(base + path, data=json.dumps(body or {}).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def state(base):
    return json.loads(get(base, "/api/state")[1])


def test_listen_address_parsing():
    assert parse_listen_address("8080") == ("127.0.0.1", 8080)
    assert parse_listen_address("0.0.0.0:9000") == ("0.0.0.0", 9000)
    assert parse_listen_address("[::1]:8080") == ("::1", 8080)
    with pytest.raises(ValueError):
        parse_listen_address("host:notaport")


def test_page_is_served(running):
    _, base, _, _ = running
    status, body = get(base, "/")
    assert status == 200 and b"Celestron AUX" in body and b"/api/events" in body


def test_scan_via_api_finds_devices_and_console_stays_silent(running):
    app, base, out, _ = running
    assert post(base, "/api/scan", {"full": False})[0] == 200
    assert wait_until(lambda: {d["address"] for d in state(base)["devices"]} == {0x10, 0x11})
    assert wait_until(lambda: state(base)["mount"] == "AVX")
    assert wait_until(lambda: not state(base)["scanning"])
    assert out.getvalue() == ""  # quiet mode: nothing on the console


def test_post_requires_the_page_header(running):
    _, base, _, _ = running
    status, body = post(base, "/api/scan", {}, headers={"Content-Type": "application/json"})
    assert status == 403


def test_settings_toggle_and_validation(running):
    app, base, _, tmp_path = running
    assert post(base, "/api/settings", {"csv": True, "keepalive": True})[0] == 200
    current = state(base)["settings"]
    assert current["csv"] and current["keepalive"]
    assert (tmp_path / "log.csv").exists()
    assert post(base, "/api/settings", {"csv": "yes"})[0] == 400
    assert post(base, "/api/settings", {})[0] == 400


def test_gps_emulation_round_trip(running):
    _, base, _, _ = running
    assert post(base, "/api/gps", {"enabled": True, "latitude": 95, "longitude": 0})[0] == 400
    assert post(base, "/api/gps", {"enabled": True, "latitude": -34.6, "longitude": -58.4})[0] == 200
    gps = state(base)["gps"]
    assert gps == {"enabled": True, "latitude": -34.6, "longitude": -58.4}
    assert post(base, "/api/gps", {"enabled": False})[0] == 200
    assert state(base)["gps"]["enabled"] is False


def test_send_command_and_commands_list(running):
    app, base, _, _ = running
    status, body = get(base, "/api/commands?device=0x10")
    names = {c["name"] for c in json.loads(body)["commands"]}
    assert "MTR_GET_MODEL" in names
    assert post(base, "/api/send", {"device": 16, "command": 5, "payload": ""})[0] == 200
    assert wait_until(lambda: app.registry.mount_model == 0x1485)
    assert post(base, "/api/send", {"device": 16, "command": 5, "payload": "zz"})[0] == 400
    assert post(base, "/api/send", {"device": 300, "command": 5})[0] == 400


def test_event_stream_delivers_traffic_and_star_lists(running):
    app, base, _, _ = running
    post(base, "/api/starsense/sample")
    with urllib.request.urlopen(base + "/api/events", timeout=5) as stream:
        items = []
        while len(items) < 1:
            line = stream.readline().decode()
            if line.startswith("data: "):
                items.append(json.loads(line[6:]))
    assert items[0]["kind"] == "stars" and len(items[0]["stars"]) == 99
    assert state(base)["stars"]["seq"] == items[0]["seq"]


def test_unknown_routes_are_404(running):
    _, base, _, _ = running
    with pytest.raises(urllib.error.HTTPError) as error:
        get(base, "/nope")
    assert error.value.code == 404
    assert post(base, "/api/nope")[0] == 404
