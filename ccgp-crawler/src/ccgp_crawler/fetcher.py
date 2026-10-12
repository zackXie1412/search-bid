from __future__ import annotations

from dataclasses import dataclass

import httpx


BLOCK_MARKERS = (
    "您的访问过于频繁",
    "频繁访问",
    "请稍后再试",
    "事件ID",
)


class SoftBlockError(RuntimeError):
    """The site returned a block page, including an HTTP 200 block page."""


class FetchError(RuntimeError):
    """A page could not be fetched after the configured retries."""


@dataclass(frozen=True, slots=True)
class FetchedPage:
    url: str
    status_code: int
    body: str
    attempts: int


def is_soft_block(status_code: int, body: str) -> bool:
    return status_code in {403, 429, 503} or any(
        marker in body[:20_000] for marker in BLOCK_MARKERS
    )


def fetch_page(
    client: httpx.Client,
    url: str,
    *,
    max_attempts: int = 3,
    retry_backoff: float = 2.0,
) -> FetchedPage:
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    last_error: Exception | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            response = client.get(url)
            body = response.text
        except httpx.HTTPError as exc:
            last_error = exc
            if attempt == max_attempts:
                break
            # Keep the sleep in this low-level helper deterministic. The page
            # delay/jitter is handled by the crawler between successful pages.
            import time

            time.sleep(retry_backoff ** (attempt - 1))
            continue

        if is_soft_block(response.status_code, body):
            raise SoftBlockError(f"soft block detected for {url} (HTTP {response.status_code})")
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            last_error = exc
            if attempt == max_attempts:
                break
            import time

            time.sleep(retry_backoff ** (attempt - 1))
            continue
        return FetchedPage(url=url, status_code=response.status_code, body=body, attempts=attempt)

    raise FetchError(f"failed to fetch {url} after {max_attempts} attempts: {last_error}") from last_error
