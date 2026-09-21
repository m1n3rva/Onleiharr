from __future__ import annotations

import pytest
import stat

from onleiharr._vendor.onleihe import OnleiheAuthError, SessionState
from onleiharr.auth import _run_external_login_browser, external_login_browser, external_login_manual, load_session, save_session


def _make_fake_page(**kwargs):
    """Create a FakePage class with the given overrides."""
    defaults = {
        "__init__": lambda self: setattr(self, "_route_callback", None),
        "goto": lambda self, url: None,
        "wait_for_timeout": lambda self, ms: None,
        "on": lambda self, event, callback: None,
        "route": lambda self, pattern, callback: setattr(self, "_route_callback", callback),
        "url": "about:blank",
    }
    defaults.update(kwargs)

    class FakePage:
        pass

    for name, impl in defaults.items():
        setattr(FakePage, name, impl)

    return FakePage


def _make_fake_browser(fake_page_cls=None):
    """Create a FakeBrowser class."""
    if fake_page_cls is None:
        fake_page_cls = _make_fake_page()

    class FakeBrowser:
        def new_page(self):
            return fake_page_cls()
        def close(self):
            pass

    return FakeBrowser


def _make_fake_chromium(fake_browser_cls=None):
    """Create a FakeChromium class."""
    if fake_browser_cls is None:
        fake_browser_cls = _make_fake_browser()

    class FakeChromium:
        def launch(self, **kwargs):
            return fake_browser_cls()

    return FakeChromium


def _make_fake_playwright(fake_chromium=None):
    """Create a FakePlaywright class."""
    if fake_chromium is None:
        fake_chromium = _make_fake_chromium()

    class FakePlaywright:
        def __init__(self):
            self.chromium = fake_chromium()
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    return FakePlaywright


def test_session_round_trip_uses_private_file(tmp_path):
    path = tmp_path / "session.json"
    session = SessionState(
        access_token="access",
        refresh_token="refresh",
        user_id="user-id",
        profile_id="master",
        library_id="library-id",
        onleihe_id="onleihe-id",
    )

    save_session(path, session)

    assert load_session(path) == session
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_manual_external_login_validates_pasted_callback(monkeypatch):
    class FakeClient:
        host = "muenchen.onleihe.de"

        def get_login_method(self):
            return {"loginType": "OPEN_ID"}

        def build_open_id_authorization_url(self, method, *, redirect_url, state):
            assert redirect_url == "https://muenchen.onleihe.de"
            return f"https://provider.invalid/authorize?state={state}"

        def login_open_id(self, code, *, redirect_url):
            assert code == "authorization-code"
            assert redirect_url == "https://muenchen.onleihe.de"
            return SessionState(access_token="access")

    monkeypatch.setattr("onleiharr.auth.secrets.token_urlsafe", lambda size: "expected-state")
    session = external_login_manual(
        FakeClient(),  # type: ignore[arg-type]
        input_func=lambda prompt: (
            "https://muenchen.onleihe.de/?code=authorization-code&state=expected-state"
        ),
    )
    assert session.access_token == "access"


def test_external_login_browser_wrapper_is_headless_false_without_handler(monkeypatch):
    called_kwargs = {}

    def fake_run(authorization_url, redirect_url, expected_state, headless, timeout_secs, login_handler, *, client=None):
        called_kwargs["headless"] = headless
        called_kwargs["login_handler"] = login_handler
        return SessionState(access_token="tok")

    monkeypatch.setattr("onleiharr.auth._run_external_login_browser", fake_run)
    monkeypatch.setattr("onleiharr.auth._external_login_request", lambda client: ("auth-url", "https://example.com", "state"))

    class FakeClient:
        host = "example.com"

    external_login_browser(FakeClient())  # type: ignore[arg-type]

    assert called_kwargs["headless"] is False
    assert called_kwargs["login_handler"] is None


