from __future__ import annotations

import json
import os
import secrets
import shutil
import tempfile
import time
from dataclasses import asdict
from pathlib import Path
from typing import Callable, Protocol
from urllib.parse import parse_qs, urlparse

from onleiharr._vendor.onleihe import OnleiheAuthError, OnleiheClient, SessionState
from onleiharr.external_auth import get_login_handler


def _load_sync_playwright():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sync_playwright = None  # type: ignore[assignment]
    return sync_playwright


def _find_system_browser() -> str | None:
    for name in ("chromium", "chromium-browser", "google-chrome", "chrome"):
        path = shutil.which(name)
        if path:
            return path
    return None


def _origins_match(url: str, target_scheme: str, target_host: str, target_port: int | None) -> bool:
    try:
        parsed = urlparse(url)
    except Exception:
        return False
    if parsed.scheme.casefold() != target_scheme.casefold():
        return False
    if parsed.hostname is None:
        return False
    if parsed.hostname.casefold() != target_host.casefold():
        return False
    if parsed.hostname.casefold().endswith(target_host.casefold()) and len(parsed.hostname) > len(target_host):
        return False
    effective_port = parsed.port or (443 if parsed.scheme.casefold() == "https" else 80)
    if target_port is None:
        target_port = 443 if target_scheme.casefold() == "https" else 80
    if effective_port != target_port:
        return False
    return True


def _normalize_path(path: str) -> str:
    if not path or path == "/":
        return ""
    return path


def default_session_path(config_path: Path) -> Path:
    return config_path.with_name("session.json")


def load_session(path: Path) -> SessionState:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise OnleiheAuthError(
            f"Could not load external login session from {path}: {exc}"
        ) from exc
    allowed = set(SessionState.__dataclass_fields__)
    return SessionState(**{key: value for key, value in data.items() if key in allowed})


