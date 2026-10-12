from __future__ import annotations

import math
import re
from dataclasses import dataclass
from datetime import date, datetime
from urllib.parse import urlencode, urlsplit

from bs4 import BeautifulSoup, Tag

from .models import Notice
from .normalize import SHANGHAI_TZ, canonicalize_url, normalize_title
from .regions import region_code_for


SEARCH_BASE_URL = "https://search.ccgp.gov.cn/bxsearch"
PUBLIC_TENDER_LABEL = "公开招标公告"
BLOCK_MARKERS = ("您的访问过于频繁", "频繁访问", "请稍后再试", "事件ID")
DATE_RE = re.compile(
    r"(?P<date>\d{4}[.\-/]\d{1,2}[.\-/]\d{1,2})\s+"
    r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?)"
)
TOTAL_RE = re.compile(r"共找到\s*([\d,]+)\s*条")


class SearchPageError(RuntimeError):
    pass


class SearchSoftBlockError(SearchPageError):
    pass


@dataclass(frozen=True, slots=True)
class SearchPageResult:
    notices: list[Notice]
    total_records: int
    total_pages: int
    listing_count: int
    filtered_count: int
    parse_error_count: int


def build_search_url(slice_date: date, page_index: int) -> str:
    if page_index < 1:
        raise ValueError("page_index must be >= 1")
    day = slice_date.strftime("%Y:%m:%d")
    params = [
        ("searchtype", "1"),
        ("page_index", str(page_index)),
        ("bidSort", "0"),
        ("buyerName", ""),
        ("projectId", ""),
        ("pinMu", "0"),
        ("bidType", "1"),
        ("dbselect", "bidx"),
        ("kw", ""),
        ("start_time", day),
        ("end_time", day),
        ("timeType", "6"),
        ("displayZone", ""),
        ("zoneId", ""),
        ("pppStatus", "0"),
        ("agentName", ""),
    ]
    return f"{SEARCH_BASE_URL}?{urlencode(params)}"


def is_soft_block(body: str) -> bool:
    return any(marker in body[:20_000] for marker in BLOCK_MARKERS)


def _category_from_url(url: str) -> str | None:
    parts = set(urlsplit(url).path.lower().split("/"))
    if "zygg" in parts:
        return "zygg"
    if "dfgg" in parts:
        return "dfgg"
    return None


def _published_at(text: str) -> str | None:
    match = DATE_RE.search(text)
    if not match:
        return None
    normalized = match.group("date").replace(".", "-").replace("/", "-")
    clock = match.group("time")
    if clock.count(":") == 1:
        clock += ":00"
    try:
        parsed = datetime.fromisoformat(f"{normalized}T{clock}")
    except ValueError:
        return None
    return parsed.replace(tzinfo=SHANGHAI_TZ).isoformat(timespec="seconds")


def _region_after_type(type_tag: Tag) -> str | None:
    fragments: list[str] = []
    for sibling in type_tag.next_siblings:
        if isinstance(sibling, Tag) and sibling.name == "strong":
            break
        if isinstance(sibling, Tag):
            fragments.append(sibling.get_text(" ", strip=True))
        else:
            fragments.append(str(sibling))
    candidate = " ".join(fragments).strip().strip("|").strip()
    candidate = re.sub(r"\s+", "", candidate)
    return candidate or None


def parse_search_page(
    page_url: str,
    html: str,
    *,
    expected_date: date | None = None,
) -> SearchPageResult:
    if is_soft_block(html):
        raise SearchSoftBlockError("search.ccgp.gov.cn returned a soft-block page")

    soup = BeautifulSoup(html, "lxml")
    page_text = soup.get_text(" ", strip=True)
    total_match = TOTAL_RE.search(page_text)
    if total_match is None:
        raise SearchPageError("search result count was not found")
    total_records = int(total_match.group(1).replace(",", ""))
    total_pages = math.ceil(total_records / 20) if total_records else 0

    rows = soup.select("ul.vT-srch-result-list-bid > li")
    if total_records and not rows:
        raise SearchPageError("result rows were not found")

    notices: list[Notice] = []
    seen: set[str] = set()
    filtered_count = 0
    parse_error_count = 0

    for row in rows:
        link = row.select_one("a[href]")
        type_tag = next(
            (tag for tag in row.select("strong") if tag.get_text(" ", strip=True) == PUBLIC_TENDER_LABEL),
            None,
        )
        if link is None:
            parse_error_count += 1
            continue
        if type_tag is None:
            filtered_count += 1
            continue
        try:
            canonical_url = canonicalize_url(page_url, link.get("href", ""))
        except ValueError:
            parse_error_count += 1
            continue
        category = _category_from_url(canonical_url)
        title = normalize_title(link.get("title") or link.get_text(" ", strip=True))
        published_at = _published_at(row.get_text(" ", strip=True))
        region_raw = _region_after_type(type_tag)
        if (
            not category
            or not title
            or published_at is None
            or canonical_url in seen
        ):
            parse_error_count += 1
            continue
        seen.add(canonical_url)
        notices.append(
            Notice(
                canonical_url=canonical_url,
                title=title,
                published_at=published_at,
                region_raw=region_raw,
                region_code=region_code_for(region_raw),
                category=category,
                source_list_url=page_url,
            )
        )

    if expected_date is not None:
        unexpected_dates = sorted(
            {
                datetime.fromisoformat(notice.published_at).date().isoformat()
                for notice in notices
                if datetime.fromisoformat(notice.published_at).date() != expected_date
            }
        )
        if unexpected_dates:
            actual = ", ".join(unexpected_dates)
            raise SearchPageError(
                f"搜索站返回的公告日期与切片日期不一致："
                f"期望 {expected_date.isoformat()}，实际 {actual}。已拒绝写入当前页"
            )

    return SearchPageResult(
        notices=notices,
        total_records=total_records,
        total_pages=total_pages,
        listing_count=len(rows),
        filtered_count=filtered_count,
        parse_error_count=parse_error_count,
    )
