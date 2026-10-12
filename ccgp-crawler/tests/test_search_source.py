from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from ccgp_crawler.search_source import (
    SearchPageError,
    SearchSoftBlockError,
    build_search_url,
    parse_search_page,
)


FIXTURE = Path(__file__).parent / "fixtures" / "search_results.html"


def test_search_url_uses_fixed_public_tender_parameters():
    url = build_search_url(date(2026, 9, 19), 3)
    params = parse_qs(urlsplit(url).query, keep_blank_values=True)
    assert urlsplit(url).netloc == "search.ccgp.gov.cn"
    assert params["page_index"] == ["3"]
    assert params["bidType"] == ["1"]
    assert params["bidSort"] == ["0"]
    assert params["timeType"] == ["6"]
    assert params["start_time"] == params["end_time"] == ["2026:09:19"]


def test_search_parser_keeps_only_public_tender_and_extracts_region():
    page_url = build_search_url(date(2026, 9, 19), 1)
    result = parse_search_page(page_url, FIXTURE.read_text(encoding="utf-8"))
    assert result.total_records == 21
    assert result.total_pages == 2
    assert result.listing_count == 2
    assert result.filtered_count == 1
    assert len(result.notices) == 1
    notice = result.notices[0]
    assert notice.title == "四川省测试公开招标项目"
    assert notice.published_at == "2026-09-19T10:20:30+08:00"
    assert notice.region_raw == "四川省"
    assert notice.region_code == "510000"
    assert notice.category == "dfgg"
    assert notice.source_list_url == page_url


def test_search_parser_detects_soft_block():
    with pytest.raises(SearchSoftBlockError):
        parse_search_page("https://search.ccgp.gov.cn/bxsearch", "您的访问过于频繁，请稍后再试")


def test_search_parser_rejects_results_outside_the_requested_slice():
    page_url = build_search_url(date(2026, 6, 1), 1)
    with pytest.raises(SearchPageError, match="期望 2026-06-01，实际 2026-09-19"):
        parse_search_page(
            page_url,
            FIXTURE.read_text(encoding="utf-8"),
            expected_date=date(2026, 6, 1),
        )
