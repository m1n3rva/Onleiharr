from __future__ import annotations

import io
import logging
import sys
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import onleiharr.cli as cli
import pytest
from onleiharr._vendor.onleihe import (
    MediaItem,
    OnleiheAPIError,
    OnleiheAuthError,
    OnleiheNotFoundError,
    ProductDetails,
    SearchResultPage,
)
from onleiharr.cli import (
    WatchedMedia,
    build_apprise,
    fetch_all_watched_media,
    fetch_category_watch_media,
    fetch_product_watch_media,
    format_message,
    lend_failure_allows_reserve,
    matches_filter,
    maybe_lend_or_reserve,
    normalize_product_watch_ids,
    notify,
    process_my_media_downloads,
)
from onleiharr.config import NotificationConfig, WatchCategory


class TtyStringIO(io.StringIO):
    def isatty(self) -> bool:
        return True


class FakeClient:
    host = "example.onleihe.de"
    last_search_body = None

    def get_product(self, product_id: str, *, include_user_context: bool = True) -> ProductDetails:
        if product_id == "issue-with-container":
            return ProductDetails(
                id=product_id,
                product_id=product_id,
                title="Finanzen 04/2026",
                subtitle=None,
                media_type="E_MAGAZINE",
                raw={"product": {"isContainer": False, "containerIds": ["series-1"]}},
            )
        if product_id == "standalone":
            return ProductDetails(
                id=product_id,
                product_id=product_id,
                title="Single Book",
                subtitle=None,
                media_type="E_BOOK",
                raw={"product": {"isContainer": False, "containerIds": []}},
            )
        return ProductDetails(
            id=product_id,
            product_id=product_id,
            title="Finanzen Reihe",
            subtitle=None,
            media_type="SERIES",
            raw={"product": {"isContainer": True, "containerIds": []}},
            included_media=[
                MediaItem(
                    id="issue-1",
                    product_id="issue-1",
                    title="Finanzen 06/2026",
                    subtitle=None,
                    media_type="E_MAGAZINE",
                    authors=[],
                    availability={"isAvailable": True},
                    cover_url="https://static.example/cover.jpg",
                )
            ],
        )

    def search_category_elements(self, category_ids: list[str], *, require_login: bool = False) -> SearchResultPage:
        raise AssertionError("fetch_category_watch_media should build the body from watch options")

    def build_category_search_body(self, category_ids, *, sort):
        assert category_ids == ["cat-1", "cat-2"]
        return {
            "query": [{"query": "U", "fields": ["categories.id"], "operator": "OR"}],
            "postFilters": [],
            "sort": sort,
            "size": 50,
        }

    def search_media(self, *, raw_body, require_login: bool = False) -> SearchResultPage:
        assert require_login is False
        self.last_search_body = raw_body
        self.search_bodies = getattr(self, "search_bodies", [])
        self.search_bodies.append(raw_body)
        return SearchResultPage(
            items=[
                MediaItem(
                    id="book-1",
                    product_id="book-1",
                    title="Python fuer Profis",
                    subtitle="Praxiswissen",
                    media_type="E_BOOK",
                    authors=["Ada Lovelace"],
                    availability={"isAvailable": True},
                ),
                MediaItem(
                    id="book-2",
                    product_id="book-2",
                    title="Gartenratgeber",
                    subtitle=None,
                    media_type="E_BOOK",
                    authors=[],
                    availability={"isAvailable": True},
                ),
                MediaItem(
                    id="audio-1",
                    product_id="audio-1",
                    title="Python Hoerbuch",
                    subtitle=None,
                    media_type="E_AUDIO",
                    authors=["Ada Lovelace"],
                    availability={"isAvailable": True},
                ),
            ],
            total_items=3,
        )


def test_product_watch_expands_included_media_without_keyword_check():
    media = fetch_product_watch_media(FakeClient(), "series-1")

    assert len(media) == 1
    assert media[0].product_id == "issue-1"
    assert media[0].keyword_required is False
    assert media[0].keyword_matched is True
    assert media[0].url == "https://example.onleihe.de/mymedia/mediadetail?productId=issue-1"
    assert media[0].cover_url == "https://static.example/cover.jpg"


def test_product_watch_resolves_single_issue_container_ids():
    media = fetch_product_watch_media(FakeClient(), "issue-with-container")

    assert [item.product_id for item in media] == ["issue-1"]
    assert media[0].source == "product:issue-with-container"


def test_product_watch_rejects_container_without_included_media():
    class Client(FakeClient):
        def get_product(self, product_id: str, *, include_user_context: bool = True) -> ProductDetails:
            return ProductDetails(
                id=product_id,
                product_id=product_id,
                title="Finanzen Reihe",
                subtitle=None,
                media_type="SERIES",
                raw={"product": {"isContainer": True, "containerIds": []}},
            )

    with pytest.raises(OnleiheAPIError, match="container.*no included media"):
        fetch_product_watch_media(Client(), "series-1")


def test_product_watch_rejects_incomplete_resolved_container():
    class Client(FakeClient):
        def get_product(self, product_id: str, *, include_user_context: bool = True) -> ProductDetails:
            if product_id == "issue-1":
                return ProductDetails(
                    id=product_id,
                    product_id=product_id,
                    title="Finanzen 07/2026",
                    subtitle=None,
                    media_type="E_MAGAZINE",
                    raw={"product": {"isContainer": False, "containerIds": ["series-1"]}},
                )
            return ProductDetails(
                id=product_id,
                product_id=product_id,
                title="Finanzen Reihe",
                subtitle=None,
                media_type="SERIES",
                raw={"product": {"isContainer": True, "containerIds": []}},
            )

    with pytest.raises(OnleiheAPIError, match="resolved to container.*no included media"):
        fetch_product_watch_media(Client(), "issue-1")


def test_normalize_product_watch_ids_resolves_issue_ids_to_deduped_containers():
    class Client(FakeClient):
        def get_product(self, product_id: str, *, include_user_context: bool = True) -> ProductDetails:
            if product_id in {"issue-a", "issue-b"}:
                return ProductDetails(
                    id=product_id,
                    product_id=product_id,
                    title="Finanzen",
                    subtitle="04/2026",
                    media_type="E_MAGAZINE",
                    raw={"product": {"isContainer": False, "containerIds": ["series-1"]}},
                )
            return super().get_product(product_id, include_user_context=include_user_context)

    normalized = normalize_product_watch_ids(Client(), ["issue-a", "issue-b", "series-1"])

    assert normalized == ["series-1"]


def test_normalize_product_watch_ids_keeps_single_product_and_warns(caplog):
    normalized = normalize_product_watch_ids(FakeClient(), ["standalone"])

    assert normalized == ["standalone"]
    assert "single product without container ids" in caplog.text


def test_normalize_product_watch_ids_drops_initial_not_found_ids(caplog):
    class Client(FakeClient):
        def get_product(self, product_id: str, *, include_user_context: bool = True) -> ProductDetails:
            if product_id == "missing":
                raise OnleiheNotFoundError(
                    "not found",
                    status_code=500,
                    payload={"messageId": "no-such-element"},
                )
            return super().get_product(product_id, include_user_context=include_user_context)

    normalized = normalize_product_watch_ids(Client(), ["missing", "series-1"])

    assert normalized == ["series-1"]
    assert "disabling this watch target" in caplog.text


def test_fetch_all_watched_media_logs_not_found_without_traceback(caplog):
    class Client(FakeClient):
        def get_product(self, product_id: str, *, include_user_context: bool = True) -> ProductDetails:
            if product_id == "missing":
                raise OnleiheNotFoundError(
                    "not found",
                    status_code=500,
                    payload={"messageId": "no-such-element"},
                )
            return super().get_product(product_id, include_user_context=include_user_context)

    config = SimpleNamespace(
        general=SimpleNamespace(
            watch_product_ids=["missing"],
            watch_categories=[],
        )
    )

    result = fetch_all_watched_media(Client(), config)  # type: ignore[arg-type]

    assert result.media == []
    assert result.errors == 1
    assert "no longer exists" in caplog.text
    assert "Traceback" not in caplog.text


def test_format_message_includes_subtitle_in_display_title():
    media = watched_media(product_id="magazine-1", available=True)
    media = WatchedMedia(
        product_id=media.product_id,
        title="Stiftung Warentest Finanzen",
        url=media.url,
        media_type=media.media_type,
        authors=media.authors,
        subtitle="06/2026",
        publication_date=media.publication_date,
        available=media.available,
        availability_text=media.availability_text,
        acsm_url=media.acsm_url,
        source=media.source,
        keyword_required=media.keyword_required,
        keyword_matched=media.keyword_matched,
    )

    assert "Stiftung Warentest Finanzen (06/2026)" in format_message(media, "auto lent")


def test_notify_sends_cover_to_image_only_target_when_file_attachment_exists(tmp_path):
    class Server:
        attachment_support = True
        attach_supported_mime_type = "^image/.*"

        def __init__(self):
            self.calls = []

        def notify(self, **kwargs):
            self.calls.append(kwargs)

    class Apprise:
        def __init__(self, server):
            self.server = server

        def find(self):
            return [self.server]

        def notify(self, **kwargs):
            raise AssertionError("aggregate notify should not be used with attachments")

    attachment = tmp_path / "download.pdf"
    attachment.write_bytes(b"pdf")
    server = Server()
    apobj = Apprise(server)

    notify(
        apobj,
        "message",
        attachments=[attachment],
        image_urls=["https://static.example/cover.jpg"],
    )  # type: ignore[arg-type]

    assert server.calls == [
        {
            "title": "Onleihe: New media",
            "body": "message",
            "attach": ["https://static.example/cover.jpg"],
        }
    ]


def test_notify_preserves_file_attachment_for_general_attachment_target(tmp_path):
    class Server:
        attachment_support = True

        def __init__(self):
            self.calls = []

        def notify(self, **kwargs):
            self.calls.append(kwargs)

    class Apprise:
        def __init__(self, server):
            self.server = server

        def find(self):
            return [self.server]

        def notify(self, **kwargs):
            raise AssertionError("aggregate notify should not be used with attachments")

    attachment = tmp_path / "download.pdf"
    attachment.write_bytes(b"pdf")
    server = Server()
    apobj = Apprise(server)

    notify(
        apobj,
        "message",
        attachments=[attachment],
        image_urls=["https://static.example/cover.jpg"],
    )  # type: ignore[arg-type]

    assert server.calls == [
        {
            "title": "Onleihe: New media",
            "body": "message",
            "attach": [str(attachment)],
        }
    ]


def test_external_auth_notification_contains_renewal_commands(tmp_path):
    class Apprise:
        def __init__(self):
            self.calls = []

        def notify(self, **kwargs):
            self.calls.append(kwargs)

    apobj = Apprise()
    config = SimpleNamespace(config_path=tmp_path / "onleiharr.toml")

    cli.notify_external_auth_required(apobj, config)  # type: ignore[arg-type]

    assert apobj.calls[0]["title"] == "Onleiharr: Anmeldung erneuern"
    assert f"onleiharr --login -c {tmp_path / 'onleiharr.toml'}" in apobj.calls[0]["body"]
    assert "systemctl --user restart onleiharr" in apobj.calls[0]["body"]


def test_notify_uses_cover_for_general_target_when_no_file_attachment():
    class Server:
        attachment_support = True

        def __init__(self):
            self.calls = []

        def notify(self, **kwargs):
            self.calls.append(kwargs)

    class Apprise:
        def __init__(self, server):
            self.server = server

        def find(self):
            return [self.server]

        def notify(self, **kwargs):
            raise AssertionError("aggregate notify should not be used with attachments")

    server = Server()
    apobj = Apprise(server)

    notify(apobj, "message", image_urls=["https://static.example/cover.jpg"])  # type: ignore[arg-type]

    assert server.calls == [
        {
            "title": "Onleihe: New media",
            "body": "message",
            "attach": ["https://static.example/cover.jpg"],
        }
    ]


