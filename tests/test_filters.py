import pytest

from jobservice import db
from jobservice.filters import (
    FilterConfig,
    apply_filters,
    check_languages,
    check_location,
    check_salary,
    check_seniority,
    dedup_key,
    monthly_eur,
)
from jobservice.models import Vacancy

CFG = FilterConfig(city="Praha", allow_remote=True, min_salary_eur=1500, eur_czk=25)


def row(**kw) -> dict:
    base = dict.fromkeys(
        ["city", "work_mode", "salary_min", "salary_max", "salary_currency", "salary_period",
         "seniority", "languages", "description_text", "company", "status"]
    )  # fmt: skip
    return base | {"title": "AI Engineer", "status": "new"} | kw


@pytest.mark.parametrize(
    ("city", "mode", "rejected"),
    [
        ("Praha – Smíchov", None, False),
        ("Hlavní město Praha", None, False),
        ("Prague", "hybrid", False),
        ("Brno", None, True),
        ("Brno", "hybrid,onsite", True),
        ("Brno", "hybrid,remote", False),
        ("Ostrava", "remote", False),
        (None, None, False),  # unknown city: don't judge
    ],
)
def test_location(city, mode, rejected):
    assert bool(check_location(row(city=city, work_mode=mode), CFG)) is rejected


def test_location_remote_not_allowed():
    cfg = FilterConfig(city="Praha", allow_remote=False)
    assert check_location(row(city="Brno", work_mode="remote"), cfg) == "Место: Brno"


def test_monthly_eur():
    assert monthly_eur(50_000, "CZK", "month", 25) == 2000
    assert monthly_eur(10, "EUR", "hour", 25) == 1600
    assert monthly_eur(36_000, "EUR", "year", 25) == 3000
    assert monthly_eur(5000, "USD", "month", 25) is None


@pytest.mark.parametrize(
    ("low", "high", "cur", "period", "rejected"),
    [
        (30_000, 35_000, "CZK", "month", True),  # 35 000 / 25 = 1400 €
        (30_000, 40_000, "CZK", "month", False),  # top of range counts
        (40_000, None, "CZK", "month", False),  # "od 40 000 Kč"
        (8, 9, "EUR", "hour", True),  # 9 × 160 = 1440 €
        (200, 300, "CZK", "hour", False),  # 300 × 160 / 25 = 1920 €
        (None, None, None, None, False),  # no salary: passes
        (1000, 1200, "USD", "month", False),  # unknown currency: passes
    ],
)
def test_salary(low, high, cur, period, rejected):
    r = row(salary_min=low, salary_max=high, salary_currency=cur, salary_period=period)
    assert bool(check_salary(r, CFG)) is rejected


def test_salary_reason_text():
    r = row(salary_min=30_000, salary_max=35_000, salary_currency="CZK", salary_period="month")
    assert check_salary(r, CFG) == "Зарплата до 1 400 €/мес"


@pytest.mark.parametrize(
    ("title", "seniority", "rejected"),
    [
        ("Senior AI Engineer", None, True),
        ("Team Lead – Recruitment", None, True),
        ("Head of HR", None, True),
        ("Sr. Python Developer", None, True),
        ("Lead Generation Specialist", None, False),
        ("IT Recruiter", None, False),
        ("Medior AI Developer", None, False),
        ("AI Engineer", "senior", True),
        ("AI Engineer", "medior", False),  # medior is kept
        ("Senior/Junior Recruiter", "junior,senior", False),  # field wins over title
    ],
)
def test_seniority(title, seniority, rejected):
    assert bool(check_seniority(row(title=title, seniority=seniority), CFG)) is rejected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Požadujeme plynulou češtinu a angličtinu.", "Чешский выше B1"),
        ("Plynulá angličtina a čeština.", "Чешский выше B1"),
        ("Čeština – rodilý mluvčí.", "Чешский выше B1"),
        ("Znalost českého jazyka na úrovni C1.", "Чешский выше B1"),
        ("Fluent Czech and English required.", "Чешский выше B1"),
        ("Czech language: native level.", "Чешский выше B1"),
        ("Fluent German is required.", "Нужен язык: german"),
        ("Aktivní znalost němčiny podmínkou.", "Нужен язык: nemciny"),
        # passes
        ("Fluent English. Czech is a plus.", None),
        ("Fluent Czech is a plus, English is a must.", None),
        ("Výborné podmínky v České republice, angličtina C1.", None),
        ("Čeština B1, angličtina C1.", None),
        ("Čeština výhodou.", None),
        ("German is an advantage.", None),
        ("Native Russian or Ukrainian speaker.", None),
        ("", None),
    ],
)
def test_languages_from_text(text, expected):
    assert check_languages(row(description_text=text), CFG) == expected


