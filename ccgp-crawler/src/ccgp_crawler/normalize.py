from __future__ import annotations

import re
from datetime import datetime, time, timedelta, timezone
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit


BASE_URL = "https://www.ccgp.gov.cn"
SHANGHAI_TZ = timezone(timedelta(hours=8))
CATEGORY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
PUBLISHED_RE = re.compile(
    r"发布时间\s*[:：]?\s*"
    r"(?P<date>\d{4}[-/]\d{1,2}[-/]\d{1,2})"
    r"(?:\s+(?P<time>\d{1,2}:\d{2}(?::\d{2})?))?"
)


def validate_category(category: str) -> str:
    value = category.strip().lower()
    if not CATEGORY_RE.fullmatch(value):
        raise ValueError(
            "category must contain only lowercase letters, digits, '_' or '-': "
            f"{category!r}"
        )
    return value


def list_url(category: str, page_no: int) -> str:
    category = validate_category(category)
    if page_no < 0:
        raise ValueError("page_no must be >= 0")
    filename = "index.htm" if page_no == 0 else f"index_{page_no}.htm"
    return f"{BASE_URL}/cggg/{category}/{filename}"


def canonicalize_url(page_url: str, href: str) -> str:
    """Resolve a list-link href and remove URL fragments.

    CCGP's own host is always stored as HTTPS. Query strings are retained
    because they can be part of a resource identity; only the fragment is
    discarded.
    """

    if not href or not href.strip():
        raise ValueError("empty href")
    absolute = urljoin(page_url, href.strip())
    absolute, _fragment = urldefrag(absolute)
    parts = urlsplit(absolute)
    if not parts.scheme or not parts.netloc:
        raise ValueError(f"invalid URL: {href!r}")

    hostname = (parts.hostname or "").lower()
    netloc = parts.netloc.lower()
    scheme = parts.scheme.lower()
    if hostname == "www.ccgp.gov.cn":
        scheme = "https"
        # The source uses the default HTTPS host; avoid retaining an explicit
        # default port while leaving other ports untouched for diagnostics.
        if parts.port in (None, 443, 80):
            netloc = hostname
    return urlunsplit((scheme, netloc, parts.path, parts.query, ""))


def parse_published_at(row_text: str) -> str | None:
    """Return a Shanghai-time ISO-8601 value from a list-row string.

    CCGP normally supplies minute precision. Date-only rows are accepted and
    represented at midnight so the field remains a valid date-time; no local
    machine timezone is involved.
    """

    match = PUBLISHED_RE.search(row_text)
    if not match:
        return None

    date_text = match.group("date").replace("/", "-")
    time_text = match.group("time") or "00:00:00"
    if time_text.count(":") == 1:
        time_text += ":00"
    try:
        parsed_date = datetime.strptime(date_text, "%Y-%m-%d").date()
        parsed_time = time.fromisoformat(time_text)
    except ValueError:
        return None
    return datetime.combine(parsed_date, parsed_time, tzinfo=SHANGHAI_TZ).isoformat(
        timespec="seconds"
    )


def normalize_title(value: str) -> str:
    # The contract says to preserve source wording. Only boundary whitespace
    # is removed; internal spaces/punctuation are not rewritten.
    return value.strip()
