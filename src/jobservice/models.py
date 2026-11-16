from dataclasses import dataclass, field


@dataclass(slots=True)
class Vacancy:
    source: str
    external_id: str
    title: str
    url: str
    company: str | None = None
    city: str | None = None
    description_text: str | None = None
    description_html: str | None = None
    # ISO 8601 (date or datetime). None when the source gives no usable date.
    published_at: str | None = None
    # The date exactly as the source showed it, e.g. "10. listopadu".
    published_raw: str | None = None


@dataclass(slots=True)
class SearchFilter:
    keywords: list[str] = field(default_factory=list)
    city: str | None = None
    max_pages: int = 3
