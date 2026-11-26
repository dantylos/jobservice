"""DOU.ua (Ukrainian IT job board) via its official RSS feeds of remote vacancies.

One feed per category and experience level, e.g.
https://jobs.dou.ua/vacancies/feeds/?category=QA&remote&exp=0-1
The feed already carries the full description, so no vacancy pages are fetched.
robots.txt (checked 2026-11-26) does not disallow the feeds for generic clients.
"""

import html as htmllib
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

FEED = "https://jobs.dou.ua/vacancies/feeds/"
DEFAULT_CATEGORIES = ("AI/ML", "Data Science", "Python", "QA", "HR")
DEFAULT_EXPERIENCE = ("0-1", "1-3")  # years; DOU's own buckets

_ID = re.compile(r"/vacancies/(\d+)/")
# "$600–800", "$2500", "від $1000", "до $3000"
_SALARY = re.compile(r"^(?:(від|до)\s+)?\$\s?([\d\s]+)(?:\s?[–-]\s?\$?\s?([\d\s]+))?$")
_REMOTE = "віддалено"
# DOU shows no period; amounts this small are hourly rates (e.g. "$10–28").
_HOURLY_BELOW = 100
_ABROAD = "за кордоном"


def feed_url(category: str, experience: str) -> str:
    # "remote" is a bare flag in DOU's URLs (…&remote&exp=…).
    return f"{FEED}?{urlencode({'category': category})}&remote&{urlencode({'exp': experience})}"


def _number(text: str) -> float:
    return float(re.sub(r"\s", "", text))


def parse_salary(token: str) -> tuple[float | None, float | None] | None:
    """(min, max) in USD for a salary token from the title, None if it is not one."""
    m = _SALARY.match(token.strip())
    if not m:
        return None
    word, low, high = m.groups()
    if word == "до":
        return None, _number(low)
    if high:
        return _number(low), _number(high)
    return _number(low), None if word == "від" else _number(low)


def parse_title(raw: str) -> dict:
    """'Junior QA в UniTalk, $600–800, Київ, віддалено' -> its parts.

    The last " в " separates the position from the company; after the company come
    comma-separated tags: salary, cities, "за кордоном" (abroad), "віддалено" (remote).
    """
    raw = htmllib.unescape(raw)  # titles carry entities such as "&amp;" and "&nbsp;"
    position, sep, rest = raw.rpartition(" в ")
    if not sep:
        return {"title": raw.strip()}
    company, *tags = [t.strip() for t in rest.split(",")]
    out: dict = {"title": position.strip(), "company": company or None}
    cities, modes = [], []
    for tag in tags:
        if tag == _REMOTE:
            modes.append("remote")
        elif tag == _ABROAD:
            continue
        elif (salary := parse_salary(tag)) is not None:
            out["salary_min"], out["salary_max"] = salary
            top = max(x for x in salary if x is not None)
            out["salary_currency"] = "USD"
            out["salary_period"] = "hour" if top < _HOURLY_BELOW else "month"
        elif tag:
            cities.append(tag)
    out["city"] = ", ".join(cities) or None
    out["work_mode"] = ",".join(modes) or None
    return out


def parse_feed(xml: str | bytes) -> list[Vacancy]:
    root = ET.fromstring(xml)
    vacancies = []
    for item in root.iter("item"):
        link = (item.findtext("link") or "").strip()
        m = _ID.search(link)
        if not m:
            log.warning("dou: item without a vacancy id: %r", link)
            continue
        html = item.findtext("description") or None
        published = item.findtext("pubDate")
        try:
            published_at = parsedate_to_datetime(published).isoformat() if published else None
        except (TypeError, ValueError):
            published_at = None
        vacancies.append(
            Vacancy(
                source="dou",
                external_id=m.group(1),
                url=link.split("?")[0],
                description_html=html,
                description_text=html_to_text(html),
                published_at=published_at,
                published_raw=published,
                market="ua",
                **parse_title(item.findtext("title") or ""),
            )
        )
    return vacancies


def _env_list(name: str, default: tuple[str, ...]) -> list[str]:
    value = os.getenv(name, "").strip()
    return [x.strip() for x in value.split(",") if x.strip()] if value else list(default)


class Dou:
    name = "dou"

    def __init__(self, http: HttpClient) -> None:
        self.http = http
        self.categories = _env_list("JOBS_DOU_CATEGORIES", DEFAULT_CATEGORIES)
        self.experience = _env_list("JOBS_DOU_EXPERIENCE", DEFAULT_EXPERIENCE)

    def collect(self, flt: SearchFilter, skip_detail: set[str]) -> Iterator[Vacancy]:
        # Keywords and city are for the Czech sites; DOU is searched by category, remote only.
        seen: set[str] = set()
        for category in self.categories:
            for experience in self.experience:
                url = feed_url(category, experience)
                log.info("dou: category=%r exp=%s", category, experience)
                try:
                    items = parse_feed(self.http.get(url).content)
                except Exception as e:  # one broken feed must not stop the run
                    log.warning("dou: feed %s failed: %s", url, e)
                    continue
                for v in items:
                    if v.external_id not in seen:
                        seen.add(v.external_id)
                        v.work_mode = v.work_mode or "remote"  # the feed is remote-only
                        yield v
