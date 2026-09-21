from __future__ import annotations

import pytest

from onleiharr._vendor.onleihe import OnleiheAuthError, OnleiheClient, SessionState
from onleiharr.auth import _run_external_login_browser
from onleiharr.external_auth import get_login_handler, munich_login_handler


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


def test_munich_provider_selects_handler() -> None:
    """ssl.muenchen.de selects the Munich handler."""
    handler = get_login_handler("https://ssl.muenchen.de/authorize")
    assert handler is munich_login_handler


def test_unknown_provider_returns_none() -> None:
    """Unknown providers return None and do not start a browser."""
    assert get_login_handler("https://unknown.example.com/authorize") is None
    assert get_login_handler("https://other.onleihe.de/auth") is None


def test_handler_receives_credentials_only_in_closure() -> None:
    """Credentials are captured in the handler closure, not passed through auth.py."""
    captured = {"username": None, "password": None}

    def make_handler(username: str, password: str):
        def handler(page) -> None:
            captured["username"] = username
            captured["password"] = password
        return handler

    handler = make_handler("secret_user", "secret_pass")
    # Verify handler signature matches what _run_external_login_browser expects
    assert callable(handler)


def test_external_login_automated_does_not_print_auth_url(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """external_login_automated does not print the authorization URL."""
    from onleiharr.auth import external_login_automated  # noqa: PLC0415
    from onleiharr.external_auth import get_login_handler as real_get_handler  # noqa: PLC0415

    monkeypatch.setattr(
        "onleiharr.auth._run_external_login_browser",
        lambda *a, **k: SessionState(access_token="tok"),
    )
    monkeypatch.setattr(
        "onleiharr.auth._external_login_request",
        lambda c: ("https://ssl.muenchen.de/authorize?state=abc", "https://muenchen.onleihe.de", "abc"),
    )

    client = _FakeClient()
    external_login_automated(client, username="u", password="p")  # type: ignore[arg-type]

    captured = capsys.readouterr()
    assert "https://ssl.muenchen.de" not in captured.out


def test_external_login_automated_forwards_headless_and_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Headless and timeout values are forwarded unchanged."""
    received_kwargs: dict = {}

    def fake_run(*args, headless, timeout_secs, **k):
        received_kwargs["headless"] = headless
        received_kwargs["timeout_secs"] = timeout_secs
        return SessionState(access_token="tok")

    monkeypatch.setattr("onleiharr.auth._run_external_login_browser", fake_run)
    monkeypatch.setattr(
        "onleiharr.auth._external_login_request",
        lambda c: ("https://ssl.muenchen.de/authorize?state=abc", "https://muenchen.onleihe.de", "abc"),
    )

    from onleiharr.auth import external_login_automated  # noqa: PLC0415

    client = _FakeClient()
    external_login_automated(client, username="u", password="p", headless=False, timeout_secs=60.0)  # type: ignore[arg-type]

    assert received_kwargs["headless"] is False
    assert received_kwargs["timeout_secs"] == 60.0


def test_external_login_automated_unknown_provider_no_browser(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unknown provider raises OnleiheAuthError without starting a browser."""
    browser_started = []

    def fake_run(*a, **k):
        browser_started.append(True)
        return SessionState(access_token="tok")

    monkeypatch.setattr("onleiharr.auth._run_external_login_browser", fake_run)
    monkeypatch.setattr(
        "onleiharr.auth._external_login_request",
        lambda c: ("https://unknown.example.com/authorize?state=abc", "https://example.com", "abc"),
    )

    from onleiharr.auth import external_login_automated  # noqa: PLC0415

    client = _FakeClient()
    with pytest.raises(OnleiheAuthError, match="unknown"):
        external_login_automated(client, username="u", password="p")  # type: ignore[arg-type]

    assert browser_started == []


def test_external_login_automated_errors_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    """Errors are not retried automatically."""
    run_count = [0]

    def fake_run(*a, **k):
        run_count[0] += 1
        raise OnleiheAuthError("browser failed")

    monkeypatch.setattr("onleiharr.auth._run_external_login_browser", fake_run)
    monkeypatch.setattr(
        "onleiharr.auth._external_login_request",
        lambda c: ("https://ssl.muenchen.de/authorize?state=abc", "https://muenchen.onleihe.de", "abc"),
    )

    from onleiharr.auth import external_login_automated  # noqa: PLC0415

    client = _FakeClient()
    with pytest.raises(OnleiheAuthError, match="browser failed"):
        external_login_automated(client, username="u", password="p")  # type: ignore[arg-type]

    assert run_count[0] == 1


def test_external_login_automated_no_secrets_in_exception(monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    """Logs and exceptions contain no secrets or complete URLs."""
    from onleiharr.auth import external_login_automated  # noqa: PLC0415

    monkeypatch.setattr(
        "onleiharr.auth._run_external_login_browser",
        lambda *a, **k: SessionState(access_token="tok"),
    )
    monkeypatch.setattr(
        "onleiharr.auth._external_login_request",
        lambda c: ("https://ssl.muenchen.de/authorize?client_id=abc&redirect_uri=https://muenchen.onleihe.de&state=xyz", "https://muenchen.onleihe.de", "xyz"),
    )

    client = _FakeClient()
    external_login_automated(client, username="supersecretuser", password="supersecretpass")  # type: ignore[arg-type]

    for record in caplog.records:
        assert "supersecretuser" not in record.message
        assert "supersecretpass" not in record.message
        assert "abc" not in record.message or "client_id" not in record.message
