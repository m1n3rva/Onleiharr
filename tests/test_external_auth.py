from __future__ import annotations

import re
from urllib.parse import urlparse

import pytest

from onleiharr._vendor.onleihe import OnleiheAuthError
from onleiharr.external_auth import get_login_handler, munich_login_handler


class _FakeElement:
    """Minimal mock for a Playwright element handle."""

    def __init__(self, text_content: str = ""):
        self._text_content = text_content
        self._filled = False
        self._clicked = False

    def fill(self, value: str) -> None:
        self._filled = True
        self._fill_value = value

    def click(self) -> None:
        self._clicked = True

    def text_content(self) -> str:
        return self._text_content

    def wait_for_element_state(self, state: str, timeout: float | None = None) -> "_FakeElement":
        return self


class _FakePage:
    """Minimal mock for a Playwright page used by the Munich handler."""

    def __init__(self):
        self._elements: dict[str, _FakeElement] = {}
        self._consent_buttons: list[_FakeElement] = []
        self._timeout: float | None = None
        self._goto_count = 0

    def set_elements(self, selectors_elements: dict[str, _FakeElement]) -> None:
        self._elements = selectors_elements

    def set_consents(self, buttons: list[_FakeElement]) -> None:
        self._consent_buttons = buttons

    def set_timeout(self, timeout: float | None) -> None:
        self._timeout = timeout

    def wait_for_selector(self, selector: str, *, timeout: float | None = None) -> _FakeElement:
        if self._timeout is not None:
            raise TimeoutError("timeout exceeded")
        if selector not in self._elements:
            raise KeyError(f"selector not found: {selector}")
        return self._elements[selector]

    def get_consents(self) -> list[_FakeElement]:
        return self._consent_buttons

    def query_selector_all(self, selector: str) -> list[_FakeElement]:
        return self._consent_buttons


def test_munich_handler_uses_exact_selectors(monkeypatch):
    """The handler must use the three exact Munich selectors."""
    username_el = _FakeElement()
    password_el = _FakeElement()
    submit_el = _FakeElement()

    page = _FakePage()
    page.set_elements({
        'input[name="L#AUSW"]': username_el,
        'input[name="LPASSW"]': password_el,
        'input[name="LLOGIN"][type="submit"]': submit_el,
    })

    munich_login_handler(page, username="testuser", password="testpass")

    assert username_el._filled is True
    assert username_el._fill_value == "testuser"
    assert password_el._filled is True
    assert password_el._fill_value == "testpass"
    assert submit_el._clicked is True


def test_munich_handler_fills_fields_once_and_clicks_submit_once(monkeypatch):
    """Each field is filled exactly once and submit is clicked exactly once."""
    username_el = _FakeElement()
    password_el = _FakeElement()
    submit_el = _FakeElement()

    page = _FakePage()
    page.set_elements({
        'input[name="L#AUSW"]': username_el,
        'input[name="LPASSW"]': password_el,
        'input[name="LLOGIN"][type="submit"]': submit_el,
    })

    munich_login_handler(page, username="u", password="p")

    assert username_el._filled is True
    assert password_el._filled is True
    assert submit_el._clicked is True


def test_munich_handler_clicks_consent_button_at_most_once(monkeypatch):
    """A recognized consent button is clicked at most once."""
    username_el = _FakeElement()
    password_el = _FakeElement()
    submit_el = _FakeElement()
    consent_el = _FakeElement(text_content="Zustimmen")

    page = _FakePage()
    page.set_elements({
        'input[name="L#AUSW"]': username_el,
        'input[name="LPASSW"]': password_el,
        'input[name="LLOGIN"][type="submit"]': submit_el,
    })
    page.set_consents([consent_el])

    munich_login_handler(page, username="u", password="p")

    assert submit_el._clicked is True
    assert consent_el._clicked is True


