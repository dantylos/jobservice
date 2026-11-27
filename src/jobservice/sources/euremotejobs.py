"""EU Remote Jobs (remote jobs in Europe) via the site's sitemap of job listings.

The sitemap index (https://euremotejobs.com/sitemap.xml) lists job sitemaps newest first; only
the first one (the latest ~1000 listings) is read. Listings whose URL already says "senior",
"principal"... are not opened; the rest are opened once each for the schema.org JobPosting data.
robots.txt (checked 2026-11-26) allows /job/ pages and the sitemap; the forbidden search_*
parameters are not used. The site's terms of service forbid automated collection; the owner
decided on 2026-11-26 to collect anyway, for personal job search only, at their own risk.
Requests are paced by HttpClient like every other source.
"""

import html as htmllib
import logging
import os
import re
from collections.abc import Iterator

from jobservice import places
from jobservice.filters import senior_word
from jobservice.http import HttpClient
from jobservice.models import SearchFilter, Vacancy
from jobservice.text import html_to_text

log = logging.getLogger(__name__)

INDEX = "https://euremotejobs.com/sitemap.xml"
_LOC = re.compile(r"<loc>\s*(?:<!\[CDATA\[)?\s*(.*?)\s*(?:\]\]>)?\s*</loc>", re.S)
_JOB = re.compile(r"^https://euremotejobs\.com/job/([^/?#]+)/?$")


def locs(xml: str) -> list[str]:
    return _LOC.findall(xml)


def job_sitemap(index_xml: str) -> str | None:
    """The newest job_listing sitemap from the index."""
    return next((u for u in locs(index_xml) if "job_listing-sitemap" in u), None)


def job_urls(sitemap_xml: str) -> list[tuple[str, str]]:
    """(slug, url) of each job listing."""
    return [(m.group(1), url) for url in locs(sitemap_xml) if (m := _JOB.match(url))]


def from_posting(slug: str, url: str, posting: dict) -> Vacancy:
    names = places.requirement_names(posting.get("applicantLocationRequirements"))
    where = places.locality(posting)
    location = places.applicant_location(names or ([where] if where else []))
    low, high, currency, period = places.salary(posting)
    published = posting.get("datePosted")
    return Vacancy(
        source="euremotejobs",
        external_id=slug,
        title=htmllib.unescape(str(posting.get("title") or slug)).strip(),
        url=url,
        company=places.company(posting),
        description_html=posting.get("description") or None,
        description_text=html_to_text(posting.get("description") or None),
        published_at=published,
        published_raw=published,
        work_mode="remote",  # a remote-only board
        applicant_location=location,
        salary_min=low,
        salary_max=high,
        salary_currency=currency,
        salary_period=period,
        market=places.market(None, location),
    )


class EuRemoteJobs:
    name = "euremotejobs"
    # Off by default (2026-11-27): slow, timeouts, almost no junior fits.
    # Still runs with --source euremotejobs.
    enabled = False
    request_delay = 3.0  # seconds; NoFluffJobs throttled (429) at 1 s
    max_failures = 3  # in a row: the site is probably blocking, stop until next run

    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self.limit = int(os.getenv("JOBS_EUREMOTEJOBS_MAX_PAGES", "400"))

    def collect(self, flt: SearchFilter, skip_detail: set[str]) -> Iterator[Vacancy]:
        sitemap = job_sitemap(self.http.get(INDEX).text)
        if not sitemap:
            log.warning("euremotejobs: no job sitemap in the index")
            return
        jobs = job_urls(self.http.get(sitemap).text)
        todo = [(s, u) for s, u in jobs if s not in skip_detail and not senior_word(s)]
        log.info("euremotejobs: %d listings, %d new and not senior", len(jobs), len(todo))
        if len(todo) > self.limit:
            log.info("euremotejobs: opening %d now, the rest next run", self.limit)
        failures = 0
        for slug, url in todo[: self.limit]:
            try:
                posting = places.job_posting(self.http.get(url).text)
            except Exception as e:  # one broken page must not stop the run
                log.warning("euremotejobs detail %s failed: %s", slug, e)
                failures += 1
                if failures >= self.max_failures:
                    log.warning("%s: %d failures in a row, the rest waits for the next run",
                                self.name, failures)  # fmt: skip
                    return
                continue
            failures = 0
            if posting:
                yield from_posting(slug, url, posting)
            else:
                log.warning("euremotejobs detail %s: no JobPosting data", slug)
