import httpx
import pytest

from jobservice import db
from jobservice.filters import FilterConfig, apply_filters, monthly_eur
from jobservice.http import HttpClient
from jobservice.models import SearchFilter
from jobservice.sources.dou import Dou, feed_url, parse_feed, parse_salary, parse_title


def test_feed_url_keeps_remote_flag_and_encodes_category():
    assert feed_url("AI/ML", "0-1") == (
        "https://jobs.dou.ua/vacancies/feeds/?category=AI%2FML&remote&exp=0-1"
    )
    assert "category=Data+Science&remote&exp=1-3" in feed_url("Data Science", "1-3")


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("$600–800", (600, 800)),
        ("$2500", (2500, 2500)),
        ("$1 500–2 000", (1500, 2000)),
        ("від $1 500", (1500, None)),
        ("до $900", (None, 900)),
        ("Київ", None),
        ("віддалено", None),
    ],
)
def test_parse_salary(token, expected):
    assert parse_salary(token) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Junior QA в UniTalk, $600–800, віддалено",
         {"title": "Junior QA", "company": "UniTalk", "city": None, "work_mode": "remote",
          "salary_min": 600, "salary_max": 800, "salary_currency": "USD",
          "salary_period": "month"}),
        # " в " inside the position: the last one separates the company
        ("Рекрутер в IT-команду в Acme, Київ, Львів, за кордоном, віддалено",
         {"title": "Рекрутер в IT-команду", "company": "Acme", "city": "Київ, Львів",
          "work_mode": "remote"}),
        ("Title without company", {"title": "Title without company"}),
        # entities from the feed; small amounts are hourly rates
        ("Automation &amp; AI в WEBXLOO, $10–28, за кордоном, віддалено",
         {"title": "Automation & AI", "company": "WEBXLOO", "city": None, "work_mode": "remote",
          "salary_min": 10, "salary_max": 28, "salary_currency": "USD",
          "salary_period": "hour"}),
        ("Розробник в A1V Lab, від&nbsp;$2000, Вашингтон (США), віддалено",
         {"title": "Розробник", "company": "A1V Lab", "city": "Вашингтон (США)",
          "work_mode": "remote", "salary_min": 2000, "salary_max": None,
          "salary_currency": "USD", "salary_period": "month"}),
    ],
)  # fmt: skip
def test_parse_title(raw, expected):
    assert parse_title(raw) == expected


def test_parse_feed(fixture):
    vacancies = parse_feed(fixture("dou_feed.xml").encode())
    assert [v.external_id for v in vacancies] == ["347670", "346376", "374664", "900001", "900002"]
    a = vacancies[1]
    assert (a.source, a.market, a.title, a.company) == ("dou", "ua", "Junior QA", "UniTalk")
    assert a.url == "https://jobs.dou.ua/companies/unitalk/vacancies/346376/"  # no utm tracking
    assert (a.salary_min, a.salary_max, a.salary_currency) == (600, 800, "USD")
    assert a.published_at == "2026-11-18T10:34:22+02:00"
    assert a.description_text.startswith("Привіт! Ми — UniTalk")
    assert "<" not in a.description_text and a.description_html.startswith("<p>")
    b = vacancies[4]
    assert (b.published_at, b.published_raw) == (None, "not a date")
    assert b.description_text is None


def test_collect_reads_every_feed_dedups_and_survives_errors(fixture, monkeypatch):
    monkeypatch.setenv("JOBS_DOU_CATEGORIES", "QA, HR")
    monkeypatch.setenv("JOBS_DOU_EXPERIENCE", "0-1,1-3")
    urls = []

    def handler(request: httpx.Request) -> httpx.Response:
        urls.append(str(request.url))
        if request.url.params.get("category") == "HR" and request.url.params["exp"] == "1-3":
            return httpx.Response(500)
        return httpx.Response(200, text=fixture("dou_feed.xml"))

    with HttpClient(delay=0, retries=1, transport=httpx.MockTransport(handler)) as http:
        vacancies = list(Dou(http).collect(SearchFilter(keywords=["ignored"]), set()))

    assert len(urls) == 4  # 2 categories x 2 experience levels, the broken one included
    assert all("&remote&" in u for u in urls)
    assert len(vacancies) == 5  # the same items in every feed are yielded once
    assert all(v.work_mode == "remote" for v in vacancies)


def test_salary_exactly_at_the_minimum_passes():
    from jobservice.filters import check_salary

    r = {"salary_min": 900, "salary_max": 1100, "salary_currency": "USD",
         "salary_period": "month", "market": "ua"}  # fmt: skip
    assert check_salary(r, FilterConfig(min_salary_eur_ua=1000, eur_usd=1.1)) is None


def test_usd_salary_is_converted():
    assert monthly_eur(1100, "USD", "month", 25, 1.1) == pytest.approx(1000)
    assert monthly_eur(1100, "USD", "month", 25) is None  # no rate given: don't judge
    assert FilterConfig.from_env({"JOBS_EUR_USD": "1.2"}).eur_usd == 1.2


def test_dou_vacancies_pass_location_and_use_ua_salary_minimum(fixture):
    conn = db.connect(":memory:")
    for v in parse_feed(fixture("dou_feed.xml").encode()):
        v.work_mode = v.work_mode or "remote"
        db.upsert(conn, v)
    apply_filters(conn, FilterConfig(city="Praha", min_salary_eur=1500, min_salary_eur_ua=1000,
                                     eur_usd=1.1))  # fmt: skip
    reasons = dict(conn.execute("SELECT external_id, filter_reason FROM vacancies").fetchall())
    assert reasons["346376"] == "Зарплата до 727 €/мес"  # $800
    assert reasons["900002"] == "Зарплата до 818 €/мес"  # up to $900
    assert reasons["900001"] is None  # from $1500, Kyiv/Lviv but remote
    assert reasons["347670"] is None  # intern, no salary given
