from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True, slots=True)
class Notice:
    """The complete business payload extracted by this crawler.

    Keep this model deliberately small. Runtime bookkeeping belongs in the
    SQLite checkpoint/page-run tables and is not part of the extracted notice.
    """

    canonical_url: str
    title: str
    published_at: str
    region_raw: str | None
    region_code: str | None
    category: str
    source_list_url: str

    def as_dict(self) -> dict[str, str | None]:
        return asdict(self)