@pytest.mark.parametrize(
    ("langs", "expected"),
    [
        ("english:advanced,czech:beginner", None),
        ("czech:advanced", "Чешский выше B1"),
        ("czech:professional", "Чешский выше B1"),
        ("czech:intermediate", None),  # ~B1-B2: borderline, left to the user
        ("hungarian:professional", "Нужен язык: hungarian"),
        ("slovak:native", "Чешский выше B1"),
        ("german:advanced", "Нужен язык: german"),
        ("german:beginner", None),
        ("russian:native,english:advanced", None),
    ],
)
def test_languages_from_field(langs, expected):
    assert check_languages(row(languages=langs), CFG) == expected


def test_from_env():
    cfg = FilterConfig.from_env(
        {"JOBS_CITY": "Brno", "JOBS_INCLUDE_REMOTE": "false", "JOBS_MIN_SALARY_EUR": "2000",
         "JOBS_EUR_CZK": "24.4"}
    )  # fmt: skip
    assert (cfg.city, cfg.allow_remote, cfg.min_salary_eur, cfg.eur_czk) == (
        "Brno", False, 2000, 24.4
    )  # fmt: skip
    assert FilterConfig.from_env({}).allow_remote is True


def test_dedup_key_ignores_noise():
    a = row(title="AI Engineer (m/ž)", company="Seznam.cz, a.s.")
    b = row(title="AI engineer", company="Seznam.cz a. s.")
    assert dedup_key(a) == dedup_key(b)
    assert dedup_key(a) != dedup_key(row(title="AI Engineer", company="Other s.r.o."))


def test_apply_filters_end_to_end():
    conn = db.connect(":memory:")

    def add(source, ext, **kw):
        db.upsert(conn, Vacancy(source=source, external_id=ext, url=f"u/{ext}", **kw))

    add(
        "jobscz",
        "1",
        title="AI Engineer",
        company="Acme s.r.o.",
        city="Praha",
        description_text="short",
    )
    add(
        "startupjobs",
        "2",
        title="AI Engineer (m/ž)",
        company="Acme, s.r.o.",
        city="Praha",
        description_text="a much longer description",
    )
    add("jobscz", "3", title="Senior Recruiter", company="X", city="Praha")
    add("jobscz", "4", title="Recruiter", company="Y", city="Brno")
    add("startupjobs", "5", title="IT Recruiter", company="Z", city="Brno", work_mode="remote")
    db.set_status(conn, 1, "later")  # fmt: skip

    stats = apply_filters(conn, CFG)
    reasons = {r["id"]: r["filter_reason"] for r in conn.execute("SELECT * FROM vacancies")}
    # the decided copy wins even though its description is shorter
    assert reasons == {1: None, 2: "Дубль #1", 3: "Уровень: senior (в названии)",
                       4: "Место: Brno", 5: None}  # fmt: skip
    assert stats == {"total": 5, "passed": 2, "filtered": 3}

    titles = [r["title"] for r in db.list_vacancies(conn, db.Filters(status="new"))]
    assert titles == ["IT Recruiter"]
    filtered = db.list_vacancies(conn, db.Filters(status="filtered"))
    assert {r["id"] for r in filtered} == {2, 3, 4}
    assert db.status_counts(conn, db.Filters())["filtered"] == 3

    # settings change → recomputed from scratch
    apply_filters(conn, FilterConfig(city=None, min_salary_eur=None))
    assert conn.execute("SELECT filter_reason FROM vacancies WHERE id = 4").fetchone()[0] is None


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("HR Operations Specialist with Italian | Prague", "Нужен язык: italian (в названии)"),
        ("Asistent/ka projektového managmentu s němčinou", "Нужен язык: nemcinou (в названии)"),
        ("German speaking Recruiter", "Нужен язык: german (в названии)"),
        ("Customer Support with Polish and English", "Нужен язык: polish (в названии)"),
        ("Czech-speaking IT Recruiter", "Чешский выше B1 (в названии)"),
        ("HR Specialist s češtinou a angličtinou", "Чешский выше B1 (в названии)"),
        # passes
        ("HR Specialist s AJ", None),
        ("IT Recruiter (English, Russian)", None),
        ("AI Engineer for Czech market", None),
        ("Recruiter – Czech Republic", None),
    ],
)
def test_languages_from_title(title, expected):
    assert check_languages(row(title=title), CFG) == expected
