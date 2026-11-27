"""Jobs.cz: HTML search results + detail pages.

Detail pages come in three flavours, tried in this order:
1. beta.jobs.cz — JSON-LD JobPosting with full description and datePosted;
2. www.jobs.cz/rpd, /fp — description in [data-jobad="body"];
3. employer templates (*.jobs.cz/detail-pozice) render the text with JS, so only
   the og:description summary is available.
"""

import json
import logging
import re
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlencode

from selectolax.lexbor import LexborHTMLParser

from jobservice.http import HttpClient
from jobservice.models import SearchFilter, Vacancy
from jobservice.text import clean, html_to_text, parse_czech_date, slugify

log = logging.getLogger(__name__)

BASE = "https://www.jobs.cz"


@dataclass(slots=True)
class Card:
    id: str
    title: str
    company: str | None
    city: str | None
    date_raw: str | None
    work_mode: str | None = None
    salary_min: float | None = None
    salary_max: float | None = None
    salary_period: str | None = None


# Card tags: "Práce převážně z domova" (mostly remote), "Možnost občasné práce z domova".
_MODE_TAGS = {"prace prevazne z domova": "remote", "moznost obcasne prace z domova": "hybrid"}
_SALARY = re.compile(r"^(?:od )?(\d[\d ]*)(?: [–-] (\d[\d ]*))? Kč(?: ?/ ?(hod|měs))?", re.I)


def parse_tags(tags: list[str]) -> dict:
    out: dict = {}
    for raw in tags:
        tag = re.sub(r"[\u200d\u202f\xa0\s]+", " ", raw).strip()
        mode = _MODE_TAGS.get(slugify(tag).replace("-", " "))
        if mode:
            out["work_mode"] = mode
        m = _SALARY.match(tag)
        if m:
            low = float(m.group(1).replace(" ", ""))
            high = float(m.group(2).replace(" ", "")) if m.group(2) else None
            out["salary_min"], out["salary_max"] = low, high
            out["salary_period"] = "hour" if (m.group(3) or "").lower() == "hod" else "month"
    return out


@dataclass(slots=True)
class Detail:
    html: str | None
    text: str | None
    published_at: str | None


def search_url(keyword: str, city: str | None, page: int) -> str:
    path = f"/prace/{slugify(city)}/" if city else "/prace/"
    params = {"q[]": keyword}
    if page > 1:
        params["page"] = str(page)
    return f"{BASE}{path}?{urlencode(params)}"


def detail_url(job_id: str) -> str:
    return f"{BASE}/rpd/{job_id}/"


def parse_search(html: str, page: int) -> tuple[list[Card], bool]:
    tree = LexborHTMLParser(html)
    cards = []
    for art in tree.css("article.SearchResultCard"):
        link = art.css_first("a[data-jobad-id]")
        if link is None:
            continue
        company = art.css_first(".SearchResultCard__footerItem span[translate='no']")
        city = art.css_first("[data-test='serp-locality']")
        status = art.css_first("[data-test-ad-status]")
        tags = [t.text() for t in art.css(".SearchResultCard__body .Tag")]
        cards.append(
            Card(
                id=link.attributes["data-jobad-id"],
                title=clean(link.text()) or "",
                company=clean(company.text()) if company else None,
                city=clean(city.text()) if city else None,
                date_raw=clean(status.text()) if status else None,
                **parse_tags(tags),
            )
        )
    has_next = tree.css_first(f"a[href*='page={page + 1}']") is not None
    return cards, has_next


def parse_detail(html: str) -> Detail:
    tree = LexborHTMLParser(html)

    for script in tree.css("script[type='application/ld+json']"):
        try:
            data = json.loads(script.text())
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                desc = item.get("description")
                return Detail(desc, html_to_text(desc), item.get("datePosted"))

    body = tree.css_first("[data-jobad='body']")
    if body is not None:
        return Detail(body.html, html_to_text(body.html), None)

    meta = tree.css_first("meta[property='og:description']") or tree.css_first(
        "meta[name='description']"
    )
    summary = clean(meta.attributes.get("content")) if meta else None
    return Detail(None, html_to_text(summary), None)


class JobsCz:
    name = "jobscz"
    enabled = True

    def __init__(self, http: HttpClient, today: date | None = None) -> None:
        self.http = http
        self.today = today or date.today()

    def collect(self, flt: SearchFilter, skip_detail: set[str]) -> Iterator[Vacancy]:
        seen: set[str] = set()
        # Jobs.cz has no URL filter for remote work, so the remote pass searches the
        # whole country and keeps only cards tagged as mostly-remote.
        passes = [(flt.city, False)]
        if flt.include_remote and flt.city:
            passes.append((None, True))
        for keyword in flt.keywords:
            for city, remote_only in passes:
                for page in range(1, flt.max_pages + 1):
                    url = search_url(keyword, city, page)
                    log.info("jobs.cz: %s", url)
                    cards, has_next = parse_search(self.http.get(url).text, page)
                    for card in cards:
                        if card.id in seen or (remote_only and card.work_mode != "remote"):
                            continue
                        seen.add(card.id)
                        yield self._vacancy(card, fetch_detail=card.id not in skip_detail)
                    if not has_next or not cards:
                        break

    def _vacancy(self, card: Card, fetch_detail: bool) -> Vacancy:
        v = Vacancy(
            source=self.name,
            external_id=card.id,
            title=card.title,
            url=detail_url(card.id),
            company=card.company,
            city=card.city,
            published_raw=card.date_raw,
            published_at=parse_czech_date(card.date_raw, self.today),
            work_mode=card.work_mode,
            salary_min=card.salary_min,
            salary_max=card.salary_max,
            salary_currency="CZK" if card.salary_min else None,
            salary_period=card.salary_period,
        )
        if fetch_detail:
            try:
                d = parse_detail(self.http.get(v.url).text)
            except Exception as e:  # one broken ad must not stop the run
                log.warning("jobs.cz detail %s failed: %s", card.id, e)
            else:
                v.description_html, v.description_text = d.html, d.text
                v.published_at = d.published_at or v.published_at
        return v
