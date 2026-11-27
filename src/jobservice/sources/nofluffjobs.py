"""NoFluffJobs (Polish IT job board, also Czech and remote jobs) via its RSS feed.

Feed: https://nofluffjobs.com/rss lists every active offer (title "Role @ Company" and link),
without description. Only offers whose link names Prague or remote work are opened, once each,
for the schema.org JobPosting data (description, level, who may apply). robots.txt (checked
2026-11-26) allows /rss and /job/ and forbids /api/ and /posting/, which are not used. The
terms could not be read automatically; the owner chose to use the site at their own risk.
"""

import html as htmllib
import logging
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from email.utils import parsedate_to_datetime

from jobservice import places
from jobservice.filters import senior_word
from jobservice.http import HttpClient
from jobservice.models import SearchFilter, Vacancy
from jobservice.text import html_to_text

log = logging.getLogger(__name__)

FEED = "https://nofluffjobs.com/rss"
_PRAGUE = {"praha", "prague"}
_REMOTE = "remote"
# The site's levels -> ours.
_LEVELS = {"trainee": "junior", "junior": "junior", "mid": "medior", "senior": "senior",
           "expert": "senior"}  # fmt: skip


def _slug_words(url: str) -> set[str]:
    return set(url.rstrip("/").rsplit("/", 1)[-1].split("-"))


def wanted(url: str) -> bool:
    """Only Prague, remote and Ukrainian offers are worth opening."""
    return bool(_slug_words(url) & (_PRAGUE | {_REMOTE} | places.UA_CITIES))


def parse_feed(xml: str | bytes) -> list[Vacancy]:
    vacancies = []
    for item in ET.fromstring(xml).iter("item"):
        link = (item.findtext("link") or "").strip()
        if "/job/" not in link:
            continue
        title = htmllib.unescape(item.findtext("title") or "").strip()
        role, _, firm = title.rpartition(" @ ")
        published = item.findtext("pubDate")
        try:
            published_at = parsedate_to_datetime(published).isoformat() if published else None
        except (TypeError, ValueError):
            published_at = None
        words = _slug_words(link)
        vacancies.append(
            Vacancy(
                source="nofluffjobs",
                external_id=link.rstrip("/").rsplit("/", 1)[-1],
                title=(role or title).strip(),
                company=firm.strip() or None,
                url=link,
                city="Praha" if words & _PRAGUE else None,
                work_mode="remote" if _REMOTE in words else None,
                published_at=published_at,
                published_raw=published,
            )
        )
    return vacancies


def apply_detail(v: Vacancy, posting: dict) -> None:
    v.description_html = posting.get("description") or v.description_html
    v.description_text = html_to_text(v.description_html)
    v.company = places.company(posting) or v.company
    if places.is_remote(posting):
        v.work_mode = "remote"
    level = (posting.get("experienceRequirements") or {}).get("description")
    v.seniority = _LEVELS.get(str(level).lower()) if level else v.seniority
    names = places.requirement_names(posting.get("applicantLocationRequirements"))
    v.applicant_location = places.applicant_location(names)
    v.salary_min, v.salary_max, v.salary_currency, v.salary_period = places.salary(posting)
    v.market = places.market(v.city, v.applicant_location)


class NoFluffJobs:
    name = "nofluffjobs"
    enabled = True
    request_delay = 3.0  # seconds; NoFluffJobs throttled (429) at 1 s
    max_failures = 3  # in a row: the site is probably blocking, stop until next run

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def collect(self, flt: SearchFilter, skip_detail: set[str]) -> Iterator[Vacancy]:
        items = parse_feed(self.http.get(FEED).content)
        kept = [v for v in items if wanted(v.url) and not senior_word(v.title)]
        log.info(
            "nofluffjobs: %d offers, %d in Prague/remote and not senior", len(items), len(kept)
        )
        failures = 0
        for v in kept:
            if v.external_id in skip_detail:
                continue  # already stored with its description
            try:
                posting = places.job_posting(self.http.get(v.url).text)
            except Exception as e:  # one broken page must not stop the run
                log.warning("nofluffjobs detail %s failed: %s", v.external_id, e)
                failures += 1
                if failures >= self.max_failures:
                    log.warning("%s: %d failures in a row, the rest waits for the next run",
                                self.name, failures)  # fmt: skip
                    return
                continue
            failures = 0
            if posting:
                apply_detail(v, posting)
            yield v
