from __future__ import annotations

import logging
from typing import Callable, Protocol
from urllib.parse import urlparse

from onleiharr._vendor.onleihe import OnleiheAuthError


class _PageLike(Protocol):
    """Minimal page interface for login handlers."""

    def wait_for_selector(self, selector: str, *, timeout: float | None = None) -> object: ...
    def query_selector_all(self, selector: str) -> list[object]: ...

logger = logging.getLogger(__name__)

# Stable selectors for the Munich library login form.
_SELECTOR_USERNAME = 'input[name="L#AUSW"]'
_SELECTOR_PASSWORD = 'input[name="LPASSW"]'
_SELECTOR_SUBMIT = 'input[name="LLOGIN"][type="submit"]'

# Recognized consent/allow labels (German), case-insensitive.
_CONSENT_LABELS = frozenset({
    "zustimmen",
    "erlauben",
    "weiter",
})

_MUNCHEN_HOST = "ssl.muenchen.de"


def munich_login_handler(
    page: _PageLike,
    *,
    username: str,
    password: str,
) -> None:
    """Fill and submit the Munich library login form.

    Supports at most one optional consent action using an explicit
    allowlist of German labels.
    """

    try:
        username_el = page.wait_for_selector(_SELECTOR_USERNAME, timeout=10000)
    except Exception:
        raise OnleiheAuthError(
            "Munich login form is missing the username field"
        ) from None

    try:
        password_el = page.wait_for_selector(_SELECTOR_PASSWORD, timeout=10000)
    except Exception:
        raise OnleiheAuthError(
            "Munich login form is missing the password field"
        ) from None

    try:
        submit_el = page.wait_for_selector(_SELECTOR_SUBMIT, timeout=10000)
    except Exception:
        raise OnleiheAuthError(
            "Munich login form is missing the submit button"
        ) from None

    try:
        username_el.fill(username)
    except Exception:
        raise OnleiheAuthError(
            "Munich login form: could not fill username field"
        ) from None

    try:
        password_el.fill(password)
    except Exception:
        raise OnleiheAuthError(
            "Munich login form: could not fill password field"
        ) from None

    try:
        submit_el.click()
    except Exception:
        raise OnleiheAuthError(
            "Munich login form: could not submit the login form"
        ) from None

    # Optional consent page: check all candidate buttons, click at most one.
    consent_buttons = page.query_selector_all(
        'input[type="submit"], button, a'
    )
    for btn in consent_buttons:
        try:
            label = btn.text_content()
            if not label:
                label = btn.get_attribute("value") or ""
            label = label.strip().casefold()
        except Exception:
            continue
        if label in _CONSENT_LABELS:
            try:
                btn.click()
            except Exception:
                pass
            break


def get_login_handler(authorization_url: str) -> Callable[..., None] | None:
    """Return a login handler if the URL matches a supported provider.

    Currently only ``ssl.muenchen.de`` (exact match, case-insensitive)
    is supported.  Subdomain tricks and suffix injections are rejected.
    """
    try:
        parsed = urlparse(authorization_url)
    except Exception:
        return None

    if not parsed.hostname:
        return None

    if parsed.hostname.casefold() != _MUNCHEN_HOST.casefold():
        return None

    return munich_login_handler  # type: ignore[return-value]
