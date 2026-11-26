"""Where a vacancy is and who may apply: shared by the sources that read schema.org JobPosting.

The candidate works from Prague. A vacancy fits if it is in Prague or remote and open to
someone living in Czechia; a vacancy for people in Ukraine goes to the Ukrainian market.
"""

import json
import re

from jobservice.text import slugify

# Country names as job boards write them -> ISO 3166 alpha-2 (only the ones worth shortening).
_CODES = {
    "czech republic": "CZ",
    "czechia": "CZ",
    "ukraine": "UA",
    "poland": "PL",
    "slovakia": "SK",
    "germany": "DE",
    "austria": "AT",
    "hungary": "HU",
    "romania": "RO",
    "netherlands": "NL",
    "united kingdom": "GB",
    "united states": "US",
    "switzerland": "CH",
    "spain": "ES",
    "portugal": "PT",
    "france": "FR",
    "ireland": "IE",
    "lithuania": "LT",
    "latvia": "LV",
    "estonia": "EE",
}
_MANY = 6  # longer lists are summarised as "N countries"

UA_CITIES = {"kyiv", "kiev", "kyjiv", "lviv", "kharkiv", "odesa", "odessa", "dnipro", "ukraine"}

# Applicant regions that include someone living in Czechia.
_OPEN_TO_CZ = re.compile(
    r"\b(cz|czech\w*|europe\w*|eu|eea|emea|worldwide|anywhere|global|countries incl\. cz)\b", re.I
)


def code(name: str) -> str:
    name = name.strip()
    return _CODES.get(name.lower(), name)


def applicant_location(names: list[str]) -> str | None:
    """'CZ, SK', 'European Union' or '120 countries incl. CZ' from a list of place names."""
    places = list(dict.fromkeys(code(n) for n in names if n and n.strip() and n.strip("- ")))
    if len(places) > _MANY:
        return f"{len(places)} countries" + (" incl. CZ" if "CZ" in places else "")
    return ", ".join(places) or None


def requirement_names(req) -> list[str]:
    """Place names from applicantLocationRequirements (a dict, a list, or plain strings)."""
    names = []
    for area in req if isinstance(req, list) else [req]:
        if isinstance(area, str):
            names.append(area)
        elif isinstance(area, dict):
            address = area.get("address") if isinstance(area.get("address"), dict) else {}
            name = area.get("name") or address.get("addressCountry") or address.get("addressRegion")
            if name:
                names.append(str(name))
    return names


def open_to_czechia(location: str | None) -> bool:
    """False only when the stated applicant location clearly leaves Czechia out."""
    return not location or bool(_OPEN_TO_CZ.search(location))


def market(city: str | None, location: str | None) -> str:
    """'ua' for a job in Ukraine or only for people in Ukraine, else 'cz'."""
    if city and set(slugify(city).split("-")) & UA_CITIES:
        return "ua"
    return "ua" if location == "UA" else "cz"


_JSON_LD = re.compile(r'<script type="application/ld\+json"[^>]*>(.*?)</script>', re.S)


def job_posting(page: str) -> dict | None:
    """The first schema.org JobPosting on a page (top level, in a list or in an @graph)."""
    for m in _JSON_LD.finditer(page):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            for node in [item, *(item.get("@graph") or [])]:
                if isinstance(node, dict) and node.get("@type") == "JobPosting":
                    return node
    return None


_PERIODS = {"MONTH": "month", "HOUR": "hour", "YEAR": "year", "DAY": "day", "WEEK": "week"}


def salary(posting: dict) -> tuple[float | None, float | None, str | None, str | None]:
    """(min, max, currency, period) from baseSalary; all None when not published."""
    base = posting.get("baseSalary") if isinstance(posting.get("baseSalary"), dict) else {}
    value = base.get("value") if isinstance(base.get("value"), dict) else {}
    low, high = value.get("minValue"), value.get("maxValue")
    if not (low or high):
        return None, None, None, None
    period = _PERIODS.get(str(value.get("unitText") or base.get("unitText") or "").upper())
    return low, high, base.get("currency"), period or "year"


def company(posting: dict) -> str | None:
    org = posting.get("hiringOrganization")
    return (org.get("name") if isinstance(org, dict) else None) or None


def is_remote(posting: dict) -> bool:
    return str(posting.get("jobLocationType") or "").upper() == "TELECOMMUTE"


def locality(posting: dict) -> str | None:
    """City of the first jobLocation ("Berlin"), or a plain address string ("Worldwide")."""
    loc = posting.get("jobLocation")
    loc = loc[0] if isinstance(loc, list) and loc else loc
    address = loc.get("address") if isinstance(loc, dict) else None
    if isinstance(address, str):
        return address.strip() or None
    if isinstance(address, dict):
        return address.get("addressLocality") or None
    return None