def test_notify_omits_cover_url_when_attachments_are_unsupported():
    class Server:
        attachment_support = False

        def __init__(self):
            self.calls = []

        def notify(self, **kwargs):
            self.calls.append(kwargs)

    class Apprise:
        def __init__(self, server):
            self.server = server

        def find(self):
            return [self.server]

        def notify(self, **kwargs):
            raise AssertionError("aggregate notify should not be used with image URLs")

    server = Server()
    apobj = Apprise(server)

    notify(apobj, "message", image_urls=["https://static.example/cover.jpg"])  # type: ignore[arg-type]

    assert server.calls == [{"title": "Onleihe: New media", "body": "message"}]


def test_notify_reports_failed_aggregate_delivery():
    class Apprise:
        def notify(self, **kwargs):
            return False

    assert notify(Apprise(), "message") is False  # type: ignore[arg-type]


def test_category_watch_filters_by_keywords():
    client = FakeClient()
    watch = WatchCategory(
        category_ids=["cat-1", "cat-2"],
        keywords=["python"],
        media_types=["E_BOOK", "E_AUDIO"],
        filters=[{"field": "language", "values": ["ger"]}],
        sort_field="licence.stockChangedTimestamp",
        sort_order="DESC",
    )

    result = fetch_category_watch_media(client, watch)

    assert [item.product_id for item in result.media] == ["book-1", "audio-1"]
    assert result.total == 3
    assert result.media[0].keyword_required is True
    assert result.media[0].keyword_matched is True
    assert len(client.search_bodies) == 1
    assert client.last_search_body["sort"] == [{"field": "licence.stockChangedTimestamp", "order": "DESC"}]
    assert client.last_search_body["postFilters"] == [
        {"field": "mediaType", "values": ["E_BOOK", "E_AUDIO"], "type": "TERMS", "operator": "OR"},
        {"field": "language", "values": ["ger"]},
    ]


def test_fetch_all_watched_media_uses_global_keyword_match_mode():
    class Client(FakeClient):
        def search_media(self, *, raw_body, require_login: bool = False) -> SearchResultPage:
            return SearchResultPage(
                items=[
                    MediaItem(
                        id="false-positive",
                        product_id="false-positive",
                        title="ICH-POWER",
                        subtitle="Wie du sie für dich entfaltest",
                        media_type="E_BOOK",
                    ),
                    MediaItem(
                        id="match",
                        product_id="match",
                        title="Test automation",
                        subtitle=None,
                        media_type="E_BOOK",
                    ),
                ]
            )

    watch = WatchCategory(category_ids=["cat-1", "cat-2"], keywords=["test"])
    config = SimpleNamespace(
        general=SimpleNamespace(
            watch_product_ids=[],
            watch_categories=[watch],
            keyword_match_mode="word_start",
        )
    )

    result = fetch_all_watched_media(Client(), config)  # type: ignore[arg-type]

    assert [item.product_id for item in result.media] == ["match"]


def test_fetch_all_watched_media_reports_target_errors():
    class Client(FakeClient):
        def get_product(self, product_id: str, *, include_user_context: bool = True) -> ProductDetails:
            if product_id == "broken":
                raise OnleiheAPIError("not found", status_code=404)
            return super().get_product(product_id, include_user_context=include_user_context)

    config = SimpleNamespace(
        general=SimpleNamespace(
            watch_product_ids=["series-1", "broken"],
            watch_categories=[],
        )
    )

    result = fetch_all_watched_media(Client(), config)  # type: ignore[arg-type]

    assert [item.product_id for item in result.media] == ["issue-1"]
    assert result.errors == 1
    assert result.successful_sources == frozenset({"product:series-1"})


def test_run_loop_checks_maintenance_after_poll_error_and_retries(monkeypatch):
    class Client:
        def __init__(self):
            self.maintenance_states = [True, False]
            self.closed = False

        def maintenance_active(self):
            return self.maintenance_states.pop(0)

        def close(self):
            self.closed = True

    client = Client()
    calls = []

    config = SimpleNamespace(
        general=SimpleNamespace(
            poll_interval_secs=300.0,
            watch_product_ids=[],
            watch_categories=[],
        ),
        notification=SimpleNamespace(test_notification=False),
        gourou=SimpleNamespace(lendings_poll_interval_secs=0.0),
        external_auth=SimpleNamespace(auto_login=False, headless=True, timeout_secs=120.0, username=None, password=None, max_login_attempts=5),
    )

    monkeypatch.setattr(cli, "build_apprise", lambda config: None)
    monkeypatch.setattr(cli, "create_onleihe_client", lambda config: client)
    monkeypatch.setattr(cli, "build_gourou_client", lambda config: None)
    monkeypatch.setattr(
        cli,
        "time",
        SimpleNamespace(monotonic=lambda: 0.0, sleep=lambda secs: calls.append(("sleep", secs))),
    )
    monkeypatch.setattr(cli, "login", lambda client, config: calls.append(("login", None)))

    def fake_fetch_all_watched_media(client, config, *, log_summary=False):
        calls.append(("fetch", log_summary))
        errors = 1 if len([call for call in calls if call[0] == "fetch"]) == 1 else 0
        return cli.WatchPollResult(media=[], errors=errors)

    monkeypatch.setattr(cli, "fetch_all_watched_media", fake_fetch_all_watched_media)

    cli.run_loop(config, SimpleNamespace(test_notification=False, once=True))

    assert calls == [("login", None), ("fetch", True), ("sleep", 300.0), ("fetch", True)]
    assert client.closed is True


def test_run_loop_retries_startup_error_during_maintenance(monkeypatch):
    class Client:
        def __init__(self):
            self.maintenance_states = [True, False]
            self.closed = False

        def maintenance_active(self):
            return self.maintenance_states.pop(0)

        def close(self):
            self.closed = True

    client = Client()
    calls = []

    config = SimpleNamespace(
        general=SimpleNamespace(
            poll_interval_secs=300.0,
            watch_product_ids=[],
            watch_categories=[],
        ),
        notification=SimpleNamespace(test_notification=False),
        gourou=SimpleNamespace(lendings_poll_interval_secs=0.0),
        external_auth=SimpleNamespace(auto_login=False, headless=True, timeout_secs=120.0, username=None, password=None, max_login_attempts=5),
    )

    monkeypatch.setattr(cli, "build_apprise", lambda config: None)
    monkeypatch.setattr(cli, "create_onleihe_client", lambda config: client)
    monkeypatch.setattr(cli, "build_gourou_client", lambda config: None)
    monkeypatch.setattr(
        cli,
        "time",
        SimpleNamespace(monotonic=lambda: 0.0, sleep=lambda secs: calls.append(("sleep", secs))),
    )

    def fake_login(client, config):
        calls.append(("login", None))
        if len([call for call in calls if call[0] == "login"]) == 1:
            raise OnleiheAPIError("Server disconnected without sending a response.")

    monkeypatch.setattr(cli, "login", fake_login)

    def fake_fetch_all_watched_media(client, config, *, log_summary=False):
        calls.append(("fetch", log_summary))
        return cli.WatchPollResult(media=[], errors=0)

    monkeypatch.setattr(cli, "fetch_all_watched_media", fake_fetch_all_watched_media)

    cli.run_loop(config, SimpleNamespace(test_notification=False, once=True))

    assert calls == [("login", None), ("sleep", 300.0), ("login", None), ("fetch", True)]
    assert client.closed is True


def test_run_loop_recovers_upa_authentication_once(monkeypatch):
    class Client:
        closed = False

        def close(self):
            self.closed = True

    client = Client()
    calls = []
    config = SimpleNamespace(
        credentials=SimpleNamespace(auth_type="upa"),
        general=SimpleNamespace(
            poll_interval_secs=300.0,
            watch_product_ids=[],
            watch_categories=[],
        ),
        notification=SimpleNamespace(test_notification=False),
        gourou=SimpleNamespace(lendings_poll_interval_secs=0.0),
        external_auth=SimpleNamespace(auto_login=False, headless=True, timeout_secs=120.0, username=None, password=None, max_login_attempts=5),
    )

    monkeypatch.setattr(cli, "build_apprise", lambda config: None)
    monkeypatch.setattr(cli, "create_onleihe_client", lambda config: client)
    monkeypatch.setattr(cli, "build_gourou_client", lambda config: None)
    monkeypatch.setattr(cli, "login", lambda client, config: calls.append("login"))

    def fetch_media(client, config, *, log_summary=False):
        calls.append("fetch")
        if calls.count("fetch") == 1:
            raise OnleiheAuthError("refresh rejected", status_code=401)
        return cli.WatchPollResult(media=[])

    monkeypatch.setattr(cli, "fetch_all_watched_media", fetch_media)

    cli.run_loop(config, SimpleNamespace(test_notification=False, once=True))

    assert calls == ["login", "fetch", "login", "fetch"]
    assert client.closed is True


def test_run_loop_upa_exits_after_one_recovery_attempt(monkeypatch):
    class Client:
        closed = False

        def close(self):
            self.closed = True

    client = Client()
    calls = []
    config = SimpleNamespace(
        credentials=SimpleNamespace(auth_type="upa"),
        general=SimpleNamespace(
            poll_interval_secs=300.0,
            watch_product_ids=[],
            watch_categories=[],
        ),
        notification=SimpleNamespace(test_notification=False),
        gourou=SimpleNamespace(lendings_poll_interval_secs=0.0),
        external_auth=SimpleNamespace(auto_login=False, headless=True, timeout_secs=120.0, username=None, password=None, max_login_attempts=99),
    )

    monkeypatch.setattr(cli, "build_apprise", lambda config: None)
    monkeypatch.setattr(cli, "create_onleihe_client", lambda config: client)
    monkeypatch.setattr(cli, "build_gourou_client", lambda config: None)
    monkeypatch.setattr(cli, "login", lambda client, config: calls.append("login"))

    def fetch_media(client, config, *, log_summary=False):
        calls.append("fetch")
        raise OnleiheAuthError("refresh rejected", status_code=401)

    monkeypatch.setattr(cli, "fetch_all_watched_media", fetch_media)

    with pytest.raises(OnleiheAuthError):
        cli.run_loop(config, SimpleNamespace(test_notification=False, once=True))

    assert calls == ["login", "fetch", "login", "fetch"]
    assert client.closed is True


def test_run_loop_resets_auth_recovery_after_successful_cycle(monkeypatch):
    class EndTestLoop(Exception):
        pass

    class Client:
        closed = False

        def close(self):
            self.closed = True

    client = Client()
    calls = []
    fetch_results = iter(["auth", "success", "auth", "success"])
    config = SimpleNamespace(
        credentials=SimpleNamespace(auth_type="upa"),
        general=SimpleNamespace(
            poll_interval_secs=300.0,
            watch_product_ids=[],
            watch_categories=[],
        ),
        notification=SimpleNamespace(test_notification=False),
        gourou=SimpleNamespace(lendings_poll_interval_secs=0.0),
        external_auth=SimpleNamespace(auto_login=False, headless=True, timeout_secs=120.0, username=None, password=None, max_login_attempts=5),
    )

    monkeypatch.setattr(cli, "build_apprise", lambda config: None)
    monkeypatch.setattr(cli, "create_onleihe_client", lambda config: client)
    monkeypatch.setattr(cli, "build_gourou_client", lambda config: None)
    monkeypatch.setattr(cli, "login", lambda client, config: calls.append("login"))
    monkeypatch.setattr(
        cli,
        "time",
        SimpleNamespace(monotonic=lambda: 0.0, sleep=lambda secs: None),
    )

    def fetch_media(client, config, *, log_summary=False):
        calls.append("fetch")
        try:
            result = next(fetch_results)
        except StopIteration:
            raise EndTestLoop from None
        if result == "auth":
            raise OnleiheAuthError("refresh rejected", status_code=401)
        return cli.WatchPollResult(media=[])

    monkeypatch.setattr(cli, "fetch_all_watched_media", fetch_media)

    with pytest.raises(EndTestLoop):
        cli.run_loop(config, SimpleNamespace(test_notification=False, once=False))

    assert calls == ["login", "fetch", "login", "fetch", "fetch", "login", "fetch", "fetch"]
    assert client.closed is True