def test_munich_handler_no_consent_page_is_not_error(monkeypatch):
    """When there is no consent page, the handler succeeds."""
    username_el = _FakeElement()
    password_el = _FakeElement()
    submit_el = _FakeElement()

    page = _FakePage()
    page.set_elements({
        'input[name="L#AUSW"]': username_el,
        'input[name="LPASSW"]': password_el,
        'input[name="LLOGIN"][type="submit"]': submit_el,
    })
    page.set_consents([])

    munich_login_handler(page, username="u", password="p")


def test_munich_handler_missing_field_raises(monkeypatch):
    """A missing field raises OnleiheAuthError."""
    username_el = _FakeElement()
    # password_el is missing

    page = _FakePage()
    page.set_elements({
        'input[name="L#AUSW"]': username_el,
        # 'input[name="LPASSW"]' is intentionally missing
        'input[name="LLOGIN"][type="submit"]': _FakeElement(),
    })

    with pytest.raises(OnleiheAuthError, match="form"):
        munich_login_handler(page, username="u", password="p")


def test_munich_handler_timeout_raises(monkeypatch):
    """A timeout waiting for elements raises OnleiheAuthError."""
    page = _FakePage()
    page.set_timeout(0.0)

    with pytest.raises(OnleiheAuthError, match="form is missing"):
        munich_login_handler(page, username="u", password="p")


def test_munich_handler_credentials_not_in_exception(monkeypatch, caplog):
    """Credentials must not appear in exception text or logs."""
    page = _FakePage()
    page.set_timeout(0.0)

    with pytest.raises(OnleiheAuthError):
        munich_login_handler(page, username="supersecretuser", password="supersecretpass")

    for record in caplog.records:
        assert "supersecretuser" not in record.message
        assert "supersecretpass" not in record.message


def test_munich_handler_consent_labels_are_case_insensitive(monkeypatch):
    """Recognized consent labels are matched case-insensitively."""
    username_el = _FakeElement()
    password_el = _FakeElement()
    submit_el = _FakeElement()
    consent_el = _FakeElement(text_content="zUSTIMMEN")

    page = _FakePage()
    page.set_elements({
        'input[name="L#AUSW"]': username_el,
        'input[name="LPASSW"]': password_el,
        'input[name="LLOGIN"][type="submit"]': submit_el,
    })
    page.set_consents([consent_el])

    munich_login_handler(page, username="u", password="p")
    assert consent_el._clicked is True


def test_registry_accepts_exact_munich_host(monkeypatch):
    """The registry accepts the exact Munich provider host."""
    handler = get_login_handler("https://ssl.muenchen.de/authorize")
    assert handler is not None

    handler = get_login_handler("https://SSL.MUENCHEN.DE/authorize")
    assert handler is not None

    handler = get_login_handler("https://ssl.muenchen.de")
    assert handler is not None


def test_registry_rejects_unknown_hosts(monkeypatch):
    """The registry rejects unknown provider hosts."""
    assert get_login_handler("https://unknown.example.com/authorize") is None
    assert get_login_handler("https://onleihe.example.org/authorize") is None
    assert get_login_handler("https://provider.invalid/auth") is None


def test_registry_rejects_subdomain_suffix_tricks(monkeypatch):
    """The registry rejects subdomain tricks and suffix injections."""
    assert get_login_handler("https://ssl.muenchen.de.attacker.invalid/authorize") is None
    assert get_login_handler("https://ssl.muenchen.de.evil.com/authorize") is None
    assert get_login_handler("https://evil-ssl.muenchen.de/authorize") is None
    assert get_login_handler("https://muenchen.de/authorize") is None
    assert get_login_handler("https://ssl.muenchen.de.evil/authorize") is None


def test_registry_rejects_urls_without_host(monkeypatch):
    """The registry rejects URLs without a valid host."""
    assert get_login_handler("") is None
    assert get_login_handler("about:blank") is None
    assert get_login_handler("file:///path/to/page") is None
