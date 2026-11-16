import json

import httpx

from jobservice.http import HttpClient
from jobservice.models import SearchFilter
from jobservice.sources.startupjobs import StartupJobs, card_to_vacancy, parse_search


def test_parse_search(fixture):
    items, has_next = parse_search(json.loads(fixture("startupjobs_search.json")))
    assert has_next
    v = card_to_vacancy(items[0])
    assert v.external_id == "108997"
    assert v.title == "Hledáme vývojáře do týmu"
    assert v.company == "Veroc Brands Group a.s."
    assert v.city == "Liberec, Turnov"  # deduplicated, order kept
    assert v.url == "https://www.startupjobs.cz/nabidka/108997/hledame-vyvojare-do-tymu"


def test_card_falls_back_to_english_and_location_name(fixture):
    items, _ = parse_search(json.loads(fixture("startupjobs_search.json")))
    v = card_to_vacancy(items[1])
    assert v.title == "Business Developer"
    assert v.city == "Praha, Česko"


def test_collect(fixture):
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/search-offers/locations-autocomplete":
            assert request.url.params["name"] == "Praha"
            return httpx.Response(200, json={"member": [
                {"location": {"locationId": "P4", "name": "Praha 4"}},
                {"location": {"locationId": "PRG", "name": "Praha"}},
            ]})  # fmt: skip
        if path == "/api/search-offers":
            bodies.append(json.loads(request.content))
            if request.url.params["page"] == "1":
                return httpx.Response(200, text=fixture("startupjobs_search.json"))
            return httpx.Response(200, json={"member": [], "view": {}})
        if path == "/api/offers/108997":
            return httpx.Response(200, text=fixture("startupjobs_offer.json"))
        return httpx.Response(500)  # detail for 108995 is broken

    with HttpClient(delay=0, retries=1, transport=httpx.MockTransport(handler)) as http:
        flt = SearchFilter(keywords=["python"], city="Praha", max_pages=5)
        vacancies = list(StartupJobs(http).collect(flt, skip_detail=set()))

    assert bodies[0] == {"query": "python", "locations": [{"locationId": "PRG", "radiusKm": 0}]}
    assert len(bodies) == 2  # stopped after the empty page
    a, b = vacancies
    assert a.description_text == "Ve Veroc rozšiřujeme tým.\n\n- full-stack\n- AI"
    assert a.description_html.startswith("<p>Ve Veroc")
    assert a.published_at == "2026-10-30T11:12:31.000Z"
    assert b.description_text is None  # failed detail doesn't stop the run
