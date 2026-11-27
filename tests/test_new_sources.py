import httpx

from jobservice import places
from jobservice.http import HttpClient
from jobservice.models import SearchFilter
from jobservice.sources import SOURCES
from jobservice.sources import euremotejobs as eurj
from jobservice.sources import nextleveljobs as nlj
from jobservice.sources import nofluffjobs as nfj


def run(source_cls, handler, skip=frozenset()):
    with HttpClient(delay=0, retries=1, transport=httpx.MockTransport(handler)) as http:
        return list(source_cls(http).collect(SearchFilter(), set(skip)))


def test_registered():
    assert {"nofluffjobs", "euremotejobs", "nextleveljobs"} <= set(SOURCES)


# ---- NoFluffJobs ----------------------------------------------------------------------


def test_nofluffjobs_feed(fixture):
    vs = {v.external_id: v for v in nfj.parse_feed(fixture("nofluffjobs_feed.xml").encode())}
    trainee = vs["trainee-data-engineer-vertex-recruitments-remote-1"]
    assert (trainee.title, trainee.company, trainee.work_mode) == (
        "Trainee Data Engineer",
        "Vertex Recruitments",
        "remote",
    )
    prague = vs["finance-transformation-consultant-gbs-team-deloitte-prague"]
    assert (prague.city, prague.work_mode, prague.company) == ("Praha", None, "Deloitte")
    assert "&amp;" not in vs[next(k for k in vs if "sii-polska" in k)].title
    assert not nfj.wanted(vs["senior-database-administrator-meniga-warszawa"].url)
    assert nfj.wanted("https://nofluffjobs.com/job/qa-firm-kyiv")


def test_nofluffjobs_collect_opens_only_prague_remote_not_senior(fixture):
    opened = []

    def handler(request):
        if request.url.path == "/rss":
            return httpx.Response(200, text=fixture("nofluffjobs_feed.xml"))
        opened.append(request.url.path)
        return httpx.Response(200, text=fixture("nofluffjobs_detail.html"))

    got = run(
        nfj.NoFluffJobs,
        handler,
        skip={"finance-transformation-consultant-gbs-team-deloitte-prague"},
    )
    assert opened == ["/job/trainee-data-engineer-vertex-recruitments-remote-1"]
    (v,) = got
    assert (v.seniority, v.work_mode, v.market) == ("junior", "remote", "cz")
    assert v.applicant_location.endswith("countries incl. CZ")
    assert v.description_text.startswith("Szukamy pasjonatów")


# ---- EU Remote Jobs -------------------------------------------------------------------


def test_euremotejobs_sitemaps(fixture):
    assert eurj.job_sitemap(fixture("euremotejobs_sitemap_index.xml")) == (
        "https://euremotejobs.com/job_listing-sitemap.xml"
    )
    assert [s for s, _ in eurj.job_urls(fixture("euremotejobs_sitemap.xml"))] == [
        "principal-revenue-strategist-2",
        "junior-qa-tester",
        "customer-success-manager-emea-5",
    ]


def test_euremotejobs_collect(fixture, monkeypatch):
    monkeypatch.setenv("JOBS_EUREMOTEJOBS_MAX_PAGES", "5")
    opened = []

    def handler(request):
        path = request.url.path
        if path == "/sitemap.xml":
            return httpx.Response(200, text=fixture("euremotejobs_sitemap_index.xml"))
        if path == "/job_listing-sitemap.xml":
            return httpx.Response(200, text=fixture("euremotejobs_sitemap.xml"))
        opened.append(path)
        if "customer" in path:
            return httpx.Response(200, text="<html>no data</html>")
        return httpx.Response(200, text=fixture("euremotejobs_detail.html"))

    (v,) = run(eurj.EuRemoteJobs, handler)
    assert opened == ["/job/junior-qa-tester/", "/job/customer-success-manager-emea-5/"]
    assert (v.external_id, v.title, v.company) == (
        "junior-qa-tester",
        "Principal Revenue Strategist",  # the fixture page is reused
        "GitLab",
    )
    assert (v.work_mode, v.market, v.applicant_location) == (
        "remote",
        "cz",
        "170 countries incl. CZ",
    )
    assert v.description_text and v.published_at.startswith("2026-11-26")


