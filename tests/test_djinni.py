import httpx

from jobservice import db
from jobservice.http import HttpClient
from jobservice.models import SearchFilter, Vacancy
from jobservice.sources.djinni import (
    Djinni,
    _location,
    apply_detail,
    feed_url,
    job_posting,
    parse_feed,
)


def test_feed_url():
    assert feed_url("ML AI", "no_exp") == (
        "https://djinni.co/jobs/rss/?primary_keyword=ML+AI&employment=remote&exp_level=no_exp"
    )


def test_parse_feed(fixture):
    vacancies = parse_feed(fixture("djinni_feed.xml").encode())
    assert [v.external_id for v in vacancies] == ["611279", "626002", "855686", "900003"]
    a = vacancies[0]
    assert (a.source, a.market, a.work_mode) == ("djinni", "ua", "remote")
    assert a.title == "Trainee QA developer (internship)"
    assert a.url == "https://djinni.co/jobs/611279-trainee-qa-developer-internship/"
    assert a.published_at == "2026-11-26T08:57:30+02:00"
    assert a.description_text.startswith("Space Scutum is a leading company")
    assert a.company is None  # not in the feed
    b = vacancies[3]
    assert b.title == "QA & Support Engineer"  # entities decoded
    assert (b.published_at, b.description_text) == (None, None)


def test_job_posting_and_detail(fixture):
    posting = job_posting(fixture("djinni_detail.html"))
    assert posting["identifier"] == 856431  # the BreadcrumbList block is skipped
    v = Vacancy(source="djinni", external_id="856431", title="QA Engineer", url="u")
    apply_detail(v, posting)
    assert v.company == "VReal Soft"
    assert (v.salary_min, v.salary_max, v.salary_currency, v.salary_period) == (
        500,
        1000,
        "USD",
        "month",
    )
    assert v.applicant_location == "UA"
    assert job_posting("<html>no data</html>") is None


def test_location_variants():
    europe = {"address": {"addressCountry": None, "addressRegion": "Europe"}}
    assert _location(europe) == "Europe"
    assert _location([{"address": {"addressCountry": "UA"}},
                      {"address": {"addressCountry": "PL"}}]) == "UA, PL"  # fmt: skip
    assert _location([{"address": {"addressCountry": "POL"}},
                      {"address": {"addressCountry": "UKR"}},
                      {"address": {"addressCountry": "UA"}}]) == "PL, UA"  # fmt: skip
    assert _location(None) is None
    assert _location([None, "junk"]) is None


def test_detail_without_salary_keeps_none():
    v = Vacancy(source="djinni", external_id="1", title="T", url="u")
    apply_detail(v, {"hiringOrganization": {"name": "Firm"}})
    assert (v.company, v.salary_min, v.salary_currency, v.applicant_location) == (
        "Firm",
        None,
        None,
        None,
    )


def test_collect_fetches_detail_once_and_skips_unknown_keywords(fixture, monkeypatch):
    monkeypatch.setenv("JOBS_DJINNI_KEYWORDS", "QA,Bogus")
    monkeypatch.setenv("JOBS_DJINNI_EXPERIENCE", "no_exp")
    big_feed = (
        "<rss><channel>"
        + "".join(
            f"<item><title>Job {i}</title><link>https://djinni.co/jobs/{i}-job/</link></item>"
            for i in range(100)
        )
        + "</channel></rss>"
    )
    pages = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/jobs/rss/":
            if request.url.params["primary_keyword"] == "Bogus":
                return httpx.Response(200, text=big_feed)  # the site ignores unknown keys
            return httpx.Response(200, text=fixture("djinni_feed.xml"))
        pages.append(request.url.path)
        if request.url.path.startswith("/jobs/626002-"):
            return httpx.Response(500)  # a broken page must not stop the run
        return httpx.Response(200, text=fixture("djinni_detail.html"))

    with HttpClient(delay=0, retries=1, transport=httpx.MockTransport(handler)) as http:
        vacancies = list(Djinni(http).collect(SearchFilter(), skip_detail={"855686"}))

    assert [v.external_id for v in vacancies] == ["611279", "626002", "855686", "900003"]
    assert sorted(pages) == ["/jobs/611279-trainee-qa-developer-internship/",
                             "/jobs/626002-trainee-junior-manual-qa-engineer/",
                             "/jobs/900003-qa-support/"]  # fmt: skip
    by_id = {v.external_id: v for v in vacancies}
    assert by_id["611279"].company == "VReal Soft"
    assert by_id["626002"].company is None
    assert by_id["855686"].company is None  # skipped: already stored


def test_already_stored_means_company_known():
    conn = db.connect(":memory:")
    db.upsert(conn, Vacancy(source="djinni", external_id="1", title="T", url="u",
                            description_text="feed text"))  # fmt: skip
    db.upsert(conn, Vacancy(source="djinni", external_id="2", title="T", url="u",
                            description_text="feed text", company="Firm"))  # fmt: skip
    assert db.ids_with_description(conn, "djinni", Djinni.detail_column) == {"2"}
    assert db.ids_with_description(conn, "djinni") == {"1", "2"}
