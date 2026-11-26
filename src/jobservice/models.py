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
    # Salary in whole currency units (not cents), as published.
    salary_min: float | None = None
    salary_max: float | None = None
    salary_currency: str | None = None  # "CZK", "EUR", ...
    salary_period: str | None = None  # "month", "hour", "day", "year"
    work_mode: str | None = None  # comma-separated: "remote", "hybrid", "onsite"
    seniority: str | None = None  # comma-separated: "junior", "medior", "senior"
    languages: str | None = None  # comma-separated "language:level", e.g. "czech:advanced"
    # Job market the vacancy belongs to: "cz" (Czech sites) or "ua" (Ukrainian remote sites).
    market: str = "cz"
    # Where applicants must live, as the site states it (e.g. "UA", "Europe"); None = not stated.
    applicant_location: str | None = None


@dataclass(slots=True)
class SearchFilter:
    keywords: list[str] = field(default_factory=list)
    city: str | None = None
    max_pages: int = 3
    # Also search remote vacancies anywhere in the country.
    include_remote: bool = False
