import base64
import json
import urllib.error
import urllib.request

import pytest

from celestron_aux.app import AppConfig
from celestron_aux.cli import PASSWORD_ENV, parse_args, web_password
from celestron_aux.web import WebServer, WebSession

HEADERS = {"Content-Type": "application/json", "X-Requested-With": "celestron-aux"}


def basic(user, password):
    return "Basic " + base64.b64encode(f"{user}:{password}".encode()).decode()


@pytest.fixture
def protected(tmp_path):
    session = WebSession(AppConfig(image_dir=tmp_path, keepalive=False))
    server = WebServer(session, "127.0.0.1", 0, password="s3cret é")
    server.start_in_background()
    host, port = server.address
    try:
        yield server, f"http://{host}:{port}"
    finally:
        server.close()
        session.close()


def request(url, headers=None, data=None):
    req = urllib.request.Request(url, headers=headers or {}, data=data, method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, response.headers, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers, error.read()


def test_every_route_needs_the_password(protected):
    _, base = protected
    for path in ("/", "/api/state", "/api/events", "/api/download/raw"):
        status, headers, body = request(base + path)
        assert status == 401
        assert headers["WWW-Authenticate"].startswith('Basic realm="AUXBUS Scanner"')
        assert json.loads(body) == {"error": "Password required."}
    status, _, _ = request(base + "/api/scan", HEADERS, b"{}")
    assert status == 401
    status, _, _ = request(base + "/api/state", {"Authorization": basic("me", "wrong")})
    assert status == 401


def test_right_password_with_any_user_name(protected):
    _, base = protected
    for user in ("", "observer"):
        status, _, body = request(base + "/api/state", {"Authorization": basic(user, "s3cret é")})
        assert status == 200 and "connected" in json.loads(body)
    status, _, _ = request(base + "/api/clock/reset", {**HEADERS, "Authorization": basic("x", "s3cret é")}, b"{}")
    assert status == 409  # past the password check: not connected


@pytest.mark.parametrize("header", ["", "Bearer abc", "Basic !!!", "Basic " + base64.b64encode(b"nocolon").decode()])
def test_malformed_credentials_are_refused(protected, header):
    server, _ = protected
    assert server.authorized(header) is False


def test_no_password_means_open(tmp_path):
    session = WebSession(AppConfig(image_dir=tmp_path, keepalive=False))
    server = WebServer(session, "127.0.0.1", 0)
    try:
        assert server.password_protected is False and server.authorized(None) is True
    finally:
        server._httpd.server_close()
        session.close()
    with pytest.raises(ValueError):
        WebServer(session, "127.0.0.1", 0, password="")


def test_password_sources(monkeypatch):
    assert PASSWORD_ENV == "AUXBUS_SCANNER_WEB_PASSWORD"
    monkeypatch.delenv(PASSWORD_ENV, raising=False)
    monkeypatch.setenv("CELESTRON_AUX_WEB_PASSWORD", "old name, not read")
    assert web_password(parse_args(["--web"])) is None
    assert web_password(parse_args(["--web", "--web-password=abc"])) == "abc"
    monkeypatch.setenv(PASSWORD_ENV, "from-env")
    assert web_password(parse_args(["--web"])) == "from-env"
    assert web_password(parse_args(["--web", "--web-password", "given"])) == "given"
    answers = iter(["typed", "typed"])
    assert web_password(parse_args(["--web", "--web-password"]), ask=lambda _: next(answers)) == "typed"
    answers = iter(["one", "two"])
    with pytest.raises(ValueError, match="differ"):
        web_password(parse_args(["--web", "--web-password"]), ask=lambda _: next(answers))
    with pytest.raises(ValueError, match="empty"):
        web_password(parse_args(["--web", "--web-password="]))


def test_password_needs_web():
    with pytest.raises(SystemExit):
        parse_args(["wifi", "1.2.3.4", "--web-password", "abc"])
