from datetime import date

import pytest
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from ccgp_crawler.job_service import PlaywrightSearchAdapter, SearchNavigationTimeout
from ccgp_crawler.search_source import SearchPageError


class FakeResponse:
    status = 200


class FakePage:
    def __init__(
        self,
        error: Exception | None = None,
        load_error: Exception | None = None,
    ):
        self.error = error
        self.load_error = load_error
        self.visited: list[str] = []

    def goto(self, url: str, **_kwargs):
        self.visited.append(url)
        if self.error:
            raise self.error
        return FakeResponse()

    def wait_for_load_state(self, *_args, **_kwargs):
        if self.load_error:
            raise self.load_error
        return None

    def wait_for_timeout(self, *_args, **_kwargs):
        return None

    def content(self):
        return "<html>recovered</html>"


def test_fetch_reopens_browser_once_after_target_is_closed(tmp_path, monkeypatch):
    adapter = PlaywrightSearchAdapter(tmp_path / "profile")
    closed = RuntimeError("Page.goto: Target page, context or browser has been closed")
    pages = iter([FakePage(closed), FakePage()])
    ensure_calls = 0

    def ensure_browser():
        nonlocal ensure_calls
        ensure_calls += 1
        adapter._page = next(pages)

    monkeypatch.setattr(adapter, "_ensure_browser", ensure_browser)
    result = adapter.fetch("https://example.test/page", date(2026, 6, 4), 1)

    assert ensure_calls == 2
    assert result.http_status == 200
    assert result.html == "<html>recovered</html>"


def test_fetch_explains_how_to_resume_when_reopen_also_fails(tmp_path, monkeypatch):
    adapter = PlaywrightSearchAdapter(tmp_path / "profile")
    closed = RuntimeError("Page.goto: Target page, context or browser has been closed")

    def ensure_browser():
        adapter._page = FakePage(closed)

    monkeypatch.setattr(adapter, "_ensure_browser", ensure_browser)
    with pytest.raises(SearchPageError, match="重新打开并继续"):
        adapter.fetch("https://example.test/page", date(2026, 6, 4), 1)


def test_fetch_classifies_only_goto_timeout_as_navigation_timeout(tmp_path, monkeypatch):
    adapter = PlaywrightSearchAdapter(tmp_path / "profile")
    page = FakePage(PlaywrightTimeoutError("Page.goto: Timeout 60000ms exceeded"))
    monkeypatch.setattr(adapter, "_ensure_browser", lambda: setattr(adapter, "_page", page))

    with pytest.raises(SearchNavigationTimeout, match="第 29 页"):
        adapter.fetch("https://example.test/page", date(2026, 6, 4), 29)


def test_fetch_keeps_domcontentloaded_timeout_tolerant(tmp_path, monkeypatch):
    adapter = PlaywrightSearchAdapter(tmp_path / "profile")
    page = FakePage(
        load_error=PlaywrightTimeoutError("page.wait_for_load_state: Timeout 20000ms exceeded")
    )
    monkeypatch.setattr(adapter, "_ensure_browser", lambda: setattr(adapter, "_page", page))

    result = adapter.fetch("https://example.test/page", date(2026, 6, 4), 1)

    assert result.http_status == 200
    assert result.html == "<html>recovered</html>"