def test_run_external_login_browser_calls_handler_once_after_goto(monkeypatch):
    handler_calls = []

    def fake_handler(page):
        handler_calls.append(page)

    class FakeRequest:
        def __init__(self):
            self.url = "https://muenchen.onleihe.de/?code=testcode&state=expected-state"

    class FakeRoute:
        def __init__(self):
            self.request = FakeRequest()
        def abort(self):
            pass
        def continue_(self):
            pass

    def fake_goto(self, url):
        # Simulate route interception during navigation
        self._route_callback(FakeRoute())

    fake_page_cls = _make_fake_page(goto=fake_goto)
    fake_browser_cls = _make_fake_browser(fake_page_cls)
    fake_chromium = _make_fake_chromium(fake_browser_cls)
    fake_playwright_cls = _make_fake_playwright(fake_chromium)

    monkeypatch.setattr("onleiharr.auth.sync_playwright", lambda: fake_playwright_cls())
    monkeypatch.setattr(
        "onleiharr.auth._complete_external_login",
        lambda callback_url, redirect_url, expected_state: SessionState(access_token="token"),
    )

    session = _run_external_login_browser(
        authorization_url="https://auth.example.com/login",
        redirect_url="https://example.com",
        expected_state="expected-state",
        headless=True,
        timeout_secs=10.0,
        login_handler=fake_handler,
    )

    assert session.access_token == "token"
    assert len(handler_calls) == 1


def test_run_external_login_browser_processes_callback_and_exchanges_code(monkeypatch):
    callback_received = []

    def fake_handler(page):
        callback_received.append(page)

    class FakeRequest:
        def __init__(self):
            self.url = "https://muenchen.onleihe.de/?code=mycode&state=expected-state"

    class FakeRoute:
        def __init__(self):
            self.request = FakeRequest()
        def abort(self):
            pass
        def continue_(self):
            pass

    def fake_goto(self, url):
        self._route_callback(FakeRoute())

    fake_page_cls = _make_fake_page(goto=fake_goto)
    fake_browser_cls = _make_fake_browser(fake_page_cls)
    fake_chromium = _make_fake_chromium(fake_browser_cls)
    fake_playwright_cls = _make_fake_playwright(fake_chromium)

    exchanged_code = []
    exchanged_redirect = []
    exchanged_state = []

    class FakeClient:
        host = "muenchen.onleihe.de"

        def login_open_id(self, code, *, redirect_url):
            exchanged_code.append(code)
            exchanged_redirect.append(redirect_url)
            return SessionState(access_token="exchanged-token")

    monkeypatch.setattr("onleiharr.auth.sync_playwright", lambda: fake_playwright_cls())

    session = _run_external_login_browser(
        authorization_url="https://auth.example.com/login",
        redirect_url="https://muenchen.onleihe.de",
        expected_state="expected-state",
        headless=True,
        timeout_secs=10.0,
        login_handler=fake_handler,
        client=FakeClient(),
    )

    assert session.access_token == "exchanged-token"
    assert callback_received
    assert exchanged_code == ["mycode"]
    assert exchanged_redirect == ["https://muenchen.onleihe.de"]


def test_run_external_login_browser_timeout_raises(monkeypatch):
    fake_playwright_cls = _make_fake_playwright()

    monkeypatch.setattr("onleiharr.auth.sync_playwright", lambda: fake_playwright_cls())

    with pytest.raises(OnleiheAuthError, match="Timed out"):
        _run_external_login_browser(
            authorization_url="https://auth.example.com/login",
            redirect_url="https://example.com",
            expected_state="expected-state",
            headless=True,
            timeout_secs=0.01,
            login_handler=lambda page: None,
        )


def test_run_external_login_browser_import_error_raises(monkeypatch):
    monkeypatch.setattr("onleiharr.auth.sync_playwright", None)

    with pytest.raises(OnleiheAuthError, match="Playwright"):
        _run_external_login_browser(
            authorization_url="https://auth.example.com/login",
            redirect_url="https://example.com",
            expected_state="expected-state",
            headless=True,
            timeout_secs=10.0,
            login_handler=lambda page: None,
        )


def test_run_external_login_browser_launch_error_raises(monkeypatch):
    class FakeBrowser:
        def new_page(self):
            raise Exception("new_page failed")
        def close(self):
            pass

    class FakeChromium:
        def launch(self, **kwargs):
            raise Exception("launch failed")

    class FakePlaywright:
        def __init__(self):
            self.chromium = FakeChromium()
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    monkeypatch.setattr("onleiharr.auth.sync_playwright", lambda: FakePlaywright())

    with pytest.raises(OnleiheAuthError, match="launch"):
        _run_external_login_browser(
            authorization_url="https://auth.example.com/login",
            redirect_url="https://example.com",
            expected_state="expected-state",
            headless=True,
            timeout_secs=10.0,
            login_handler=lambda page: None,
        )