def save_session(path: Path, session: SessionState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(asdict(session), handle, separators=(",", ":"))
        if os.name != "nt":
            temporary_path.chmod(0o600)
        temporary_path.replace(path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _complete_external_login(
    client: OnleiheClient,
    *,
    callback_url: str,
    expected_state: str,
    redirect_url: str,
) -> SessionState:
    parsed = urlparse(callback_url)
    if not _origins_match(callback_url, "https", client.host, None):
        raise OnleiheAuthError(
            "Redirect URL does not belong to the configured Onleihe host"
        )
    query = parse_qs(parsed.query)
    if query.get("state", [None])[0] != expected_state:
        raise OnleiheAuthError("OpenID redirect state does not match")
    code = query.get("code", [None])[0]
    if not code:
        error = query.get(
            "error_description", query.get("error", ["missing authorization code"])
        )[0]
        raise OnleiheAuthError(f"OpenID authorization failed: {error}")
    return client.login_open_id(code, redirect_url=redirect_url)


def _external_login_request(client: OnleiheClient) -> tuple[str, str, str]:
    method = client.get_login_method()
    if method.get("loginType") != "OPEN_ID":
        raise OnleiheAuthError(
            f"Library login type is {method.get('loginType')!r}, not OPEN_ID"
        )
    redirect_url = f"https://{client.host}"
    expected_state = secrets.token_urlsafe(24)
    authorization_url = client.build_open_id_authorization_url(
        method, redirect_url=redirect_url, state=expected_state
    )
    return authorization_url, redirect_url, expected_state


class _PageLike(Protocol):
    def goto(self, url: str) -> None: ...
    def wait_for_timeout(self, ms: int) -> None: ...
    def on(self, event: str, callback: Callable) -> None: ...


def _run_external_login_browser(
    authorization_url: str,
    redirect_url: str,
    expected_state: str,
    headless: bool,
    timeout_secs: float,
    login_handler: Callable[[_PageLike], None] | None = None,
    *,
    client: OnleiheClient | None = None,
) -> SessionState:
    sync_pw = _load_sync_playwright()
    if sync_pw is None:
        raise OnleiheAuthError(
            "External login needs Playwright. Install with: pipx inject onleiharr playwright"
        )

    callback_url: str | None = None
    page_ref: list[_PageLike] = []

    def intercept(route) -> None:
        nonlocal callback_url
        candidate = route.request.url
        parsed = urlparse(candidate)
        if "code" in parse_qs(parsed.query) or "error" in parse_qs(parsed.query):
            if _origins_match(candidate, parsed.scheme, parsed.hostname or "", parsed.port):
                callback_url = candidate
                route.abort()
                return
        route.continue_()

    deadline = time.monotonic() + timeout_secs
    browser = None
    try:
        with sync_pw() as playwright:
            system_browser = _find_system_browser()
            try:
                if system_browser:
                    browser = playwright.chromium.launch(executable_path=system_browser, headless=headless)
                else:
                    browser = playwright.chromium.launch(headless=headless)
            except Exception:
                raise OnleiheAuthError(
                    "Failed to launch browser. Install a supported browser: "
                    "system Chromium (chromium/chromium-browser) or "
                    "run 'python -m playwright install chromium'."
                ) from None
            page = browser.new_page()
            page_ref.append(page)
            page.route("**/*", intercept)
            try:
                page.goto(authorization_url)
            except Exception:
                raise OnleiheAuthError(
                    "Failed to navigate to authorization URL"
                ) from None

            # Validate login origin before calling handler
            if login_handler is not None:
                try:
                    current_url = page.url
                    auth_parsed = urlparse(authorization_url)
                    if not _origins_match(
                        current_url,
                        auth_parsed.scheme or "https",
                        auth_parsed.hostname or "",
                        auth_parsed.port,
                    ):
                        raise OnleiheAuthError(
                            "Login page redirected to an unexpected origin"
                        ) from None
                except OnleiheAuthError:
                    raise
                except Exception:
                    raise OnleiheAuthError(
                        "Failed to validate login page origin"
                    ) from None
                try:
                    login_handler(page)
                except OnleiheAuthError:
                    raise
                except Exception:
                    raise OnleiheAuthError(
                        "External login handler failed"
                    ) from None
            # Wait for navigation to complete and check page URL for callback
            while callback_url is None and time.monotonic() < deadline:
                page.wait_for_timeout(200)
                current_url = page.url
                parsed = urlparse(current_url)
                query = parse_qs(parsed.query)
                if "code" in query or "error" in query:
                    if _origins_match(current_url, parsed.scheme, parsed.hostname or "", parsed.port):
                        callback_url = current_url
                    break
    finally:
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass

    if callback_url is None:
        raise OnleiheAuthError("Timed out waiting for the external login callback")

    if client is not None:
        return _complete_external_login(
            client,
            callback_url=callback_url,
            expected_state=expected_state,
            redirect_url=redirect_url,
        )

    raise OnleiheAuthError(
        "External login requires an OnleiheClient to complete the session exchange"
    )


def external_login_manual(client: OnleiheClient, *, input_func=input) -> SessionState:
    authorization_url, redirect_url, expected_state = _external_login_request(client)
    print("Open the following URL in a browser on your local workstation.")
    print("Before logging in, disable JavaScript for this browser tab (for example through DevTools).")
    print("This prevents the Onleihe web app from consuming the one-time authorization code.")
    print(authorization_url)
    callback_url = input_func(
        "After the redirect, paste the complete https://...onleihe.de/?code=... URL: "
    ).strip()
    return _complete_external_login(
        client,
        callback_url=callback_url,
        expected_state=expected_state,
        redirect_url=redirect_url,
    )


def external_login_browser(
    client: OnleiheClient,
    *,
    timeout_secs: float = 300.0,
) -> SessionState:
    authorization_url, redirect_url, expected_state = _external_login_request(client)
    print("Open this URL in a browser and complete the library login:")
    print(authorization_url)
    return _run_external_login_browser(
        authorization_url,
        redirect_url,
        expected_state,
        headless=False,
        timeout_secs=timeout_secs,
        login_handler=None,
        client=client,
    )


def external_login_automated(
    client: OnleiheClient,
    *,
    username: str,
    password: str,
    headless: bool = True,
    timeout_secs: float = 120.0,
) -> SessionState:
    """Perform exactly one unattended OIDC login for a supported provider.

    Selects a handler by hostname only.  If no handler exists for the
    provider, raises ``OnleiheAuthError`` before launching a browser.
    """
    authorization_url, redirect_url, expected_state = _external_login_request(client)

    handler = get_login_handler(authorization_url)
    if handler is None:
        try:
            parsed_hostname = urlparse(authorization_url).hostname or "unknown"
        except Exception:
            parsed_hostname = "unknown"
        raise OnleiheAuthError(
            f"Automated login is not supported for provider: {parsed_hostname}"
        )

    def _handler(page: object) -> None:
        handler(page, username=username, password=password)

    return _run_external_login_browser(
        authorization_url,
        redirect_url,
        expected_state,
        headless=headless,
        timeout_secs=timeout_secs,
        login_handler=_handler,
        client=client,
    )