def test_euremotejobs_ukraine_only_goes_to_ua_tab():
    posting = {"title": "QA", "applicantLocationRequirements": [{"name": "Ukraine"}]}
    v = eurj.from_posting("qa", "u", posting)
    assert (v.applicant_location, v.market) == ("UA", "ua")
    v = eurj.from_posting("qa", "u", {"title": "QA", "jobLocation": {"address": "Worldwide"}})
    assert (v.applicant_location, v.market) == ("Worldwide", "cz")


# ---- Next Level Jobs ------------------------------------------------------------------


def test_nextleveljobs_page(fixture):
    v = nlj.from_page("6b06", "u", fixture("nextleveljobs_detail.html"))
    assert (v.title, v.company, v.city, v.work_mode) == (
        "Product Security Engineer",
        "Supabase",
        None,
        "remote",
    )
    assert (v.seniority, v.applicant_location, v.market) == ("senior", "European Union", "cz")
    assert v.description_text.startswith("Requirements")
    assert nlj.from_page("x", "u", "<html></html>") is None


def test_nextleveljobs_onsite_salary_and_level():
    page = (
        '<script type="application/ld+json">{"@type": "JobPosting", "title": "QA Engineer",'
        ' "jobLocation": {"address": {"addressLocality": "Prague", "addressCountry": "Czechia"}},'
        ' "baseSalary": {"currency": "EUR", "value": {"minValue": 40000, "maxValue": 50000,'
        ' "unitText": "YEAR"}}}</script>'
        '<dt class="x">Experience</dt><dd><span class="y">Mid-level</span></dd>'
    )
    v = nlj.from_page("1", "u", page)
    assert (v.city, v.work_mode, v.seniority) == ("Prague", "onsite", "medior")
    assert (v.salary_min, v.salary_max, v.salary_currency, v.salary_period) == (
        40000,
        50000,
        "EUR",
        "year",
    )


def test_nextleveljobs_collect_respects_limit_and_skip(fixture, monkeypatch):
    monkeypatch.setenv("JOBS_NEXTLEVELJOBS_MAX_PAGES", "1")
    opened = []

    def handler(request):
        if request.url.path == "/jobs/sitemap.xml":
            return httpx.Response(200, text=fixture("nextleveljobs_sitemap.xml"))
        opened.append(request.url.path)
        return httpx.Response(200, text=fixture("nextleveljobs_detail.html"))

    got = run(nlj.NextLevelJobs, handler, skip={"6b06ffded8846513fe5327e2"})
    assert opened == ["/companies/telli/jobs/6b0426b6d354741332568e9d"]
    assert [v.external_id for v in got] == ["6b0426b6d354741332568e9d"]


def test_salary_helper_defaults():
    assert places.salary({}) == (None, None, None, None)
    assert places.salary({"baseSalary": {"currency": "PLN", "value": {"maxValue": 9000}}}) == (
        None,
        9000,
        "PLN",
        "year",
    )


def test_source_stops_after_failures_in_a_row(fixture):
    opened = []

    def handler(request):
        if request.url.path == "/jobs/sitemap.xml":
            return httpx.Response(200, text=fixture("nextleveljobs_sitemap.xml"))
        opened.append(request.url.path)
        return httpx.Response(403)

    class Strict(nlj.NextLevelJobs):
        max_failures = 2

    assert run(Strict, handler) == []
    assert len(opened) == 2  # the third page is left for the next run


def test_slow_sites_ask_for_a_longer_pause():
    for cls in (nfj.NoFluffJobs, eurj.EuRemoteJobs, nlj.NextLevelJobs):
        assert cls.request_delay >= 3


def test_titles_are_unescaped():
    v = eurj.from_posting("pe", "u", {"title": "Product Engineer &#8211; Integrations &amp; AI"})
    assert v.title == "Product Engineer – Integrations & AI"
    page = '<script type="application/ld+json">{"@type": "JobPosting", "title": "QA &amp; Test"}</script>'  # noqa: E501
    assert nlj.from_page("1", "u", page).title == "QA & Test"


def test_slow_sites_are_off_by_default():
    enabled = {n for n, cls in SOURCES.items() if cls.enabled}
    assert enabled == {"jobscz", "startupjobs", "dou", "djinni", "nofluffjobs"}
