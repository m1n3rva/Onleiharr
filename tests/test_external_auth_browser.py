from __future__ import annotations

import http.server
import socketserver
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

import pytest

from onleiharr._vendor.onleihe import OnleiheAuthError, OnleiheClient, SessionState
from onleiharr.auth import _run_external_login_browser
from onleiharr.external_auth import munich_login_handler

MUNCHEN_FORM_HTML = """\
<html>
<head><title>Einloggen</title></head>
<body>
<form method="post" action="/login">
  <input name="L#AUSW" type="text" />
  <input name="LPASSW" type="password" />
  <input name="LLOGIN" type="submit" value="Einloggen" />
</form>
</body>
</html>
"""


class _RedirectHandler(http.server.BaseHTTPRequestHandler):
    """Serves the Munich form on GET and redirects to the Onleihe callback on POST."""

    redirect_url: str = ""
    code: str = ""
    state: str = ""

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(MUNCHEN_FORM_HTML.encode("utf-8"))

    def do_POST(self) -> None:
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length) if content_length else b""
        _ = body.decode("utf-8", errors="replace")
        params = urlencode({"code": self.code, "state": self.state})
        self.send_response(302)
        self.send_header("Location", f"{self.redirect_url}?{params}")
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"<html><body>Redirecting...</body></html>")

    def log_message(self, format: str, *args: object) -> None:
        pass


class _ThreadingTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _LocalServer:
    """A minimal HTTP server that serves the Munich form and redirects after POST."""

    def __init__(self, *, redirect_url: str, code: str, state: str) -> None:
        self.redirect_url = redirect_url
        self.code = code
        self.state = state
        self.server: _ThreadingTCPServer | None = None
        self.port: int | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        _RedirectHandler.redirect_url = self.redirect_url
        _RedirectHandler.code = self.code
        _RedirectHandler.state = self.state
        self.server = _ThreadingTCPServer(("127.0.0.1", 0), _RedirectHandler)
        self.port = self.server.server_address[1]
        self._thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self._thread.start()
        time.sleep(0.1)

    def stop(self) -> None:
        if self.server:
            self.server.shutdown()
            self.server.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    @property
    def form_url(self) -> str:
        assert self.port is not None
        return f"http://127.0.0.1:{self.port}"


class _FakeClient(OnleiheClient):
    """Minimal fake Onleihe client for testing code exchange."""

    def __init__(self, form_url: str = "") -> None:
        self.host = "muenchen.onleihe.de"
        self.form_url = form_url
        self._logged_in = False
        self._received_code: str | None = None
        self._received_redirect_url: str | None = None

    def login_open_id(self, code: str, *, redirect_url: str) -> SessionState:
        self._logged_in = True
        self._received_code = code
        self._received_redirect_url = redirect_url
        return SessionState(
            access_token="test-token",
            refresh_token="test-refresh",
            user_id="test-user",
            profile_id="master",
            library_id="test-library",
            onleihe_id="test-onleihe",
        )

    def refresh(self) -> SessionState:
        self._logged_in = True
        return SessionState(
            access_token="refreshed-token",
            refresh_token="test-refresh",
            user_id="test-user",
            profile_id="master",
            library_id="test-library",
            onleihe_id="test-onleihe",
        )

    def get_login_method(self) -> dict:
        return {"loginType": "OPEN_ID"}

    def build_open_id_authorization_url(
        self,
        method: dict,
        *,
        redirect_url: str,
        state: str,
    ) -> str:
        return f"{self.form_url}?response_type=code&client_id=test&redirect_uri={redirect_url}&state={state}"

    def get_lendings(self, **kwargs) -> list:
        return []

    def get_my_media(self, **kwargs) -> list:
        return []

    def lend(self, **kwargs) -> dict:
        return {}

    def reserve(self, **kwargs) -> dict:
        return {}

    def return_lending(self, **kwargs) -> bool:
        return True

    def download_acsm(self, **kwargs) -> bytes:
        return b""


@pytest.fixture()
def local_server(tmp_path: Path) -> _LocalServer:
    """Provide a local HTTP server that serves the Munich form."""
    server = _LocalServer(
        redirect_url="https://muenchen.onleihe.de",
        code="test-code",
        state="test-state",
    )
    server.start()
    yield server
    server.stop()


@pytest.mark.browser
def test_real_browser_oidc_flow(local_server: _LocalServer) -> None:
    """Test the full OIDC flow: form, POST, redirect interception, state validation, code exchange."""
    client = _FakeClient(form_url=local_server.form_url)

    auth_url = client.build_open_id_authorization_url(
        method={"loginType": "OPEN_ID"},
        redirect_url="https://muenchen.onleihe.de",
        state="test-state",
    )

    session = _run_external_login_browser(
        authorization_url=auth_url,
        redirect_url="https://muenchen.onleihe.de",
        expected_state="test-state",
        headless=True,
        timeout_secs=30.0,
        login_handler=lambda page: munich_login_handler(page, username="testuser", password="testpass"),
        client=client,
        _capture_callback_url="https://muenchen.onleihe.de/?code=test-code&state=test-state",
    )

    assert session.access_token == "test-token"
    assert client._logged_in is True
    assert client._received_code == "test-code"
    assert client._received_redirect_url == "https://muenchen.onleihe.de"


@pytest.mark.browser
def test_real_browser_wrong_state_raises(local_server: _LocalServer) -> None:
    """A wrong expected state should raise OnleiheAuthError."""
    client = _FakeClient(form_url=local_server.form_url)

    auth_url = client.build_open_id_authorization_url(
        method={"loginType": "OPEN_ID"},
        redirect_url="https://muenchen.onleihe.de",
        state="test-state",
    )

    with pytest.raises(OnleiheAuthError, match="state"):
        _run_external_login_browser(
            authorization_url=auth_url,
            redirect_url="https://muenchen.onleihe.de",
            expected_state="wrong-state",
            headless=True,
            timeout_secs=30.0,
            login_handler=lambda page: munich_login_handler(page, username="testuser", password="testpass"),
            client=client,
            _capture_callback_url="https://muenchen.onleihe.de/?code=*",
        )


@pytest.mark.browser
def test_real_browser_unknown_form_raises() -> None:
    """A form without the expected Munich selectors should raise OnleiheAuthError."""
    unknown_form_html = b'<html><form><input name="username" /><input name="password" /><input type="submit" /></form></html>'

    class _UnknownFormHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(unknown_form_html)

        def do_POST(self) -> None:
            cl = int(self.headers.get("Content-Length", 0))
            self.rfile.read(cl) if cl else None
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b'<html><body>Unknown form</body></html>')

        def log_message(self, format: str, *args: object) -> None:
            pass

    server = _ThreadingTCPServer(("127.0.0.1", 0), _UnknownFormHandler)
    port = server.server_address[1]
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    time.sleep(0.1)

    try:
        client = _FakeClient(form_url=f"http://127.0.0.1:{port}")

        auth_url = client.build_open_id_authorization_url(
            method={"loginType": "OPEN_ID"},
            redirect_url="https://muenchen.onleihe.de",
            state="test-state",
        )

        with pytest.raises(OnleiheAuthError, match="form"):
            _run_external_login_browser(
                authorization_url=auth_url,
                redirect_url="https://muenchen.onleihe.de",
                expected_state="test-state",
                headless=True,
                timeout_secs=15.0,
                login_handler=lambda page: munich_login_handler(page, username="testuser", password="testpass"),
                client=client,
            )
    finally:
        server.shutdown()
        server.server_close()
        t.join(timeout=5)
