"""Djinni.co (Ukrainian IT job board) via its official RSS feeds of remote vacancies.

Feed: https://djinni.co/jobs/rss/?primary_keyword=QA&employment=remote&exp_level=no_exp
The feed has the title, full description and date but no company or salary, so for each new
vacancy its page is fetched once and the schema.org JobPosting data in it is read (company,
salary, where applicants must live). robots.txt (checked 2026-11-26) allows /jobs; the terms of
use only forbid harvesting candidates' personal data, which this does not touch.
"""

import html as htmllib
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

from jobservice.http import HttpClient
from jobservice.models import SearchFilter, Vacancy
from jobservice.text import html_to_text

log = logging.getLogger(__name__)

FEED = "https://djinni.co/jobs/rss/"
# Djinni's own category keys (from the filter on djinni.co/jobs). An unknown key is silently
# ignored by the site, which then returns the latest 100 jobs of every kind.
DEFAULT_KEYWORDS = ("ML AI", "Data Science", "Python", "QA", "QA Automation", "HR", "Recruiter")
DEFAULT_EXPERIENCE = ("no_exp", "1y")
_UNFILTERED = 100  # a feed this long means the keyword was not recognised

_ID = re.compile(r"/jobs/(\d+)-")
_JSON_LD = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)
# The site mixes ISO 3166 alpha-2 and alpha-3 codes ("UA", "UKR"); show the short form.
_ALPHA3 = {
    "UKR": "UA",
    "POL": "PL",
    "CZE": "CZ",
    "DEU": "DE",
    "SVK": "SK",
    "ROU": "RO",
    "HUN": "HU",
    "LTU": "LT",
    "LVA": "LV",
    "EST": "EE",
    "USA": "US",
    "GBR": "GB",
}
_PERIODS = {"MONTH": "month", "HOUR": "hour", "YEAR": "year", "DAY": "day", "WEEK": "week"}


def feed_url(keyword: str, experience: str) -> str:
    query = {"primary_keyword": keyword, "employment": "remote", "exp_level": experience}
    return f"{FEED}?{urlencode(query)}"


def parse_feed(xml: str | bytes) -> list[Vacancy]:
    root = ET.fromstring(xml)
    vacancies = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        m = _ID.search(link)
        if not m:
            log.warning("djinni: item without a vacancy id: %r", link)
            continue
        description = item.findtext("description") or None
        published = item.findtext("pubDate")
        try:
            published_at = parsedate_to_datetime(published).isoformat() if published else None
        except (TypeError, ValueError):
            published_at = None
        vacancies.append(
            Vacancy(
                source="djinni",
                external_id=m.group(1),
                title=htmllib.unescape(item.findtext("title") or "").strip(),
                url=link,
                description_html=description,
                description_text=html_to_text(description),
                published_at=published_at,
                published_raw=published,
                work_mode="remote",  # the feed is remote-only
                market="ua",
            )
        )
    return vacancies


def job_posting(page: str) -> dict | None:
    for m in _JSON_LD.finditer(page):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("@type") == "JobPosting":
            return data
    return None


def _location(req) -> str | None:
    """'UA', 'Europe', 'UA, PL'... from applicantLocationRequirements (one or a list)."""
    places = []
    for area in req if isinstance(req, list) else [req]:
        area = area if isinstance(area, dict) else {}
        address = area.get("address") or {}
        place = address.get("addressCountry") or address.get("addressRegion") or area.get("name")
        if place:
            places.append(_ALPHA3.get(str(place).upper(), str(place)))
    return ", ".join(dict.fromkeys(places)) or None


def apply_detail(v: Vacancy, posting: dict) -> None:
    v.company = (posting.get("hiringOrganization") or {}).get("name") or v.company
    v.applicant_location = _location(posting.get("applicantLocationRequirements"))
    salary = posting.get("baseSalary") or {}
    value = salary.get("value") or {}
    if value.get("minValue") or value.get("maxValue"):
        v.salary_min = value.get("minValue")
        v.salary_max = value.get("maxValue")
        v.salary_currency = salary.get("currency")
        v.salary_period = _PERIODS.get(str(value.get("unitText")).upper(), "month")


def _env_list(name: str, default: tuple[str, ...]) -> list[str]:
    value = os.getenv(name, "").strip()
    return [x.strip() for x in value.split(",") if x.strip()] if value else list(default)


class Djinni:
    name = "djinni"
    enabled = True
    # The feed always has the description; the page is only needed while the company is unknown.
    detail_column = "company"

    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self.keywords = _env_list("JOBS_DJINNI_KEYWORDS", DEFAULT_KEYWORDS)
        self.experience = _env_list("JOBS_DJINNI_EXPERIENCE", DEFAULT_EXPERIENCE)

    def collect(self, flt: SearchFilter, skip_detail: set[str]) -> Iterator[Vacancy]:
        # Keywords and city of the filter are for the Czech sites; Djinni uses its own categories.
        seen: set[str] = set()
        for keyword in self.keywords:
            for experience in self.experience:
                url = feed_url(keyword, experience)
                log.info("djinni: keyword=%r exp=%s", keyword, experience)
                try:
                    items = parse_feed(self.http.get(url).content)
                except Exception as e:  # one broken feed must not stop the run
                    log.warning("djinni: feed %s failed: %s", url, e)
                    continue
                if len(items) >= _UNFILTERED:
                    log.warning("djinni: keyword %r looks unknown to the site, skipped", keyword)
                    continue
                for v in items:
                    if v.external_id in seen:
                        continue
                    seen.add(v.external_id)
                    if v.external_id not in skip_detail:
                        self._fill_detail(v)
                    yield v

    def _fill_detail(self, v: Vacancy) -> None:
        try:
            posting = job_posting(self.http.get(v.url).text)
        except Exception as e:  # one broken page must not stop the run
            log.warning("djinni detail %s failed: %s", v.external_id, e)
            return
        if posting:
            apply_detail(v, posting)
        else:
            log.warning("djinni detail %s: no JobPosting data", v.external_id)
