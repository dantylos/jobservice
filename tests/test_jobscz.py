from datetime import date
from urllib.parse import parse_qs, urlsplit

import httpx

from jobservice.http import HttpClient
from jobservice.models import SearchFilter
from jobservice.sources.jobscz import JobsCz, parse_detail, parse_search, search_url


def test_search_url():
    assert search_url("python", "Hradec Králové", 1) == (
        "https://www.jobs.cz/prace/hradec-kralove/?q%5B%5D=python"
    )
    assert search_url("python dev", None, 2) == (
        "https://www.jobs.cz/prace/?q%5B%5D=python+dev&page=2"
    )


def test_parse_search(fixture):
    cards, has_next = parse_search(fixture("jobscz_search.html"), page=1)
    assert has_next
    assert len(cards) == 2  # the promo card without a job link is skipped
    first, second = cards
    assert first.id == "2001367610"
    assert first.title == "Python Developer – AI & data"
    assert first.company == "VLTAVA LABE MEDIA a.s."
    assert first.city == "Praha – Jinonice"
    assert first.date_raw == "10. listopadu"
    assert second.date_raw == "Končí za 3 dny"


def test_parse_search_last_page(fixture):
    _, has_next = parse_search(fixture("jobscz_search.html"), page=2)
    assert not has_next


def test_parse_detail_body(fixture):
    d = parse_detail(fixture("jobscz_detail_body.html"))
    assert 'data-jobad="body"' in d.html
    assert d.text == "Pracovní nabídka\n\nHledáme Python vývojáře.\n\n- FastAPI\n- PostgreSQL"
    assert d.published_at is None


def test_parse_detail_jsonld(fixture):
    d = parse_detail(fixture("jobscz_detail_jsonld.html"))
    assert d.html == "<p>Práce s <b>Pythonem</b>.</p><p>Brno</p>"
    assert d.text == "Práce s Pythonem.\n\nBrno"
    assert d.published_at == "2026-11-05T11:50:12+01:00"


def test_parse_detail_widget_falls_back_to_summary(fixture):
    d = parse_detail(fixture("jobscz_detail_widget.html"))
    assert d.html is None
    assert d.text == "We are thrilled to announce a new position & more."


def test_collect(fixture):
    pages = {
        "/rpd/2001367610/": fixture("jobscz_detail_jsonld.html"),
        "/rpd/2001498258/": fixture("jobscz_detail_body.html"),
    }
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url)
        if request.url.path == "/prace/praha/":
            assert parse_qs(urlsplit(str(request.url)).query)["q[]"] == ["python"]
            return httpx.Response(200, text=fixture("jobscz_search.html"))
        return httpx.Response(200, text=pages[request.url.path])

    with HttpClient(delay=0, transport=httpx.MockTransport(handler)) as http:
        src = JobsCz(http, today=date(2026, 11, 16))
        flt = SearchFilter(keywords=["python"], city="Praha", max_pages=1)
        vacancies = list(src.collect(flt, skip_detail={"2001498258"}))

    assert [v.external_id for v in vacancies] == ["2001367610", "2001498258"]
    a, b = vacancies
    assert a.url == "https://www.jobs.cz/rpd/2001367610/"
    assert a.published_at == "2026-11-05T11:50:12+01:00"  # JSON-LD beats the card date
    assert a.published_raw == "10. listopadu"
    assert a.description_text == "Práce s Pythonem.\n\nBrno"
    # skipped detail: no request, description left for the DB to keep
    assert b.description_text is None
    assert b.published_at is None
    assert all("2001498258" not in str(u) for u in requested)