def test_run_loop_propagates_failed_upa_relogin(monkeypatch):
    class Client:
        closed = False

        def close(self):
            self.closed = True

    client = Client()
    login_calls = 0
    config = SimpleNamespace(
        credentials=SimpleNamespace(auth_type="upa"),
        general=SimpleNamespace(
            poll_interval_secs=300.0,
            watch_product_ids=[],
            watch_categories=[],
        ),
        notification=SimpleNamespace(test_notification=False),
        gourou=SimpleNamespace(lendings_poll_interval_secs=0.0),
        external_auth=SimpleNamespace(auto_login=False, headless=True, timeout_secs=120.0, username=None, password=None, max_login_attempts=5),
    )

    monkeypatch.setattr(cli, "build_apprise", lambda config: None)
    monkeypatch.setattr(cli, "create_onleihe_client", lambda config: client)
    monkeypatch.setattr(cli, "build_gourou_client", lambda config: None)

    def fake_login(client, config):
        nonlocal login_calls
        login_calls += 1
        if login_calls == 2:
            raise OnleiheAuthError("login rejected", status_code=401)

    monkeypatch.setattr(cli, "login", fake_login)
    monkeypatch.setattr(
        cli,
        "fetch_all_watched_media",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OnleiheAuthError("refresh rejected", status_code=401)
        ),
    )

    with pytest.raises(OnleiheAuthError, match="login rejected"):
        cli.run_loop(config, SimpleNamespace(test_notification=False, once=True))

    assert login_calls == 2
    assert client.closed is True


def test_run_loop_keeps_open_id_manual_recovery(monkeypatch):
    class Client:
        closed = False
        session_callback = None

        def close(self):
            self.closed = True

    client = Client()
    notifications = []
    config = SimpleNamespace(
        credentials=SimpleNamespace(auth_type="open_id"),
        general=SimpleNamespace(
            poll_interval_secs=300.0,
            watch_product_ids=[],
            watch_categories=[],
        ),
        notification=SimpleNamespace(test_notification=False),
        gourou=SimpleNamespace(lendings_poll_interval_secs=0.0),
        external_auth=SimpleNamespace(auto_login=False, headless=True, timeout_secs=120.0, username=None, password=None, max_login_attempts=5),
    )

    monkeypatch.setattr(cli, "build_apprise", lambda config: None)
    monkeypatch.setattr(cli, "create_onleihe_client", lambda config: client)
    monkeypatch.setattr(cli, "build_gourou_client", lambda config: None)
    monkeypatch.setattr(cli, "login", lambda client, config: None)
    monkeypatch.setattr(
        cli,
        "fetch_all_watched_media",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            OnleiheAuthError("refresh rejected", status_code=401)
        ),
    )
    monkeypatch.setattr(
        cli,
        "notify_external_auth_required",
        lambda apobj, config: notifications.append(config),
    )

    with pytest.raises(OnleiheAuthError):
        cli.run_loop(config, SimpleNamespace(test_notification=False, once=True))

    assert notifications == [config]
    assert client.closed is True


def test_run_loop_keeps_healthy_watches_active_and_primes_recovered_watch(monkeypatch):
    class EndTestLoop(Exception):
        pass

    class Client:
        closed = False

        def maintenance_active(self):
            return False

        def close(self):
            self.closed = True

    client = Client()
    healthy_source = "product:healthy"
    recovering_source = "category:0:recovering"
    healthy_existing = replace(
        watched_media(product_id="healthy-existing", available=True),
        source=healthy_source,
    )
    healthy_new = replace(
        watched_media(product_id="healthy-new", available=True),
        source=healthy_source,
    )
    recovered_existing = replace(
        watched_media(product_id="recovered-existing", available=True),
        source=recovering_source,
    )
    recovered_new = replace(
        watched_media(product_id="recovered-new", available=True),
        source=recovering_source,
    )
    poll_results = iter(
        [
            cli.WatchPollResult(
                media=[healthy_existing],
                errors=1,
                successful_sources=frozenset({healthy_source}),
            ),
            cli.WatchPollResult(
                media=[healthy_existing, healthy_new],
                errors=1,
                successful_sources=frozenset({healthy_source}),
            ),
            cli.WatchPollResult(
                media=[healthy_existing, healthy_new, recovered_existing],
                successful_sources=frozenset({healthy_source, recovering_source}),
            ),
            cli.WatchPollResult(
                media=[
                    healthy_existing,
                    healthy_new,
                    recovered_existing,
                    recovered_new,
                ],
                successful_sources=frozenset({healthy_source, recovering_source}),
            ),
        ]
    )
    handled_ids: list[str] = []

    def fetch_media(client, config, *, log_summary=False):
        try:
            return next(poll_results)
        except StopIteration:
            raise EndTestLoop from None

    def handle_media(media, *args, **kwargs):
        handled_ids.append(media.product_id)
        return "handled", None

    config = SimpleNamespace(
        general=SimpleNamespace(
            poll_interval_secs=1.0,
            watch_product_ids=[],
            watch_categories=[],
        ),
        notification=SimpleNamespace(test_notification=False),
        gourou=SimpleNamespace(lendings_poll_interval_secs=0.0),
        external_auth=SimpleNamespace(auto_login=False, headless=True, timeout_secs=120.0, username=None, password=None, max_login_attempts=5),
    )
    monkeypatch.setattr(cli, "build_apprise", lambda config: None)
    monkeypatch.setattr(cli, "create_onleihe_client", lambda config: client)
    monkeypatch.setattr(cli, "build_gourou_client", lambda config: None)
    monkeypatch.setattr(cli, "login", lambda client, config: None)
    monkeypatch.setattr(cli, "fetch_all_watched_media", fetch_media)
    monkeypatch.setattr(cli, "maybe_lend_or_reserve", handle_media)
    monkeypatch.setattr(cli, "notify", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        cli,
        "time",
        SimpleNamespace(monotonic=lambda: 0.0, sleep=lambda secs: None),
    )

    with pytest.raises(EndTestLoop):
        cli.run_loop(config, SimpleNamespace(test_notification=False, once=False))

    assert handled_ids == ["healthy-new", "recovered-new"]
    assert client.closed is True