def test_run_external_login_browser_navigation_error_raises(monkeypatch):
    def fake_goto(self, url):
        raise Exception("navigation failed")

    fake_page_cls = _make_fake_page(goto=fake_goto)
    fake_browser_cls = _make_fake_browser(fake_page_cls)
    fake_chromium = _make_fake_chromium(fake_browser_cls)
    fake_playwright_cls = _make_fake_playwright(fake_chromium)

    monkeypatch.setattr("onleiharr.auth.sync_playwright", lambda: fake_playwright_cls())

    with pytest.raises(OnleiheAuthError, match="navigation"):
        _run_external_login_browser(
            authorization_url="https://auth.example.com/login",
            redirect_url="https://example.com",
            expected_state="expected-state",
            headless=True,
            timeout_secs=10.0,
            login_handler=lambda page: None,
        )


def test_run_external_login_browser_closes_on_handler_failure(monkeypatch):
    closed = []

    class FakePage:
        def goto(self, url):
            pass
        def wait_for_timeout(self, ms):
            pass
        def on(self, event, callback):
            pass
        def route(self, pattern, callback):
            pass

    class FakeBrowser:
        def new_page(self):
            return FakePage()
        def close(self):
            closed.append(True)

    class FakeChromium:
        def launch(self, **kwargs):
            return FakeBrowser()

    class FakePlaywright:
        chromium = FakeChromium()
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass

    monkeypatch.setattr("onleiharr.auth.sync_playwright", lambda: FakePlaywright())

    with pytest.raises(OnleiheAuthError, match="handler"):
        _run_external_login_browser(
            authorization_url="https://auth.example.com/login",
            redirect_url="https://example.com",
            expected_state="expected-state",
            headless=True,
            timeout_secs=10.0,
            login_handler=lambda page: (_ for _ in ()).throw(OnleiheAuthError("handler failed")),
        )

    assert len(closed) == 1


def test_run_external_login_browser_rejects_wrong_host(monkeypatch):
    class FakeRequest:
        def __init__(self):
            self.url = "https://wrong.example.com/?code=testcode&state=expected-state"

    class FakeRoute:
        def __init__(self):
            self.request = FakeRequest()
        def abort(self):
            pass
        def continue_(self):
            pass

    def fake_goto(self, url):
        self._route_callback(FakeRoute())

    fake_page_cls = _make_fake_page(goto=fake_goto)
    fake_browser_cls = _make_fake_browser(fake_page_cls)
    fake_chromium = _make_fake_chromium(fake_browser_cls)
    fake_playwright_cls = _make_fake_playwright(fake_chromium)

    monkeypatch.setattr("onleiharr.auth.sync_playwright", lambda: fake_playwright_cls())

    with pytest.raises(OnleiheAuthError, match="host"):
        _run_external_login_browser(
            authorization_url="https://auth.example.com/login",
            redirect_url="https://example.com",
            expected_state="expected-state",
            headless=True,
            timeout_secs=10.0,
            login_handler=lambda page: None,
        )


def test_run_external_login_browser_rejects_wrong_state(monkeypatch):
    class FakeRequest:
        def __init__(self):
            self.url = "https://muenchen.onleihe.de/?code=testcode&state=wrong-state"

    class FakeRoute:
        def __init__(self):
            self.request = FakeRequest()
        def abort(self):
            pass
        def continue_(self):
            pass

    def fake_goto(self, url):
        self._route_callback(FakeRoute())

    fake_page_cls = _make_fake_page(goto=fake_goto)
    fake_browser_cls = _make_fake_browser(fake_page_cls)
    fake_chromium = _make_fake_chromium(fake_browser_cls)
    fake_playwright_cls = _make_fake_playwright(fake_chromium)

    monkeypatch.setattr("onleiharr.auth.sync_playwright", lambda: fake_playwright_cls())

    with pytest.raises(OnleiheAuthError, match="state"):
        _run_external_login_browser(
            authorization_url="https://auth.example.com/login",
            redirect_url="https://example.com",
            expected_state="expected-state",
            headless=True,
            timeout_secs=10.0,
            login_handler=lambda page: None,
        )
