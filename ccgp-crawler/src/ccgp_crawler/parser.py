from __future__ import annotations

from dataclasses import dataclass
import re
from urllib.parse import urlsplit

from bs4 import BeautifulSoup

from .models import Notice
from .normalize import canonicalize_url, normalize_title, parse_published_at, validate_category
from .regions import region_code_for


CCGP_HOST = "www.ccgp.gov.cn"
REGION_RE = re.compile(r"(?:地域|地区)\s*[:：]\s*([^\s]+)")
TENDER_TYPES = {"公开招标", "邀请招标"}
TENDER_PATHS = {"gkzb", "yqzb"}


@dataclass(frozen=True, slots=True)
class ParseResult:
    notices: list[Notice]
    candidate_count: int
    skipped_count: int
    filtered_count: int
    listing_count: int


def _is_tender(row, url: str, title: str) -> bool:
    label = row.select_one('em[rel="bxlx"]')
    if label is not None:
        return label.get_text(" ", strip=True) in TENDER_TYPES
    # When the type label is missing, require both the URL's bid category and
    # the announcement title; never classify an award/correction by keyword.
    path_parts = set(urlsplit(url).path.lower().split("/"))
    return bool(path_parts & TENDER_PATHS) and title.endswith(("公开招标公告", "邀请招标公告"))


def _region_raw(row) -> str | None:
    text = row.get_text(" ", strip=True)
    match = REGION_RE.search(text)
    return match.group(1).strip() if match else None


def _is_notice_url(url: str) -> bool:
    parts = urlsplit(url)
    if parts.hostname != CCGP_HOST:
        return False
    path = parts.path.lower()
    filename = path.rsplit("/", 1)[-1]
    return (
        path.startswith("/cggg/")
        and path.endswith(".htm")
        and not re.fullmatch(r"index(?:_\d+)?\.htm", filename)
    )


def _candidate_rows(soup: BeautifulSoup):
    # The current layout uses ul.c_list_bid. The li fallback keeps a small
    # markup change from silently turning an otherwise valid page into zero
    # records; URL/path filtering below prevents navigation links leaking in.
    return soup.select("ul.c_list_bid li") or soup.select("li")


def parse_list_page_result(page_url: str, html: str, category: str) -> ParseResult:
    category = validate_category(category)
    soup = BeautifulSoup(html, "lxml")
    source_list_url = canonicalize_url(page_url, page_url)
    notices: list[Notice] = []
    seen: set[str] = set()
    candidate_count = 0
    skipped_count = 0
    filtered_count = 0
    listing_count = 0

    for row in _candidate_rows(soup):
        link = row.select_one("a[href]")
        if link is None:
            continue
        candidate_count += 1
        try:
            url = canonicalize_url(page_url, link.get("href", ""))
        except ValueError:
            skipped_count += 1
            continue
        if not _is_notice_url(url) or url in seen:
            if url in seen:
                skipped_count += 1
            continue

        seen.add(url)
        listing_count += 1
        title = normalize_title(link.get("title") or link.get_text(" ", strip=True))
        if not _is_tender(row, url, title):
            filtered_count += 1
            continue
        published_at = parse_published_at(row.get_text(" ", strip=True))
        # A row without either required value is not a valid extracted notice.
        # It is counted for diagnostics rather than being persisted as partial
        # business data.
        if not title or published_at is None:
            skipped_count += 1
            continue

        region_raw = _region_raw(row)
        notices.append(
            Notice(
                canonical_url=url,
                title=title,
                published_at=published_at,
                region_raw=region_raw,
                region_code=region_code_for(region_raw),
                category=category,
                source_list_url=source_list_url,
            )
        )

    return ParseResult(
        notices=notices,
        candidate_count=candidate_count,
        skipped_count=skipped_count,
        filtered_count=filtered_count,
        listing_count=listing_count,
    )


def parse_list_page(page_url: str, html: str, category: str) -> list[Notice]:
    return parse_list_page_result(page_url, html, category).notices