def test_run_loop_retries_transient_media_handling_error(monkeypatch):
    class EndTestLoop(Exception):
        pass

    class Client:
        closed = False

        def maintenance_active(self):
            return False

        def close(self):
            self.closed = True

    client = Client()
    existing = watched_media(product_id="existing", available=True)
    new = watched_media(product_id="new", available=True)
    poll_results = iter(
        [
            cli.WatchPollResult(media=[existing]),
            cli.WatchPollResult(media=[existing, new]),
            cli.WatchPollResult(media=[existing, new]),
        ]
    )
    attempts = 0

    def fetch_media(client, config, *, log_summary=False):
        try:
            return next(poll_results)
        except StopIteration:
            raise EndTestLoop from None

    def handle_media(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OnleiheAPIError("transient failure")
        return "handled", None

    config = SimpleNamespace(
        general=SimpleNamespace(
            poll_interval_secs=1.0,
            watch_product_ids=[],
            watch_categories=[],
        ),
        notification=SimpleNamespace(test_notification=False),
        gourou=SimpleNamespace(lendings_poll_interval_secs=0.0),
        external_auth=SimpleNamespace(auto_login=False, headless=True, timeout_secs=120.0, username=None, password=None, max_login_attempts=5),
    )
    monkeypatch.setattr(cli, "build_apprise", lambda config: None)
    monkeypatch.setattr(cli, "create_onleihe_client", lambda config: client)
    monkeypatch.setattr(cli, "build_gourou_client", lambda config: None)
    monkeypatch.setattr(cli, "login", lambda client, config: None)
    monkeypatch.setattr(cli, "fetch_all_watched_media", fetch_media)
    monkeypatch.setattr(cli, "maybe_lend_or_reserve", handle_media)
    monkeypatch.setattr(cli, "notify", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        cli,
        "time",
        SimpleNamespace(monotonic=lambda: 0.0, sleep=lambda secs: None),
    )

    with pytest.raises(EndTestLoop):
        cli.run_loop(config, SimpleNamespace(test_notification=False, once=False))

    assert attempts == 2
    assert client.closed is True


def test_run_loop_retries_notification_without_repeating_lend(monkeypatch):
    class EndTestLoop(Exception):
        pass

    class Client:
        closed = False

        def close(self):
            self.closed = True

    client = Client()
    existing = watched_media(product_id="existing", available=True)
    new = watched_media(product_id="new", available=True)
    poll_results = iter(
        [
            cli.WatchPollResult(media=[existing]),
            cli.WatchPollResult(media=[existing, new]),
            cli.WatchPollResult(media=[existing, new]),
        ]
    )
    lend_attempts = 0
    notification_results = iter([False, True])

    def fetch_media(client, config, *, log_summary=False):
        try:
            return next(poll_results)
        except StopIteration:
            raise EndTestLoop from None

    def handle_media(
        media,
        client,
        config,
        gourou_client,
        rented_media_ids,
        downloaded_media_ids,
    ):
        nonlocal lend_attempts
        if media.product_id in rented_media_ids:
            return "already handled this run", None
        lend_attempts += 1
        rented_media_ids.add(media.product_id)
        return "auto lent", None

    config = SimpleNamespace(
        general=SimpleNamespace(
            poll_interval_secs=1.0,
            watch_product_ids=[],
            watch_categories=[],
        ),
        notification=SimpleNamespace(test_notification=False),
        gourou=SimpleNamespace(lendings_poll_interval_secs=0.0),
        external_auth=SimpleNamespace(auto_login=False, headless=True, timeout_secs=120.0, username=None, password=None, max_login_attempts=5),
    )
    monkeypatch.setattr(cli, "build_apprise", lambda config: object())
    monkeypatch.setattr(cli, "create_onleihe_client", lambda config: client)
    monkeypatch.setattr(cli, "build_gourou_client", lambda config: None)
    monkeypatch.setattr(cli, "login", lambda client, config: None)
    monkeypatch.setattr(cli, "fetch_all_watched_media", fetch_media)
    monkeypatch.setattr(cli, "maybe_lend_or_reserve", handle_media)
    monkeypatch.setattr(cli, "notify", lambda *args, **kwargs: next(notification_results))
    monkeypatch.setattr(
        cli,
        "time",
        SimpleNamespace(monotonic=lambda: 0.0, sleep=lambda secs: None),
    )

    with pytest.raises(EndTestLoop):
        cli.run_loop(config, SimpleNamespace(test_notification=False, once=False))

    assert lend_attempts == 1
    assert client.closed is True


def test_matches_filter_uses_title_subtitle_and_authors():
    item = MediaItem(
        id="book-1",
        product_id="book-1",
        title="Neutral",
        subtitle="Praxiswissen",
        media_type="E_BOOK",
        authors=["Ada Lovelace"],
    )

    assert matches_filter(item, ["ada"])
    assert matches_filter(item, ["praxis"])
    assert not matches_filter(item, ["python"])


@pytest.mark.parametrize(
    ("title", "subtitle", "keyword"),
    [
        ("111 Orte", "komplett überarbeitete Neuauflage", "arbeit"),
        ("ICH-POWER", "Wie du sie für dich entfaltest", "test"),
        ("African Comfort Food", "Authentisches Streetfood", "etf"),
        ("cafe\N{COMBINING ACUTE ACCENT}test", None, "test"),
        ("İtest", None, "test"),
    ],
)
def test_word_start_mode_rejects_keyword_inside_word(title, subtitle, keyword):
    item = MediaItem(
        id="book-1",
        product_id="book-1",
        title=title,
        subtitle=subtitle,
        media_type="E_BOOK",
    )

    assert matches_filter(item, [keyword], mode="contains")
    assert not matches_filter(item, [keyword], mode="word_start")


@pytest.mark.parametrize(
    ("title", "keyword"),
    [
        ("Finanzen verstehen", "finanz"),
        ("Arbeitsrecht kompakt", "arbeit"),
        ("ETF-Sparplan", "etf"),
        ("Mein_ETF_Sparplan", "etf"),
        ("VERMÖGEN aufbauen", "vermögen"),
    ],
)
def test_word_start_mode_matches_prefixes_after_separators(title, keyword):
    item = MediaItem(
        id="book-1",
        product_id="book-1",
        title=title,
        subtitle=None,
        media_type="E_BOOK",
    )

    assert matches_filter(item, [keyword], mode="word_start")


def test_available_media_reserves_when_lend_fails_because_unavailable():
    class Client:
        host = "example.onleihe.de"
        lent = False
        reserved = False

        def lend(self, product_id: str):
            self.lent = True
            raise OnleiheAPIError(
                "lend failed",
                status_code=409,
                payload={"messageId": "no-available-licences"},
            )

        def reserve(self, product_id: str):
            self.reserved = True
            return {}

        def maintenance_active(self):
            return False

    client = Client()
    handled_ids: set[str] = set()
    message, download_path = maybe_lend_or_reserve(
        watched_media(product_id="book-1", available=True),
        client,  # type: ignore[arg-type]
        config=None,  # type: ignore[arg-type]
        gourou_client=None,
        rented_media_ids=handled_ids,
        downloaded_media_ids=set(),
    )

    assert client.lent is True
    assert client.reserved is True
    assert handled_ids == {"book-1"}
    assert message == "auto reserved after lend failed"
    assert download_path is None


def test_available_media_does_not_reserve_during_maintenance():
    class Client:
        reserved = False

        def lend(self, product_id: str):
            raise OnleiheAPIError(
                "lend failed",
                status_code=409,
                payload={"messageId": "no-available-licences"},
            )

        def maintenance_active(self):
            return True

        def reserve(self, product_id: str):
            self.reserved = True

    client = Client()

    with pytest.raises(cli.MaintenanceDetectedError):
        maybe_lend_or_reserve(
            watched_media(product_id="book-1", available=True),
            client,  # type: ignore[arg-type]
            config=None,  # type: ignore[arg-type]
            gourou_client=None,
            rented_media_ids=set(),
            downloaded_media_ids=set(),
        )

    assert client.reserved is False


def test_any_lend_api_error_checks_maintenance():
    class Client:
        maintenance_checks = 0

        def lend(self, product_id: str):
            raise OnleiheAPIError("service unavailable", status_code=503)

        def maintenance_active(self):
            self.maintenance_checks += 1
            return True

    client = Client()

    with pytest.raises(cli.MaintenanceDetectedError):
        maybe_lend_or_reserve(
            watched_media(product_id="book-1", available=True),
            client,  # type: ignore[arg-type]
            config=None,  # type: ignore[arg-type]
            gourou_client=None,
            rented_media_ids=set(),
            downloaded_media_ids=set(),
        )

    assert client.maintenance_checks == 1


def test_available_media_does_not_reserve_after_auth_lend_error():
    class Client:
        reserved = False

        def lend(self, product_id: str):
            raise OnleiheAuthError("auth failed", status_code=401)

        def reserve(self, product_id: str):
            self.reserved = True

        def maintenance_active(self):
            return False

    client = Client()

    try:
        maybe_lend_or_reserve(
            watched_media(product_id="book-1", available=True),
            client,  # type: ignore[arg-type]
            config=None,  # type: ignore[arg-type]
            gourou_client=None,
            rented_media_ids=set(),
            downloaded_media_ids=set(),
        )
    except OnleiheAuthError:
        pass
    else:
        raise AssertionError("auth lend errors must be propagated")

    assert client.reserved is False


def test_lend_failure_allows_reserve_only_for_availability_errors():
    assert lend_failure_allows_reserve(
        OnleiheAPIError(
            "lend failed",
            status_code=409,
            payload={"messageId": "no-available-licences"},
        )
    )
    assert not lend_failure_allows_reserve(
        OnleiheAPIError("lend failed", status_code=409, payload={"message": "no licence available maybe"})
    )


def test_my_media_seed_primes_all_lendings_without_keyword_filter():
    class Client:
        def get_my_media_items(self, *, include_player_licences: bool = True):
            return [
                MediaItem(
                    id="loan-1",
                    product_id="loan-1",
                    title="Unrelated Loan",
                    subtitle=None,
                    media_type="E_BOOK",
                    authors=[],
                    lend_id="lend-1",
                )
            ]

    downloaded_ids: set[str] = set()
    count = process_my_media_downloads(
        Client(),  # type: ignore[arg-type]
        config=None,  # type: ignore[arg-type]
        apobj=None,  # type: ignore[arg-type]
        gourou_client=object(),  # type: ignore[arg-type]
        downloaded_media_ids=downloaded_ids,
        seed_only=True,
    )

    assert count == 1
    assert downloaded_ids == {"loan-1"}


def test_build_apprise_returns_none_without_targets():
    config = SimpleNamespace(
        notification=NotificationConfig(
            urls=[],
            apprise_config_path=None,
            test_notification=False,
            email=None,
        )
    )

    assert build_apprise(config) is None  # type: ignore[arg-type]


def test_init_config_existing_interactive_decline_keeps_file(tmp_path, monkeypatch):
    config_path = tmp_path / "onleiharr.toml"
    config_path.write_text("existing", encoding="utf-8")
    called = False

    def fake_wizard(path, *, version):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(cli, "run_first_start_wizard", fake_wizard)
    monkeypatch.setattr(sys, "stdin", TtyStringIO("\n"))
    monkeypatch.setattr(sys, "stdout", TtyStringIO())

    assert cli.main(["--init-config", "-c", str(config_path)]) == 0
    assert called is False
    assert config_path.read_text(encoding="utf-8") == "existing"


def test_init_config_existing_interactive_accept_runs_wizard(tmp_path, monkeypatch):
    config_path = tmp_path / "onleiharr.toml"
    config_path.write_text("existing", encoding="utf-8")
    called_with = None

    def fake_wizard(path, *, version):
        nonlocal called_with
        called_with = path
        path.write_text("new", encoding="utf-8")
        return True

    monkeypatch.setattr(cli, "run_first_start_wizard", fake_wizard)
    monkeypatch.setattr(sys, "stdin", TtyStringIO("y\n"))
    monkeypatch.setattr(sys, "stdout", TtyStringIO())

    assert cli.main(["--init-config", "-c", str(config_path)]) == 0
    assert called_with == config_path
    assert config_path.read_text(encoding="utf-8") == "new"


def test_init_config_existing_non_interactive_refuses_overwrite(tmp_path, monkeypatch):
    config_path = tmp_path / "onleiharr.toml"
    config_path.write_text("existing", encoding="utf-8")
    called = False

    def fake_wizard(path, *, version):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(cli, "run_first_start_wizard", fake_wizard)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    assert cli.main(["--init-config", "-c", str(config_path)]) == 1
    assert called is False
    assert config_path.read_text(encoding="utf-8") == "existing"


def test_invalid_config_interactive_decline_keeps_file(tmp_path, monkeypatch):
    config_path = tmp_path / "onleiharr.toml"
    config_path.write_text("watch_product_ids = ['legacy-url']\n", encoding="utf-8")
    called = False

    def fake_wizard(path, *, version):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(cli, "run_first_start_wizard", fake_wizard)
    monkeypatch.setattr(sys, "stdin", TtyStringIO("\n"))
    monkeypatch.setattr(sys, "stdout", TtyStringIO())

    assert cli.main(["--once", "-c", str(config_path)]) == 1
    assert called is False
    assert config_path.exists()


def test_invalid_config_interactive_accept_deletes_and_runs_wizard(tmp_path, monkeypatch):
    config_path = tmp_path / "onleiharr.toml"
    config_path.write_text("watch_product_ids = ['legacy-url']\n", encoding="utf-8")
    run_called = False

    def fake_wizard(path, *, version):
        assert not path.exists()
        path.write_text(
            """
[general]
poll_interval_secs = 300.0
watch_product_ids = []

[notification]
urls = []

[credentials]
host = "niedersachsen.onleihe.de"
onleihe_name = "Onleihe Niedersachsen"
library_name = "Stadtbibliothek Achim"
username = "user"
password = "secret"
""",
            encoding="utf-8",
        )
        return True

    def fake_run_loop(config, args):
        nonlocal run_called
        run_called = True

    monkeypatch.setattr(cli, "run_first_start_wizard", fake_wizard)
    monkeypatch.setattr(cli, "run_loop", fake_run_loop)
    monkeypatch.setattr(sys, "stdin", TtyStringIO("y\n"))
    monkeypatch.setattr(sys, "stdout", TtyStringIO())

    assert cli.main(["--once", "-c", str(config_path)]) == 0
    assert run_called is True
    assert "watch_product_ids = []" in config_path.read_text(encoding="utf-8")


def test_invalid_config_non_interactive_does_not_delete(tmp_path, monkeypatch):
    config_path = tmp_path / "onleiharr.toml"
    config_path.write_text("watch_product_ids = ['legacy-url']\n", encoding="utf-8")
    called = False

    def fake_wizard(path, *, version):
        nonlocal called
        called = True
        return True

    monkeypatch.setattr(cli, "run_first_start_wizard", fake_wizard)
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    monkeypatch.setattr(sys, "stdout", io.StringIO())

    assert cli.main(["--once", "-c", str(config_path)]) == 1
    assert called is False
    assert config_path.exists()


def watched_media(*, product_id: str, available: bool) -> WatchedMedia:
    return WatchedMedia(
        product_id=product_id,
        title="Test Book",
        url=f"https://example.onleihe.de/mymedia/mediadetail?productId={product_id}",
        media_type="E_AUDIO",
        authors=(),
        subtitle=None,
        publication_date=None,
        available=available,
        availability_text=None,
        acsm_url=None,
        source="test",
        keyword_required=False,
        keyword_matched=True,
    )


def test_parse_args_supports_explicit_watch_and_download_commands():
    watch = cli.parse_args(["-c", "config.toml", "watch", "--once"])
    watch_with_leading_option = cli.parse_args(["--once", "watch"])
    download = cli.parse_args(["download", "a" * 24, "-c", "config.toml"])

    assert watch.command == "watch"
    assert watch.once is True
    assert watch.config_path.name == "config.toml"
    assert watch_with_leading_option.once is True
    assert download.command == "download"
    assert download.target == "a" * 24
    assert download.config_path.name == "config.toml"


def test_install_user_systemd_uses_explicit_watch_command(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(cli.subprocess, "run", lambda *args, **kwargs: None)

    cli.install_user_systemd(tmp_path / "onleiharr.toml")

    unit = (tmp_path / ".config/systemd/user/onleiharr.service").read_text(encoding="utf-8")
    assert f"ExecStart=/usr/bin/onleiharr -c {tmp_path / 'onleiharr.toml'} watch" in unit


def test_implicit_watcher_warns_but_explicit_watch_does_not(tmp_path, monkeypatch, caplog):
    config = SimpleNamespace(
        general=SimpleNamespace(poll_interval_secs=300.0, watch_product_ids=[], watch_categories=[]),
        notification=SimpleNamespace(test_notification=False),
        config_path=tmp_path / "config.toml",
    )
    monkeypatch.setattr(cli, "ensure_config_or_exit", lambda *args, **kwargs: None)
    monkeypatch.setattr(cli, "load_config", lambda path: config)
    monkeypatch.setattr(cli, "run_startup_checks", lambda config: None)
    monkeypatch.setattr(cli, "run_loop", lambda config, args: None)

    with caplog.at_level("WARNING"):
        assert cli.main(["-c", str(tmp_path / "config.toml")]) == 0
    assert "without the 'watch' command is deprecated" in caplog.text
    assert f"onleiharr --install-as-user-systemd -c {tmp_path / 'config.toml'}" in caplog.text
    assert "systemctl --user daemon-reload" in caplog.text
    assert "systemctl --user restart onleiharr" in caplog.text

    caplog.clear()
    with caplog.at_level("WARNING"):
        assert cli.main(["watch", "-c", str(tmp_path / "config.toml")]) == 0
    assert "without the 'watch' command is deprecated" not in caplog.text


def test_parse_download_target_accepts_id_and_matching_url():
    product_id = "69b3ed6bc56755bf97cb3b9a"

    assert cli.parse_download_target(product_id.upper(), expected_host="example.onleihe.de") == product_id
    assert (
        cli.parse_download_target(
            f"https://example.onleihe.de/search/mediadetail?productId={product_id}",
            expected_host="example.onleihe.de",
        )
        == product_id
    )


def test_parse_download_target_rejects_other_host_and_ambiguous_id():
    product_id = "69b3ed6bc56755bf97cb3b9a"

    with pytest.raises(ValueError, match="does not match"):
        cli.parse_download_target(
            f"https://other.onleihe.de/search?productId={product_id}",
            expected_host="example.onleihe.de",
        )
    with pytest.raises(ValueError, match="exactly one"):
        cli.parse_download_target(
            f"https://example.onleihe.de/search?productId={product_id}&productId={'a' * 24}",
            expected_host="example.onleihe.de",
        )


def _download_config(tmp_path):
    return SimpleNamespace(
        credentials=SimpleNamespace(host="example.onleihe.de"),
        gourou=SimpleNamespace(
            remove_drm=False,
            remove_drm_ack=None,
            download_permissions=0o644,
        ),
    )


def _download_product(product_id: str = "69b3ed6bc56755bf97cb3b9a") -> ProductDetails:
    return ProductDetails(
        id=product_id,
        product_id=product_id,
        title="Test Book",
        subtitle=None,
        media_type="E_BOOK",
        availability={"isAvailable": True},
    )


class _ReadyGourou:
    def __init__(self, config):
        self.config = config

    def validate_download_readiness(self):
        return None


def test_download_existing_lending_is_not_returned(tmp_path, monkeypatch):
    product = _download_product()

    class Client:
        def __init__(self):
            self.lend_calls = 0
            self.return_calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get_product(self, product_id, *, include_user_context=True):
            return product

        def get_my_media_items(self, *, include_player_licences=True):
            return [
                MediaItem(
                    id=product.id,
                    product_id=product.product_id,
                    title=product.title,
                    subtitle=None,
                    media_type="E_BOOK",
                    lend_id="existing-lend",
                    acsm_url="https://download.invalid/book.acsm",
                )
            ]

        def lend(self, product_id):
            self.lend_calls += 1

        def return_lend(self, lend_id):
            self.return_calls += 1

    client = Client()
    monkeypatch.setattr(cli, "GourouClient", _ReadyGourou)
    monkeypatch.setattr(cli, "create_onleihe_client", lambda config: client)
    monkeypatch.setattr(cli, "login", lambda client, config: None)
    monkeypatch.setattr(cli, "download_acsm_with_gourou", lambda **kwargs: (True, tmp_path / "book.epub"))

    assert cli.run_download_command(_download_config(tmp_path), product.product_id) == 0
    assert client.lend_calls == 0
    assert client.return_calls == 0


@pytest.mark.parametrize("downloaded, expected_status", [(True, 0), (False, 1)])
def test_download_new_lending_is_returned_even_after_download_failure(
    tmp_path, monkeypatch, downloaded, expected_status
):
    product = _download_product()

    class Client:
        def __init__(self):
            self.lent = False
            self.returned = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get_product(self, product_id, *, include_user_context=True):
            return product

        def get_my_media_items(self, *, include_player_licences=True):
            if self.returned or not self.lent:
                return []
            return [
                MediaItem(
                    id=product.id,
                    product_id=product.product_id,
                    title=product.title,
                    subtitle=None,
                    media_type="E_BOOK",
                    lend_id="new-lend",
                )
            ]

        def lend(self, product_id):
            self.lent = True
            return {"lendId": "new-lend", "acsm_url": "https://download.invalid/book.acsm"}

        def return_lend(self, lend_id):
            assert lend_id == "new-lend"
            self.returned = True
            return {}

    client = Client()
    monkeypatch.setattr(cli, "GourouClient", _ReadyGourou)
    monkeypatch.setattr(cli, "create_onleihe_client", lambda config: client)
    monkeypatch.setattr(cli, "login", lambda client, config: None)
    monkeypatch.setattr(
        cli,
        "download_acsm_with_gourou",
        lambda **kwargs: (downloaded, tmp_path / "book.epub" if downloaded else None),
    )

    assert cli.run_download_command(_download_config(tmp_path), product.product_id) == expected_status
    assert client.returned is True


def test_download_readiness_failure_happens_before_login(tmp_path, monkeypatch):
    class NotReadyGourou(_ReadyGourou):
        def validate_download_readiness(self):
            raise cli.GourouError("not activated")

    monkeypatch.setattr(cli, "GourouClient", NotReadyGourou)
    monkeypatch.setattr(
        cli,
        "create_onleihe_client",
        lambda config: pytest.fail("client must not be created when Gourou is not ready"),
    )

    assert cli.run_download_command(_download_config(tmp_path), "69b3ed6bc56755bf97cb3b9a") == 1


def test_select_download_media_offers_interactive_container_choice(monkeypatch):
    first = _download_product("a" * 24)
    second = _download_product("b" * 24)
    container = _download_product("c" * 24)
    container.raw = {"product": {"isContainer": True}}
    container.included_media = [first, second]
    monkeypatch.setattr(sys, "stdin", TtyStringIO())
    monkeypatch.setattr(sys, "stdout", TtyStringIO())

    assert cli.select_download_media(container, input_func=lambda prompt: "2") is second


def test_download_without_lend_id_does_not_start_acsm_download(tmp_path, monkeypatch):
    product = _download_product()

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get_product(self, product_id, *, include_user_context=True):
            return product

        def get_my_media_items(self, *, include_player_licences=True):
            return []

        def lend(self, product_id):
            return {"acsm_url": "https://download.invalid/book.acsm"}

    monkeypatch.setattr(cli, "GourouClient", _ReadyGourou)
    monkeypatch.setattr(cli, "create_onleihe_client", lambda config: Client())
    monkeypatch.setattr(cli, "login", lambda client, config: None)
    monkeypatch.setattr(cli.time, "sleep", lambda interval: None)
    monkeypatch.setattr(
        cli,
        "download_acsm_with_gourou",
        lambda **kwargs: pytest.fail("download must not start without a lending ID"),
    )

    assert cli.run_download_command(_download_config(tmp_path), product.product_id) == 1


def test_download_reports_failed_return_and_keeps_failed_status(tmp_path, monkeypatch):
    product = _download_product()

    class Client:
        def __init__(self):
            self.lent = False

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def get_product(self, product_id, *, include_user_context=True):
            return product

        def get_my_media_items(self, *, include_player_licences=True):
            return []

        def lend(self, product_id):
            self.lent = True
            return {"lendId": "new-lend", "acsm_url": "https://download.invalid/book.acsm"}

        def return_lend(self, lend_id):
            raise OnleiheAPIError("return failed", status_code=500)

    monkeypatch.setattr(cli, "GourouClient", _ReadyGourou)
    monkeypatch.setattr(cli, "create_onleihe_client", lambda config: Client())
    monkeypatch.setattr(cli, "login", lambda client, config: None)
    monkeypatch.setattr(cli, "download_acsm_with_gourou", lambda **kwargs: (True, tmp_path / "book.epub"))

    assert cli.run_download_command(_download_config(tmp_path), product.product_id) == 1


def test_login_openid_valid_session_refresh_succeeds_without_browser(monkeypatch):
    from onleiharr._vendor.onleihe import SessionState
    from onleiharr.cli import login as cli_login

    class Client:
        onleihe_id = None
        library_id = None
        session = None
        session_callback = None

        def refresh(self):
            pass

    client = Client()

    config = SimpleNamespace(
        credentials=SimpleNamespace(auth_type="open_id", session_path=None),
        external_auth=SimpleNamespace(auto_login=True, headless=True, timeout_secs=120.0, username="u", password="p", max_login_attempts=5),
        config_path=Path("/tmp/test.toml"),
    )

    session_path = Path("/tmp/test_session.json")
    monkeypatch.setattr(cli, "default_session_path", lambda config_path: session_path)
    monkeypatch.setattr(cli, "load_session", lambda path: SessionState(access_token="existing", refresh_token="refresh", user_id="uid", profile_id="pid", library_id="lid", onleihe_id="oid"))

    cli_login(client, config)

    assert client.onleihe_id == "oid"
    assert client.library_id == "lid"


def test_login_openid_missing_session_triggers_auto_login(monkeypatch):
    from onleiharr._vendor.onleihe import OnleiheAuthError, SessionState
    from onleiharr.cli import login as cli_login

    client = SimpleNamespace(
        onleihe_id=None,
        library_id=None,
        session=None,
        session_callback=None,
    )

    config = SimpleNamespace(
        credentials=SimpleNamespace(auth_type="open_id", session_path=None),
        external_auth=SimpleNamespace(auto_login=True, headless=True, timeout_secs=120.0, username="extuser", password="extpass", max_login_attempts=5),
        config_path=Path("/tmp/test.toml"),
    )

    session_path = Path("/tmp/test_session_missing.json")

    monkeypatch.setattr(cli, "default_session_path", lambda config_path: session_path)
    monkeypatch.setattr(cli, "load_session", lambda path: (_ for _ in ()).throw(OnleiheAuthError("session not found")))

    auto_login_calls = []

    def fake_external_login_automated(client, *, username, password, headless, timeout_secs):
        auto_login_calls.append({
            "username": username,
            "password": password,
            "headless": headless,
            "timeout_secs": timeout_secs,
        })
        client.session = SessionState(access_token="new", refresh_token="newrefresh", user_id="uid", profile_id="pid", library_id="lid", onleihe_id="oid")
        client.onleihe_id = "oid"
        client.library_id = "lid"
        return client.session

    monkeypatch.setattr(cli, "external_login_automated", fake_external_login_automated)

    def fake_save_session(path, session):
        pass

    monkeypatch.setattr(cli, "save_session", fake_save_session)

    cli_login(client, config)

    assert client.onleihe_id == "oid"
    assert client.library_id == "lid"
    assert len(auto_login_calls) == 1
    assert auto_login_calls[0]["username"] == "extuser"
    assert auto_login_calls[0]["password"] == "extpass"
    assert auto_login_calls[0]["headless"] is True
    assert auto_login_calls[0]["timeout_secs"] == 120.0


def test_login_openid_disabled_auto_login_preserves_error(monkeypatch):
    from onleiharr._vendor.onleihe import OnleiheAuthError
    from onleiharr.cli import login as cli_login

    client = SimpleNamespace(
        onleihe_id=None,
        library_id=None,
        session=None,
        session_callback=None,
    )

    config = SimpleNamespace(
        credentials=SimpleNamespace(auth_type="open_id", session_path=None),
        external_auth=SimpleNamespace(auto_login=False, headless=True, timeout_secs=120.0, username=None, password=None, max_login_attempts=5),
        config_path=Path("/tmp/test.toml"),
    )

    session_path = Path("/tmp/test_session_missing.json")

    monkeypatch.setattr(cli, "default_session_path", lambda config_path: session_path)
    monkeypatch.setattr(cli, "load_session", lambda path: (_ for _ in ()).throw(OnleiheAuthError("session not found")))

    with pytest.raises(OnleiheAuthError, match="session not found"):
        cli_login(client, config)


def test_login_openid_auto_login_failure_preserves_original_error(monkeypatch):
    from onleiharr._vendor.onleihe import OnleiheAuthError
    from onleiharr.cli import login as cli_login

    client = SimpleNamespace(
        onleihe_id=None,
        library_id=None,
        session=None,
        session_callback=None,
    )

    config = SimpleNamespace(
        credentials=SimpleNamespace(auth_type="open_id", session_path=None),
        external_auth=SimpleNamespace(auto_login=True, headless=True, timeout_secs=120.0, username="extuser", password="extpass", max_login_attempts=5),
        config_path=Path("/tmp/test.toml"),
    )

    session_path = Path("/tmp/test_session_missing.json")

    monkeypatch.setattr(cli, "default_session_path", lambda config_path: session_path)
    monkeypatch.setattr(cli, "load_session", lambda path: (_ for _ in ()).throw(OnleiheAuthError("session not found")))

    def fake_external_login_automated(client, *, username, password, headless, timeout_secs):
        raise OnleiheAuthError("browser login failed")

    monkeypatch.setattr(cli, "external_login_automated", fake_external_login_automated)

    with pytest.raises(OnleiheAuthError, match="browser login failed"):
        cli_login(client, config)


def test_login_openid_corrupt_session_triggers_auto_login(monkeypatch):
    from onleiharr._vendor.onleihe import OnleiheAuthError, SessionState
    from onleiharr.cli import login as cli_login

    client = SimpleNamespace(
        onleihe_id=None,
        library_id=None,
        session=None,
        session_callback=None,
    )

    config = SimpleNamespace(
        credentials=SimpleNamespace(auth_type="open_id", session_path=None),
        external_auth=SimpleNamespace(auto_login=True, headless=True, timeout_secs=120.0, username="extuser", password="extpass", max_login_attempts=5),
        config_path=Path("/tmp/test.toml"),
    )

    session_path = Path("/tmp/test_session_corrupt.json")

    monkeypatch.setattr(cli, "default_session_path", lambda config_path: session_path)
    monkeypatch.setattr(cli, "load_session", lambda path: (_ for _ in ()).throw(OnleiheAuthError("invalid JSON")))

    auto_login_calls = []

    def fake_external_login_automated(client, *, username, password, headless, timeout_secs):
        auto_login_calls.append(True)
        client.session = SessionState(access_token="new", refresh_token="newrefresh", user_id="uid", profile_id="pid", library_id="lid", onleihe_id="oid")
        client.onleihe_id = "oid"
        client.library_id = "lid"
        return client.session

    monkeypatch.setattr(cli, "external_login_automated", fake_external_login_automated)

    def fake_save_session(path, session):
        pass

    monkeypatch.setattr(cli, "save_session", fake_save_session)

    cli_login(client, config)

    assert client.onleihe_id == "oid"
    assert client.library_id == "lid"
    assert len(auto_login_calls) == 1


def test_login_openid_refresh_failure_triggers_auto_login(monkeypatch):
    from onleiharr._vendor.onleihe import OnleiheAuthError, SessionState
    from onleiharr.cli import login as cli_login

    class Client:
        onleihe_id = "existing-oid"
        library_id = "existing-lid"
        session = SessionState(access_token="stale", refresh_token="stale-refresh", user_id="uid", profile_id="pid", library_id="lid", onleihe_id="oid")
        session_callback = None

        def refresh(self):
            raise OnleiheAuthError("refresh rejected", status_code=401)

    client = Client()

    config = SimpleNamespace(
        credentials=SimpleNamespace(auth_type="open_id", session_path=None),
        external_auth=SimpleNamespace(auto_login=True, headless=True, timeout_secs=120.0, username="extuser", password="extpass", max_login_attempts=5),
        config_path=Path("/tmp/test.toml"),
    )

    session_path = Path("/tmp/test_session_refresh.json")

    monkeypatch.setattr(cli, "default_session_path", lambda config_path: session_path)
    monkeypatch.setattr(cli, "load_session", lambda path: client.session)

    auto_login_calls = []

    def fake_external_login_automated(client, *, username, password, headless, timeout_secs):
        auto_login_calls.append(True)
        client.session = SessionState(access_token="fresh", refresh_token="fresh-refresh", user_id="uid", profile_id="pid", library_id="new-lid", onleihe_id="new-oid")
        client.onleihe_id = "new-oid"
        client.library_id = "new-lid"
        return client.session

    monkeypatch.setattr(cli, "external_login_automated", fake_external_login_automated)

    def fake_save_session(path, session):
        pass

    monkeypatch.setattr(cli, "save_session", fake_save_session)

    cli_login(client, config)

    assert client.onleihe_id == "new-oid"
    assert client.library_id == "new-lid"
    assert len(auto_login_calls) == 1


def test_login_openid_run_loop_sends_manual_notification_after_auto_login_failure(monkeypatch):
    from onleiharr._vendor.onleihe import OnleiheAuthError, SessionState
    from onleiharr.cli import login as cli_login

    class Client:
        closed = False
        onleihe_id = None
        library_id = None
        session = None
        session_callback = None

        def close(self):
            self.closed = True

        def maintenance_active(self):
            return False

    client = Client()

    config = SimpleNamespace(
        credentials=SimpleNamespace(auth_type="open_id", session_path=None),
        general=SimpleNamespace(
            poll_interval_secs=300.0,
            watch_product_ids=[],
            watch_categories=[],
        ),
        notification=SimpleNamespace(test_notification=False),
        gourou=SimpleNamespace(lendings_poll_interval_secs=0.0),
        external_auth=SimpleNamespace(auto_login=True, headless=True, timeout_secs=120.0, username="extuser", password="extpass", max_login_attempts=5),
    )

    monkeypatch.setattr(cli, "build_apprise", lambda config: None)
    monkeypatch.setattr(cli, "create_onleihe_client", lambda config: client)
    monkeypatch.setattr(cli, "build_gourou_client", lambda config: None)

    def fake_login(client, config):
        raise OnleiheAuthError("auto-login failed")

    monkeypatch.setattr(cli, "login", fake_login)

    notifications = []

    def fake_fetch_all_watched_media(client, config, *, log_summary=False):
        raise OnleiheAuthError("refresh rejected", status_code=401)

    monkeypatch.setattr(cli, "fetch_all_watched_media", fake_fetch_all_watched_media)

    monkeypatch.setattr(
        cli,
        "notify_external_auth_required",
        lambda apobj, config: notifications.append(config),
    )

    with pytest.raises(OnleiheAuthError):
        cli.run_loop(config, SimpleNamespace(test_notification=False, once=True))

    assert notifications == [config]
    assert client.closed is True


def test_login_upa_unchanged(monkeypatch):
    from onleiharr.cli import login as cli_login

    class Client:
        onleihe_id = None
        library_id = None

        def login(self, username, password, **kwargs):
            login_args.append({"username": username, "password": password, "kwargs": kwargs})

    client = Client()
    login_args = []

    config = SimpleNamespace(
        credentials=SimpleNamespace(
            auth_type="upa",
            username="upauser",
            password="upapass",
            host="example.onleihe.de",
            onleihe_name=None,
            onleihe_id="onleihe-id",
            library_name=None,
            library_id="library-id",
            session_path=None,
        ),
        external_auth=SimpleNamespace(auto_login=True, headless=True, timeout_secs=120.0, username="extuser", password="extpass", max_login_attempts=5),
    )

    cli_login(client, config)

    assert len(login_args) == 1
    assert login_args[0]["username"] == "upauser"
    assert login_args[0]["password"] == "upapass"
    assert login_args[0]["kwargs"]["onleihe_id"] == "onleihe-id"
    assert login_args[0]["kwargs"]["library_id"] == "library-id"


def test_recover_authentication_upa_first_failure_succeeds(monkeypatch):
    from onleiharr.cli import recover_authentication

    client = SimpleNamespace()
    login_calls = []

    def fake_login(client, config):
        login_calls.append(True)

    monkeypatch.setattr(cli, "login", fake_login)

    config = SimpleNamespace(
        credentials=SimpleNamespace(auth_type="upa"),
        external_auth=SimpleNamespace(auto_login=False, headless=True, timeout_secs=120.0, username=None, password=None, max_login_attempts=5),
    )

    result = recover_authentication(
        client,
        config,
        failed_operation="watch poll",
        login_attempts=0,
    )

    assert result == 1
    assert len(login_calls) == 1


def test_recover_authentication_upa_second_failure_returns_none(monkeypatch):
    from onleiharr.cli import recover_authentication

    client = SimpleNamespace()
    login_calls = []

    def fake_login(client, config):
        login_calls.append(True)

    monkeypatch.setattr(cli, "login", fake_login)

    config = SimpleNamespace(
        credentials=SimpleNamespace(auth_type="upa"),
        external_auth=SimpleNamespace(auto_login=False, headless=True, timeout_secs=120.0, username=None, password=None, max_login_attempts=5),
    )

    result = recover_authentication(
        SimpleNamespace(session_callback=None),
        config,
        failed_operation="watch poll",
        login_attempts=5,
    )

    assert result is None
    assert len(login_calls) == 0


def test_recover_authentication_upa_non_upa_returns_none(monkeypatch):
    from onleiharr.cli import recover_authentication

    config = SimpleNamespace(
        credentials=SimpleNamespace(auth_type="open_id"),
        external_auth=SimpleNamespace(auto_login=False, headless=True, timeout_secs=120.0, username=None, password=None, max_login_attempts=5),
    )

    result = recover_authentication(
        SimpleNamespace(session_callback=None),
        config,
        failed_operation="watch poll",
        login_attempts=0,
    )

    assert result is None


def test_recover_authentication_oidc_auto_login_enabled(monkeypatch):
    from onleiharr._vendor.onleihe import SessionState
    from onleiharr.cli import recover_authentication

    client = SimpleNamespace(session_callback=None)
    auto_login_calls = []

    def fake_external_login_automated(client, *, username, password, headless, timeout_secs):
        auto_login_calls.append({
            "username": username,
            "password": password,
            "headless": headless,
            "timeout_secs": timeout_secs,
        })
        client.session = SessionState(access_token="new", refresh_token="newrefresh", user_id="uid", profile_id="pid", library_id="lid", onleihe_id="oid")
        client.onleihe_id = "oid"
        client.library_id = "lid"
        return client.session

    monkeypatch.setattr(cli, "external_login_automated", fake_external_login_automated)

    config = SimpleNamespace(
        credentials=SimpleNamespace(auth_type="open_id"),
        external_auth=SimpleNamespace(auto_login=True, headless=False, timeout_secs=60.0, username="extuser", password="extpass", max_login_attempts=5),
    )

    result = recover_authentication(
        client,
        config,
        failed_operation="watch poll",
        login_attempts=0,
    )

    assert result == 1
    assert len(auto_login_calls) == 1
    assert auto_login_calls[0]["username"] == "extuser"
    assert auto_login_calls[0]["password"] == "extpass"
    assert auto_login_calls[0]["headless"] is False
    assert auto_login_calls[0]["timeout_secs"] == 60.0


def test_recover_authentication_oidc_auto_login_disabled_returns_none(monkeypatch):
    from onleiharr.cli import recover_authentication

    config = SimpleNamespace(
        credentials=SimpleNamespace(auth_type="open_id"),
        external_auth=SimpleNamespace(auto_login=False, headless=True, timeout_secs=120.0, username=None, password=None, max_login_attempts=5),
    )

    result = recover_authentication(
        SimpleNamespace(),
        config,
        failed_operation="watch poll",
        login_attempts=0,
    )

    assert result is None


def test_recover_authentication_oidc_max_attempts_raises(monkeypatch):
    from onleiharr._vendor.onleihe import OnleiheAuthError
    from onleiharr.cli import recover_authentication

    auto_login_calls = []

    def fake_external_login_automated(client, *, username, password, headless, timeout_secs):
        auto_login_calls.append(True)
        return SessionState(access_token="new", refresh_token="newrefresh", user_id="uid", profile_id="pid", library_id="lid", onleihe_id="oid")

    monkeypatch.setattr(cli, "external_login_automated", fake_external_login_automated)

    config = SimpleNamespace(
        credentials=SimpleNamespace(auth_type="open_id"),
        external_auth=SimpleNamespace(auto_login=True, headless=True, timeout_secs=120.0, username="extuser", password="extpass", max_login_attempts=5),
    )

    with pytest.raises(OnleiheAuthError, match="OIDC automated login failed after 5 attempts"):
        recover_authentication(
            SimpleNamespace(),
            config,
            failed_operation="watch poll",
            login_attempts=5,
        )

    assert len(auto_login_calls) == 0


def test_run_loop_oidc_auto_login_recovery_on_watch_poll(monkeypatch):
    class EndTestLoop(Exception):
        pass

    class Client:
        closed = False
        onleihe_id = None
        library_id = None
        session = None
        session_callback = None

        def close(self):
            self.closed = True

        def maintenance_active(self):
            return False

    client = Client()
    auto_login_calls = []

    from onleiharr._vendor.onleihe import SessionState

    def fake_external_login_automated(client, *, username, password, headless, timeout_secs):
        auto_login_calls.append(True)
        return SessionState(access_token="new", refresh_token="newrefresh", user_id="uid", profile_id="pid", library_id="lid", onleihe_id="oid")

    monkeypatch.setattr(cli, "external_login_automated", fake_external_login_automated)

    config = SimpleNamespace(
        credentials=SimpleNamespace(auth_type="open_id", session_path=None),
        general=SimpleNamespace(
            poll_interval_secs=300.0,
            watch_product_ids=[],
            watch_categories=[],
        ),
        notification=SimpleNamespace(test_notification=False),
        gourou=SimpleNamespace(lendings_poll_interval_secs=0.0),
        external_auth=SimpleNamespace(auto_login=True, headless=True, timeout_secs=120.0, username="extuser", password="extpass", max_login_attempts=5),
        config_path=Path("/tmp/test.toml"),
    )

    monkeypatch.setattr(cli, "build_apprise", lambda config: None)
    monkeypatch.setattr(cli, "create_onleihe_client", lambda config: client)
    monkeypatch.setattr(cli, "build_gourou_client", lambda config: None)
    monkeypatch.setattr(cli, "login", lambda client, config: None)
    monkeypatch.setattr(
        cli,
        "time",
        SimpleNamespace(monotonic=lambda: 0.0, sleep=lambda secs: None),
    )

    fetch_calls = []

    def fake_fetch_all_watched_media(client, config, *, log_summary=False):
        fetch_calls.append(True)
        if len(fetch_calls) == 1:
            from onleiharr._vendor.onleihe import OnleiheAuthError
            raise OnleiheAuthError("refresh rejected", status_code=401)
        if len(fetch_calls) >= 3:
            raise EndTestLoop
        return cli.WatchPollResult(media=[])

    monkeypatch.setattr(cli, "fetch_all_watched_media", fake_fetch_all_watched_media)

    with pytest.raises(EndTestLoop):
        cli.run_loop(config, SimpleNamespace(test_notification=False, once=False))

    assert len(auto_login_calls) == 1
    assert len(fetch_calls) == 3
    assert client.closed is True


def test_run_loop_oidc_auto_login_second_auth_failure_exits(monkeypatch):
    from onleiharr._vendor.onleihe import OnleiheAuthError, SessionState

    class Client:
        closed = False
        session_callback = None

        def close(self):
            self.closed = True

        def maintenance_active(self):
            return False

    client = Client()

    def fake_external_login_automated(client, *, username, password, headless, timeout_secs):
        return SessionState(access_token="new", refresh_token="newrefresh", user_id="uid", profile_id="pid", library_id="lid", onleihe_id="oid")

    monkeypatch.setattr(cli, "external_login_automated", fake_external_login_automated)

    config = SimpleNamespace(
        credentials=SimpleNamespace(auth_type="open_id", session_path=None),
        general=SimpleNamespace(
            poll_interval_secs=300.0,
            watch_product_ids=[],
            watch_categories=[],
        ),
        notification=SimpleNamespace(test_notification=False),
        gourou=SimpleNamespace(lendings_poll_interval_secs=0.0),
        external_auth=SimpleNamespace(auto_login=True, headless=True, timeout_secs=120.0, username="extuser", password="extpass", max_login_attempts=5),
        config_path=Path("/tmp/test.toml"),
    )

    monkeypatch.setattr(cli, "build_apprise", lambda config: None)
    monkeypatch.setattr(cli, "create_onleihe_client", lambda config: client)
    monkeypatch.setattr(cli, "build_gourou_client", lambda config: None)
    monkeypatch.setattr(cli, "login", lambda client, config: None)

    def fake_fetch_all_watched_media(client, config, *, log_summary=False):
        raise OnleiheAuthError("refresh rejected", status_code=401)

    monkeypatch.setattr(cli, "fetch_all_watched_media", fake_fetch_all_watched_media)

    with pytest.raises(OnleiheAuthError):
        cli.run_loop(config, SimpleNamespace(test_notification=False, once=True))

    assert client.closed is True


def test_run_loop_oidc_auto_login_disabled_preserves_manual_recovery(monkeypatch):
    from onleiharr._vendor.onleihe import OnleiheAuthError, SessionState

    class Client:
        closed = False
        session_callback = None

        def close(self):
            self.closed = True

        def maintenance_active(self):
            return False

    client = Client()
    notifications = []

    config = SimpleNamespace(
        credentials=SimpleNamespace(auth_type="open_id", session_path=None),
        general=SimpleNamespace(
            poll_interval_secs=300.0,
            watch_product_ids=[],
            watch_categories=[],
        ),
        notification=SimpleNamespace(test_notification=False),
        gourou=SimpleNamespace(lendings_poll_interval_secs=0.0),
        external_auth=SimpleNamespace(auto_login=False, headless=True, timeout_secs=120.0, username=None, password=None, max_login_attempts=5),
        config_path=Path("/tmp/test.toml"),
    )

    monkeypatch.setattr(cli, "build_apprise", lambda config: None)
    monkeypatch.setattr(cli, "create_onleihe_client", lambda config: client)
    monkeypatch.setattr(cli, "build_gourou_client", lambda config: None)
    monkeypatch.setattr(cli, "login", lambda client, config: None)

    def fake_fetch_all_watched_media(client, config, *, log_summary=False):
        raise OnleiheAuthError("refresh rejected", status_code=401)

    monkeypatch.setattr(cli, "fetch_all_watched_media", fake_fetch_all_watched_media)

    monkeypatch.setattr(
        cli,
        "notify_external_auth_required",
        lambda apobj, config: notifications.append(config),
    )

    with pytest.raises(OnleiheAuthError):
        cli.run_loop(config, SimpleNamespace(test_notification=False, once=True))

    assert notifications == [config]
    assert client.closed is True


def test_run_loop_oidc_auto_login_reset_after_successful_cycle(monkeypatch):
    class EndTestLoop(Exception):
        pass

    from onleiharr._vendor.onleihe import OnleiheAuthError, SessionState

    class Client:
        closed = False
        session_callback = None

        def close(self):
            self.closed = True

        def maintenance_active(self):
            return False

    client = Client()
    auto_login_calls = []

    def fake_external_login_automated(client, *, username, password, headless, timeout_secs):
        auto_login_calls.append(True)
        return SessionState(access_token="new", refresh_token="newrefresh", user_id="uid", profile_id="pid", library_id="lid", onleihe_id="oid")

    monkeypatch.setattr(cli, "external_login_automated", fake_external_login_automated)

    fetch_results = iter(["auth", "success", "auth", "success"])

    config = SimpleNamespace(
        credentials=SimpleNamespace(auth_type="open_id", session_path=None),
        general=SimpleNamespace(
            poll_interval_secs=300.0,
            watch_product_ids=[],
            watch_categories=[],
        ),
        notification=SimpleNamespace(test_notification=False),
        gourou=SimpleNamespace(lendings_poll_interval_secs=0.0),
        external_auth=SimpleNamespace(auto_login=True, headless=True, timeout_secs=120.0, username="extuser", password="extpass", max_login_attempts=5),
        config_path=Path("/tmp/test.toml"),
    )

    monkeypatch.setattr(cli, "build_apprise", lambda config: None)
    monkeypatch.setattr(cli, "create_onleihe_client", lambda config: client)
    monkeypatch.setattr(cli, "build_gourou_client", lambda config: None)
    monkeypatch.setattr(cli, "login", lambda client, config: None)
    monkeypatch.setattr(
        cli,
        "time",
        SimpleNamespace(monotonic=lambda: 0.0, sleep=lambda secs: None),
    )

    def fake_fetch_all_watched_media(client, config, *, log_summary=False):
        try:
            result = next(fetch_results)
        except StopIteration:
            raise EndTestLoop from None
        if result == "auth":
            raise OnleiheAuthError("refresh rejected", status_code=401)
        return cli.WatchPollResult(media=[])

    monkeypatch.setattr(cli, "fetch_all_watched_media", fake_fetch_all_watched_media)

    with pytest.raises(EndTestLoop):
        cli.run_loop(config, SimpleNamespace(test_notification=False, once=False))

    assert len(auto_login_calls) == 2


def test_recover_authentication_oidc_browser_failure_propagates(monkeypatch):
    from onleiharr._vendor.onleihe import OnleiheAuthError, SessionState
    from onleiharr.cli import recover_authentication

    def fake_external_login_automated(client, *, username, password, headless, timeout_secs):
        raise OnleiheAuthError("browser login failed")

    monkeypatch.setattr(cli, "external_login_automated", fake_external_login_automated)

    config = SimpleNamespace(
        credentials=SimpleNamespace(auth_type="open_id"),
        external_auth=SimpleNamespace(auto_login=True, headless=True, timeout_secs=120.0, username="extuser", password="extpass", max_login_attempts=5),
    )

    with pytest.raises(OnleiheAuthError, match="browser login failed"):
        recover_authentication(
            SimpleNamespace(),
            config,
            failed_operation="watch poll",
            recovery_already_attempted=False,
        )


def test_recover_authentication_oidc_max_login_attempts_exceeded(monkeypatch):
    from onleiharr._vendor.onleihe import OnleiheAuthError
    from onleiharr.cli import recover_authentication

    def fake_external_login_automated(client, *, username, password, headless, timeout_secs):
        raise OnleiheAuthError("browser login failed")

    monkeypatch.setattr(cli, "external_login_automated", fake_external_login_automated)

    config = SimpleNamespace(
        credentials=SimpleNamespace(auth_type="open_id"),
        external_auth=SimpleNamespace(auto_login=True, headless=True, timeout_secs=120.0, username="extuser", password="extpass", max_login_attempts=5),
    )

    with pytest.raises(OnleiheAuthError, match="OIDC automated login failed after 5 attempts"):
        recover_authentication(
            SimpleNamespace(),
            config,
            failed_operation="watch poll",
            login_attempts=5,
        )


def test_recover_authentication_oidc_max_login_attempts_succeeds_before_limit(monkeypatch):
    from onleiharr._vendor.onleihe import SessionState
    from onleiharr.cli import recover_authentication

    auto_login_calls = []

    def fake_external_login_automated(client, *, username, password, headless, timeout_secs):
        auto_login_calls.append(True)
        client.session = SessionState(access_token="new", refresh_token="newrefresh", user_id="uid", profile_id="pid", library_id="lid", onleihe_id="oid")
        client.onleihe_id = "oid"
        client.library_id = "lid"
        return client.session

    monkeypatch.setattr(cli, "external_login_automated", fake_external_login_automated)

    config = SimpleNamespace(
        credentials=SimpleNamespace(auth_type="open_id"),
        external_auth=SimpleNamespace(auto_login=True, headless=True, timeout_secs=120.0, username="extuser", password="extpass", max_login_attempts=5),
    )

    result = recover_authentication(
        SimpleNamespace(session_callback=None),
        config,
        failed_operation="watch poll",
        login_attempts=4,
    )

    assert result == 5
    assert len(auto_login_calls) == 1


def test_recover_authentication_upa_max_login_attempts_exceeded(monkeypatch):
    from onleiharr.cli import recover_authentication

    login_calls = []

    def fake_login(client, config):
        login_calls.append(True)

    monkeypatch.setattr(cli, "login", fake_login)

    config = SimpleNamespace(
        credentials=SimpleNamespace(auth_type="upa"),
        external_auth=SimpleNamespace(auto_login=False, headless=True, timeout_secs=120.0, username=None, password=None, max_login_attempts=5),
    )

    result = recover_authentication(
        SimpleNamespace(),
        config,
        failed_operation="watch poll",
        login_attempts=5,
    )

    assert result is None
    assert len(login_calls) == 0


def test_recover_authentication_upa_succeeds_on_first_attempt(monkeypatch):
    from onleiharr.cli import recover_authentication

    login_calls = []

    def fake_login(client, config):
        login_calls.append(True)

    monkeypatch.setattr(cli, "login", fake_login)

    config = SimpleNamespace(
        credentials=SimpleNamespace(auth_type="upa"),
        external_auth=SimpleNamespace(auto_login=False, headless=True, timeout_secs=120.0, username=None, password=None, max_login_attempts=5),
    )

    result = recover_authentication(
        SimpleNamespace(),
        config,
        failed_operation="watch poll",
        login_attempts=0,
    )

    assert result == 1
    assert len(login_calls) == 1


def test_recover_authentication_oidc_disabled_still_returns_none(monkeypatch):
    from onleiharr.cli import recover_authentication

    config = SimpleNamespace(
        credentials=SimpleNamespace(auth_type="open_id"),
        external_auth=SimpleNamespace(auto_login=False, headless=True, timeout_secs=120.0, username=None, password=None, max_login_attempts=5),
    )

    result = recover_authentication(
        SimpleNamespace(),
        config,
        failed_operation="watch poll",
        login_attempts=0,
    )

    assert result is None


def test_recover_authentication_oidc_browser_failure_propagates(monkeypatch):
    from onleiharr._vendor.onleihe import OnleiheAuthError, SessionState
    from onleiharr.cli import recover_authentication

    def fake_external_login_automated(client, *, username, password, headless, timeout_secs):
        raise OnleiheAuthError("browser login failed")

    monkeypatch.setattr(cli, "external_login_automated", fake_external_login_automated)

    config = SimpleNamespace(
        credentials=SimpleNamespace(auth_type="open_id"),
        external_auth=SimpleNamespace(auto_login=True, headless=True, timeout_secs=120.0, username="extuser", password="extpass", max_login_attempts=5),
    )

    with pytest.raises(OnleiheAuthError, match="OIDC automated login failed after 5 attempts"):
        recover_authentication(
            SimpleNamespace(),
            config,
            failed_operation="watch poll",
            login_attempts=0,
        )


def test_recover_authentication_oidc_browser_failure_logs_error(monkeypatch, caplog):
    from onleiharr._vendor.onleihe import OnleiheAuthError
    from onleiharr.cli import recover_authentication

    def fake_external_login_automated(client, *, username, password, headless, timeout_secs):
        raise OnleiheAuthError("browser login failed")

    monkeypatch.setattr(cli, "external_login_automated", fake_external_login_automated)

    config = SimpleNamespace(
        credentials=SimpleNamespace(auth_type="open_id"),
        external_auth=SimpleNamespace(auto_login=True, headless=True, timeout_secs=120.0, username="extuser", password="extpass", max_login_attempts=5),
    )

    with pytest.raises(OnleiheAuthError, match="OIDC automated login failed after 5 attempts"):
        recover_authentication(
            SimpleNamespace(),
            config,
            failed_operation="watch poll",
            login_attempts=0,
        )

    assert caplog.record_tuples[-1] == (
        "onleiharr.cli",
        logging.ERROR,
        "Automated OIDC login failed during watch poll (attempt 5/5)",
    )


def test_login_command_uses_manual_flow(monkeypatch, capsys):
    from onleiharr.auth import external_login_manual
    from onleiharr.cli import main as cli_main
    from onleiharr._vendor.onleihe import SessionState

    login_calls = []

    def fake_external_login_manual(client):
        login_calls.append(("manual", client))
        return SessionState(access_token="tok", refresh_token="ref", user_id="uid", profile_id="pid", library_id="lid", onleihe_id="oid")

    def fake_external_login_browser(client):
        login_calls.append(("browser", client))
        return SessionState(access_token="tok", refresh_token="ref", user_id="uid", profile_id="pid", library_id="lid", onleihe_id="oid")

    monkeypatch.setattr("onleiharr.cli.external_login_manual", fake_external_login_manual)
    monkeypatch.setattr("onleiharr.cli.external_login_browser", fake_external_login_browser)

    config_path = Path("/tmp/test_config.toml")
    config_text = """
[general]
poll_interval_secs = 300

[credentials]
auth_type = "open_id"
host = "example.onleihe.de"
onleihe_id = "test-onleihe-id"
library_id = "test-library-id"

[notification]
urls = []
"""
    config_path.write_text(config_text)

    args = ["--login", "-c", str(config_path)]

    class FakeStdin:
        def isatty(self):
            return True

    class FakeStdout:
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", FakeStdin())
    monkeypatch.setattr("sys.stdout", FakeStdout())

    def fake_create_onleihe_client(config):
        class FakeClient:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
        return FakeClient()

    monkeypatch.setattr("onleiharr.cli.create_onleihe_client", fake_create_onleihe_client)

    def fake_save_session(path, session):
        pass

    monkeypatch.setattr("onleiharr.cli.save_session", fake_save_session)

    result = cli_main(args)

    assert result == 0
    assert len(login_calls) == 1
    assert login_calls[0][0] == "manual"


def test_login_browser_command_uses_browser_flow(monkeypatch, capsys):
    from onleiharr.cli import main as cli_main
    from onleiharr._vendor.onleihe import SessionState

    login_calls = []

    def fake_external_login_manual(client):
        login_calls.append(("manual", client))
        return SessionState(access_token="tok", refresh_token="ref", user_id="uid", profile_id="pid", library_id="lid", onleihe_id="oid")

    def fake_external_login_browser(client):
        login_calls.append(("browser", client))
        return SessionState(access_token="tok", refresh_token="ref", user_id="uid", profile_id="pid", library_id="lid", onleihe_id="oid")

    monkeypatch.setattr("onleiharr.cli.external_login_manual", fake_external_login_manual)
    monkeypatch.setattr("onleiharr.cli.external_login_browser", fake_external_login_browser)

    config_path = Path("/tmp/test_config_browser.toml")
    config_text = """
[general]
poll_interval_secs = 300

[credentials]
auth_type = "open_id"
host = "example.onleihe.de"
onleihe_id = "test-onleihe-id"
library_id = "test-library-id"

[notification]
urls = []
"""
    config_path.write_text(config_text)

    args = ["--login", "--login-browser", "-c", str(config_path)]

    class FakeStdin:
        def isatty(self):
            return True

    class FakeStdout:
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", FakeStdin())
    monkeypatch.setattr("sys.stdout", FakeStdout())

    def fake_create_onleihe_client(config):
        class FakeClient:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
        return FakeClient()

    monkeypatch.setattr("onleiharr.cli.create_onleihe_client", fake_create_onleihe_client)

    def fake_save_session(path, session):
        pass

    monkeypatch.setattr("onleiharr.cli.save_session", fake_save_session)

    result = cli_main(args)

    assert result == 0
    assert len(login_calls) == 1
    assert login_calls[0][0] == "browser"


def test_login_command_never_uses_automated_flow(monkeypatch, capsys):
    from onleiharr.cli import main as cli_main
    from onleiharr._vendor.onleihe import SessionState

    auto_login_calls = []

    def fake_external_login_automated(client, *, username, password, headless, timeout_secs):
        auto_login_calls.append(True)
        return SessionState(access_token="tok", refresh_token="ref", user_id="uid", profile_id="pid", library_id="lid", onleihe_id="oid")

    monkeypatch.setattr("onleiharr.cli.external_login_automated", fake_external_login_automated)

    config_path = Path("/tmp/test_config_auto.toml")
    config_text = """
[general]
poll_interval_secs = 300

[credentials]
auth_type = "open_id"
host = "example.onleihe.de"
onleihe_id = "test-onleihe-id"
library_id = "test-library-id"

[external_auth]
auto_login = true
username = "testuser"
password = "testpass"

[notification]
urls = []
"""
    config_path.write_text(config_text)

    args = ["--login", "-c", str(config_path)]

    class FakeStdin:
        def isatty(self):
            return True

    class FakeStdout:
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", FakeStdin())
    monkeypatch.setattr("sys.stdout", FakeStdout())

    def fake_external_login_manual(client):
        return SessionState(access_token="tok", refresh_token="ref", user_id="uid", profile_id="pid", library_id="lid", onleihe_id="oid")

    monkeypatch.setattr("onleiharr.cli.external_login_manual", fake_external_login_manual)

    def fake_create_onleihe_client(config):
        class FakeClient:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
        return FakeClient()

    monkeypatch.setattr("onleiharr.cli.create_onleihe_client", fake_create_onleihe_client)

    def fake_save_session(path, session):
        pass

    monkeypatch.setattr("onleiharr.cli.save_session", fake_save_session)

    result = cli_main(args)

    assert result == 0
    assert len(auto_login_calls) == 0


@pytest.mark.browser
def test_upa_startup_does_not_import_playwright(monkeypatch, capsys):
    from onleiharr.cli import main as cli_main

    config_path = Path("/tmp/test_config_upa.toml")
    config_text = """
[general]
poll_interval_secs = 300

[credentials]
auth_type = "upa"
host = "example.onleihe.de"
username = "upauser"
password = "upapass"
onleihe_id = "test-onleihe-id"
library_id = "test-library-id"

[notification]
urls = []
"""
    config_path.write_text(config_text)

    args = ["-c", str(config_path), "--once"]

    playwright_imported = []

    original_sync_playwright = None

    def fake_sync_playwright():
        playwright_imported.append(True)
        raise RuntimeError("Playwright should not be imported for UPA")

    try:
        import playwright.sync_api
        original_sync_playwright = playwright.sync_api.sync_playwright
    except ImportError:
        pass

    monkeypatch.setattr("playwright.sync_api.sync_playwright", fake_sync_playwright)

    def fake_create_onleihe_client(config):
        class FakeClient:
            closed = False
            onleihe_id = None
            library_id = None
            session = None
            session_callback = None

            def close(self):
                self.closed = True

            def maintenance_active(self):
                return False

            def login(self, username, password, **kwargs):
                pass

        return FakeClient()

    monkeypatch.setattr("onleiharr.cli.create_onleihe_client", fake_create_onleihe_client)

    def fake_login(client, config):
        pass

    monkeypatch.setattr("onleiharr.cli.login", fake_login)

    def fake_fetch_all_watched_media(client, config, *, log_summary=False):
        return cli.WatchPollResult(media=[])

    monkeypatch.setattr("onleiharr.cli.fetch_all_watched_media", fake_fetch_all_watched_media)

    monkeypatch.setattr("onleiharr.cli.build_apprise", lambda config: None)
    monkeypatch.setattr("onleiharr.cli.build_gourou_client", lambda config: None)
    monkeypatch.setattr(
        "onleiharr.cli.time",
        SimpleNamespace(monotonic=lambda: 0.0, sleep=lambda secs: None),
    )

    result = cli_main(args)

    assert result == 0
    assert len(playwright_imported) == 0


def test_errors_and_logs_contain_no_secrets(monkeypatch, capsys):
    from onleiharr.cli import main as cli_main
    from onleiharr._vendor.onleihe import OnleiheAuthError, SessionState

    config_path = Path("/tmp/test_config_secrets.toml")
    config_text = """
[general]
poll_interval_secs = 300

[credentials]
auth_type = "open_id"
host = "example.onleihe.de"
onleihe_id = "test-onleihe-id"
library_id = "test-library-id"

[external_auth]
auto_login = true
username = "super-secret-user"
password = "super-secret-pass"

[notification]
urls = []
"""
    config_path.write_text(config_text)

    args = ["--login", "-c", str(config_path)]

    class FakeStdin:
        def isatty(self):
            return True

    class FakeStdout:
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", FakeStdin())
    monkeypatch.setattr("sys.stdout", FakeStdout())

    def fake_external_login_manual(client):
        raise OnleiheAuthError("authentication failed")

    monkeypatch.setattr("onleiharr.cli.external_login_manual", fake_external_login_manual)

    def fake_create_onleihe_client(config):
        class FakeClient:
            def __enter__(self):
                return self
            def __exit__(self, *args):
                pass
        return FakeClient()

    monkeypatch.setattr("onleiharr.cli.create_onleihe_client", fake_create_onleihe_client)

    with pytest.raises(OnleiheAuthError, match="authentication failed"):
        cli_main(args)

    captured = capsys.readouterr()
    combined = captured.out + captured.err

    assert "super-secret-user" not in combined
    assert "super-secret-pass" not in combined
