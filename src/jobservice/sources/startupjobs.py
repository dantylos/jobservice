"""StartupJobs.cz via the JSON API its own website uses (back.startupjobs.cz)."""

import logging
from collections.abc import Iterator

from jobservice.http import HttpClient
from jobservice.models import SearchFilter, Vacancy
from jobservice.text import html_to_text

log = logging.getLogger(__name__)

API = "https://back.startupjobs.cz/api"
WEB = "https://www.startupjobs.cz"
_HEADERS = {"Accept": "application/ld+json", "Content-Type": "application/ld+json"}


def localized(value: dict | str | None) -> str | None:
    if isinstance(value, dict):
        return value.get("cs") or value.get("en") or None
    return value or None


def parse_search(data: dict) -> tuple[list[dict], bool]:
    return data.get("member", []), bool((data.get("view") or {}).get("next"))


def card_to_vacancy(item: dict) -> Vacancy:
    offer_id = str(item["id"])
    places = [
        localized(loc.get("place")) or localized(loc.get("name"))
        for loc in item.get("locations", [])
    ]
    city = ", ".join(dict.fromkeys(p for p in places if p)) or None
    slug = localized(item.get("slug")) or ""
    salary = item.get("salary") or {}
    return Vacancy(
        source="startupjobs",
        external_id=offer_id,
        title=localized(item.get("name")) or "",
        url=f"{WEB}/nabidka/{offer_id}/{slug}".rstrip("/"),
        company=(item.get("company") or {}).get("name"),
        city=city,
        # The API sends amounts in hundredths (3500000 = 35 000 CZK).
        salary_min=salary["min"] / 100 if salary.get("min") else None,
        salary_max=salary["max"] / 100 if salary.get("max") else None,
        salary_currency=salary.get("currency"),
        salary_period=salary.get("period"),
        work_mode=",".join(item.get("locationPreferences") or []) or None,
        seniority=",".join(item.get("seniorities") or []) or None,
    )


def apply_detail(v: Vacancy, data: dict) -> None:
    v.description_html = localized(data.get("description"))
    v.description_text = html_to_text(v.description_html)
    v.published_at = data.get("publishedAt") or data.get("createdAt")
    v.published_raw = v.published_at
    langs = []
    for req in data.get("requiredLanguages") or []:
        lang = req.get("language") or {}
        name = (lang.get("name") or {}).get("en") if isinstance(lang.get("name"), dict) else None
        name = name or str(lang.get("id") or "")
        if name:
            langs.append(f"{name.lower()}:{(req.get('level') or '').lower()}")
    v.languages = ",".join(langs) or None


class StartupJobs:
    name = "startupjobs"

    def __init__(self, http: HttpClient) -> None:
        self.http = http

    def resolve_location(self, city: str) -> str | None:
        resp = self.http.post(
            f"{API}/search-offers/locations-autocomplete",
            params={"name": city},
            json={},
            headers=_HEADERS,
        )
        hints = [m["location"] for m in resp.json().get("member", []) if m.get("location")]
        for hint in hints:
            if hint.get("name", "").casefold() == city.casefold():
                return hint["locationId"]
        return hints[0]["locationId"] if hints else None

    def collect(self, flt: SearchFilter, skip_detail: set[str]) -> Iterator[Vacancy]:
        bodies: list[dict] = [{}]
        if flt.city:
            location_id = self.resolve_location(flt.city)
            if location_id:
                bodies = [{"locations": [{"locationId": location_id, "radiusKm": 0}]}]
            else:
                log.warning("startupjobs: unknown city %r, searching without it", flt.city)
            if flt.include_remote:
                bodies.append({"locationPreferences": ["remote"]})

        seen: set[str] = set()
        for keyword in flt.keywords:
            for body in bodies:
                for page in range(1, flt.max_pages + 1):
                    log.info("startupjobs: query=%r %s page=%d", keyword, body or "", page)
                    resp = self.http.post(
                        f"{API}/search-offers",
                        params={"page": page},
                        json={**body, "query": keyword},
                        headers=_HEADERS,
                    )
                    items, has_next = parse_search(resp.json())
                    for item in items:
                        v = card_to_vacancy(item)
                        if v.external_id in seen:
                            continue
                        seen.add(v.external_id)
                        if v.external_id not in skip_detail:
                            self._fill_detail(v)
                        yield v
                    if not has_next or not items:
                        break

    def _fill_detail(self, v: Vacancy) -> None:
        try:
            resp = self.http.get(f"{API}/offers/{v.external_id}", headers=_HEADERS)
            apply_detail(v, resp.json())
        except Exception as e:  # one broken offer must not stop the run
            log.warning("startupjobs detail %s failed: %s", v.external_id, e)
