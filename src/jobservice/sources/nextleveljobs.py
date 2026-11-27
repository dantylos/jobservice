"""Next Level Jobs (software jobs in Europe) via the site's sitemap of job pages.

https://nextleveljobs.eu/jobs/sitemap.xml lists every open job; each new one is opened once for
the schema.org JobPosting data (title, company, city or remote region, salary) and the
"Experience" level shown on the page. The first run has thousands of pages, so at most
JOBS_NEXTLEVELJOBS_MAX_PAGES are opened per run and the rest follow on later runs.
robots.txt (checked 2026-11-26) allows everything; the terms do not mention automated access.
"""

import html as htmllib
import logging
import os
import re
from collections.abc import Iterator

from jobservice import places
from jobservice.http import HttpClient
from jobservice.models import SearchFilter, Vacancy
from jobservice.text import html_to_text

log = logging.getLogger(__name__)

SITEMAP = "https://nextleveljobs.eu/jobs/sitemap.xml"
_JOB = re.compile(r"<loc>(https://nextleveljobs\.eu/companies/[^/]+/jobs/([0-9a-f]+))</loc>")
_LEVEL = re.compile(r">Experience</dt>.*?<span[^>]*>([^<]+)</span>", re.S)
_LEVELS = {"junior": "junior", "mid-level": "medior", "senior": "senior", "staff": "senior",
           "principal": "senior", "lead": "senior"}  # fmt: skip


def job_urls(sitemap_xml: str) -> list[tuple[str, str]]:
    """(id, url) of each job."""
    return [(m.group(2), m.group(1)) for m in _JOB.finditer(sitemap_xml)]


def level(page: str) -> str | None:
    m = _LEVEL.search(page)
    return _LEVELS.get(m.group(1).strip().lower()) if m else None


def _text(description: str | None) -> str | None:
    """The site gives plain text; html_to_text only if it ever sends HTML."""
    if not description:
        return None
    return html_to_text(description) if "<" in description else description.strip()


def from_page(job_id: str, url: str, page: str) -> Vacancy | None:
    posting = places.job_posting(page)
    if not posting:
        return None
    remote = places.is_remote(posting)
    where = places.locality(posting)
    names = places.requirement_names(posting.get("applicantLocationRequirements"))
    location = places.applicant_location(names)
    city = None if remote else where
    low, high, currency, period = places.salary(posting)
    published = posting.get("datePosted")
    return Vacancy(
        source="nextleveljobs",
        external_id=job_id,
        title=htmllib.unescape(str(posting.get("title") or "")).strip() or job_id,
        url=url,
        company=places.company(posting),
        city=city,
        description_text=_text(posting.get("description")),
        published_at=published,
        published_raw=published,
        work_mode="remote" if remote else "onsite",
        seniority=level(page),
        applicant_location=location,
        salary_min=low,
        salary_max=high,
        salary_currency=currency,
        salary_period=period,
        market=places.market(city, location),
    )


class NextLevelJobs:
    name = "nextleveljobs"
    # Off by default (2026-11-27): slow, senior-only, no junior fits in Prague.
    # Still runs with --source nextleveljobs.
    enabled = False
    request_delay = 3.0  # seconds; NoFluffJobs throttled (429) at 1 s
    max_failures = 3  # in a row: the site is probably blocking, stop until next run

    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self.limit = int(os.getenv("JOBS_NEXTLEVELJOBS_MAX_PAGES", "200"))

    def collect(self, flt: SearchFilter, skip_detail: set[str]) -> Iterator[Vacancy]:
        jobs = job_urls(self.http.get(SITEMAP).text)
        todo = [(i, u) for i, u in jobs if i not in skip_detail]
        log.info("nextleveljobs: %d jobs, %d new", len(jobs), len(todo))
        if len(todo) > self.limit:
            log.info("nextleveljobs: opening %d now, the rest next run", self.limit)
        failures = 0
        for job_id, url in todo[: self.limit]:
            try:
                v = from_page(job_id, url, self.http.get(url).text)
            except Exception as e:  # one broken page must not stop the run
                log.warning("nextleveljobs %s failed: %s", job_id, e)
                failures += 1
                if failures >= self.max_failures:
                    log.warning("%s: %d failures in a row, the rest waits for the next run",
                                self.name, failures)  # fmt: skip
                    return
                continue
            failures = 0
            if v:
                yield v
            else:
                log.warning("nextleveljobs %s: no JobPosting data", job_id)
