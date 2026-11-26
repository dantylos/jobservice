import io
from urllib.parse import urlencode
from wsgiref.util import setup_testing_defaults

import pytest

from jobservice import db
from jobservice.models import Vacancy
from jobservice.web import App


def request(app: App, method: str, path: str, query: str = "", form=None, headers=None):
    body = urlencode(form or {}).encode()
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "QUERY_STRING": query,
        "CONTENT_LENGTH": str(len(body)),
        "wsgi.input": io.BytesIO(body),
        "HTTP_HOST": "127.0.0.1:8000",
        **(headers or {}),
    }
    setup_testing_defaults(environ)
    status, headers_out, payload = app.handle(environ)
    return status, dict(headers_out), payload.decode()


@pytest.fixture
def app(tmp_path):
    path = tmp_path / "jobs.db"
    conn = db.connect(path)
    for i, (title, date, city) in enumerate(
        [
            ("Python Developer", "2026-11-10", "Praha"),
            ("HR Recruiter <script>", "2026-11-12T09:00:00Z", "Brno"),
            ("Old Undated Job", None, "Praha – Smíchov"),
        ],
        start=1,
    ):
        db.upsert(conn, Vacancy(
            source="jobscz", external_id=str(i), title=title, url=f"https://x/{i}",
            company=f"Firm {i}", city=city, published_at=date,
            description_text=f"Popis {i}\n<b>not html</b>",
        ))  # fmt: skip
    conn.close()
    return App(path), path


def ids_in_order(path, **filters):
    conn = db.connect(path)
    rows = db.list_vacancies(conn, db.Filters(**filters))
    conn.close()
    return [r["title"] for r in rows]


def test_order_newest_first_undated_last(app):
    _, path = app
    assert ids_in_order(path) == ["HR Recruiter <script>", "Python Developer", "Old Undated Job"]


def test_list_page_escapes_and_filters(app):
    a, _ = app
    status, _, html = request(a, "GET", "/")
    assert status == "200 OK"
    assert (
        "HR Recruiter &lt;script&gt;" in html
        and "<script>" not in html.split("<main>")[1].split("</main>")[0]
    )
    assert 'Новые<span class="n">3</span>' in html

    _, _, html = request(a, "GET", "/", "city=praha")  # LIKE is case-insensitive
    assert "Python Developer" in html and "Old Undated Job" in html
    assert "HR Recruiter" not in html


def test_detail_shows_text_not_html(app):
    a, _ = app
    status, _, html = request(a, "GET", "/v/1")
    assert status == "200 OK"
    assert "Popis 1\n&lt;b&gt;not html&lt;/b&gt;" in html
    assert "только краткое описание" in html  # jobs.cz without description_html


def test_decision_moves_to_next_and_keeps_filters(app):
    a, path = app
    # list order for status=new: 2, 1, 3 → after deciding on 2 we go to 1
    status, headers, _ = request(
        a, "POST", "/v/2/status",
        form={"status": "skip", "reason": "не мой профиль", "f_status": "new", "f_city": "a"},
    )  # fmt: skip
    assert status == "303 See Other"
    assert headers["Location"] == "/v/1?status=new&city=a"

    conn = db.connect(path)
    row = db.get_vacancy(conn, 2)
    assert (row["status"], row["status_reason"]) == ("skip", "не мой профиль")
    assert row["status_at"]
    conn.close()
    assert ids_in_order(path) == ["Python Developer", "Old Undated Job"]
    assert ids_in_order(path, status="skip") == ["HR Recruiter <script>"]


def test_reason_only_kept_for_skip_and_last_goes_to_list(app):
    a, path = app
    _, headers, _ = request(
        a, "POST", "/v/3/status", form={"status": "apply", "reason": "x", "f_status": "new"}
    )
    assert headers["Location"] == "/?status=new"  # 3 is last in the list
    conn = db.connect(path)
    assert db.get_vacancy(conn, 3)["status_reason"] is None
    conn.close()


def test_recollect_keeps_decision(app):
    _, path = app
    conn = db.connect(path)
    db.set_status(conn, 1, "later")
    db.upsert(conn, Vacancy(source="jobscz", external_id="1", title="Renamed", url="https://x/1"))
    assert db.get_vacancy(conn, 1)["status"] == "later"
    conn.close()


@pytest.mark.parametrize(
    ("method", "path", "form", "headers", "expected"),
    [
        ("POST", "/v/1/status", {"status": "bogus"}, None, "400 Bad Request"),
        ("POST", "/v/99/status", {"status": "skip"}, None, "404 Not Found"),
        ("GET", "/v/99", None, None, "404 Not Found"),
        ("GET", "/nope", None, None, "404 Not Found"),
        ("POST", "/v/1/status", {"status": "skip"}, {"HTTP_ORIGIN": "https://evil.example"},
         "403 Forbidden"),
    ],
)  # fmt: skip
def test_errors(app, method, path, form, headers, expected):
    a, p = app
    assert request(a, method, path, form=form, headers=headers)[0] == expected
    conn = db.connect(p)
    assert db.get_vacancy(conn, 1)["status"] == "new"
    conn.close()


def test_migrates_old_database(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    old = sqlite3.connect(path)
    old.executescript(db.SCHEMA)
    old.execute(
        "INSERT INTO vacancies (source, external_id, title, url, first_seen_at, last_seen_at) "
        "VALUES ('jobscz', '1', 'T', 'u', 'x', 'x')"
    )
    old.commit()
    old.close()
    conn = db.connect(path)
    assert db.get_vacancy(conn, 1)["status"] == "new"
    assert db.get_vacancy(conn, 1)["market"] == "cz"
    conn.close()


def add_score(path, vacancy_id, ai, qa, hr, barrier="low", fits=("ok",), gaps=("<b>x</b>",)):
    from jobservice import scoring

    conn = db.connect(path)
    s = scoring.parse_score(dict(score_ai=ai, score_qa=qa, score_hr=hr, entry_barrier=barrier,
                                 fits=list(fits), gaps=list(gaps)))  # fmt: skip
    scoring.save_score(conn, vacancy_id, s, "m", "h")
    conn.close()


def test_sort_by_score_puts_unscored_last(app):
    _, path = app
    add_score(path, 1, 10, 70, 0)  # Python Developer: best QA 70
    add_score(path, 3, 50, 20, 0)  # Old Undated Job: best AI 50
    assert ids_in_order(path, sort="score") == [
        "Python Developer", "Old Undated Job", "HR Recruiter <script>"]  # fmt: skip
    assert ids_in_order(path, sort="ai")[:2] == ["Old Undated Job", "Python Developer"]
    assert ids_in_order(path)[0] == "HR Recruiter <script>"  # default: newest first


def test_list_shows_score_badge_and_keeps_sort(app):
    a, path = app
    add_score(path, 1, 10, 70, 0, barrier="medium")
    _, _, html = request(a, "GET", "/", "sort=score")
    assert 'class="score hi">QA 70</span>' in html
    assert "средний порог" in html
    assert 'href="/v/1?status=new&sort=score"' in html
    assert '<option value="score" selected>' in html
    # sorting by one track shows that track's score
    _, _, html = request(a, "GET", "/", "sort=ai")
    assert 'class="score lo">AI 10</span>' in html


def test_unknown_sort_falls_back_to_date(app):
    a, _ = app
    status, _, html = request(a, "GET", "/", "sort=;drop")
    assert status == "200 OK" and '<option value="date" selected>' in html


def test_detail_shows_fit_block_escaped(app):
    a, path = app
    add_score(path, 1, 10, 70, 5, fits=("pytest в проектах",), gaps=("<script>x</script>",))
    _, _, html = request(a, "GET", "/v/1")
    assert "Почему подходит" in html and "pytest в проектах" in html
    assert "&lt;script&gt;x&lt;/script&gt;" in html
    assert 'class="score hi">QA 70</span>' in html and 'class="score lo">HR 5</span>' in html


def test_detail_without_score_has_no_fit_block(app):
    a, _ = app
    _, _, html = request(a, "GET", "/v/2")
    assert "Почему подходит" not in html


def test_decision_keeps_score_sort_for_next(app):
    a, path = app
    add_score(path, 1, 10, 70, 0)
    add_score(path, 3, 50, 20, 0)
    _, headers, _ = request(
        a, "POST", "/v/1/status", form={"status": "later", "f_status": "new", "f_sort": "score"}
    )
    assert headers["Location"] == "/v/3?status=new&sort=score"


def test_sort_by_barrier_low_first_then_score(app):
    a, path = app
    add_score(path, 1, 10, 70, 0, barrier="high")  # Python Developer
    add_score(path, 2, 30, 0, 0, barrier="low")  # HR Recruiter
    add_score(path, 3, 50, 0, 0, barrier="medium")  # Old Undated Job
    assert ids_in_order(path, sort="barrier") == [
        "HR Recruiter <script>", "Old Undated Job", "Python Developer"]  # fmt: skip
    _, _, html = request(a, "GET", "/", "sort=barrier")
    assert '<option value="barrier" selected>' in html


def test_sort_by_barrier_ties_by_score_unscored_last(app):
    _, path = app
    add_score(path, 1, 10, 40, 0, barrier="low")
    add_score(path, 3, 80, 0, 0, barrier="low")
    assert ids_in_order(path, sort="barrier") == [
        "Old Undated Job", "Python Developer", "HR Recruiter <script>"]  # fmt: skip


def _reject(path, vid, reason):
    conn = db.connect(path)
    conn.execute("UPDATE vacancies SET filter_reason = ? WHERE id = ?", (reason, vid))
    conn.commit()
    conn.close()


def test_filtered_vacancies_are_hidden_everywhere_on_the_page(app):
    app, path = app
    _reject(path, 1, "Оценка: балл 30 < 50")
    for query in ("", "status=all", "status=filtered"):  # unknown view falls back to "new"
        _, _, html = request(app, "GET", "/", query)
        assert "Python Developer" not in html
        assert "HR Recruiter" in html
    _, _, html = request(app, "GET", "/")
    assert "Отсеянные" not in html
    assert 'Все<span class="n">2</span>' in html  # the filtered one is not counted
    assert 'Новые<span class="n">2</span>' in html


def test_decided_vacancy_shows_even_if_filters_reject_it(app):
    app, path = app
    conn = db.connect(path)
    db.set_status(conn, 1, "apply")
    conn.close()
    _reject(path, 1, "Место: Brno")
    _, _, html = request(app, "GET", "/", "status=apply")
    assert "Python Developer" in html
    _, _, html = request(app, "GET", "/", "status=all")
    assert "Python Developer" in html


def test_filtered_vacancy_still_opens_by_direct_link(app):
    app, path = app
    _reject(path, 1, "Место: Brno")
    status, _, html = request(app, "GET", "/v/1")
    assert status == "200 OK" and "Место: Brno" in html


def test_filter_reason_counts_groups_by_kind(app):
    _, path = app
    _reject(path, 1, "Оценка: балл 30 < 50")
    _reject(path, 2, "Оценка: высокий порог входа")
    _reject(path, 3, "Дубль #7")
    conn = db.connect(path)
    assert db.filter_reason_counts(conn) == {"Оценка": 2, "Дубль": 1}
    conn.execute("UPDATE vacancies SET filter_reason = 'Зарплата до 1 311 €/мес' WHERE id = 2")
    conn.execute("UPDATE vacancies SET filter_reason = 'Зарплата до 697 €/мес' WHERE id = 3")
    assert db.filter_reason_counts(conn) == {"Зарплата": 2, "Оценка": 1}
    conn.close()


def _add_ua(path, ext, title):
    conn = db.connect(path)
    db.upsert(conn, Vacancy(source="dou", external_id=ext, title=title, url=f"https://ua/{ext}",
                            company="UA Firm", work_mode="remote", market="ua"))  # fmt: skip
    conn.close()


def test_market_tabs_split_lists_and_counts(app):
    app, path = app
    _add_ua(path, "u1", "Remote QA Kyiv")
    _, _, html = request(app, "GET", "/")
    assert 'class="on" href="/?market=cz&status=new"' in html
    assert 'Чехия<span class="n">3</span>' in html
    assert 'Украина (remote)<span class="n">1</span>' in html
    assert "Python Developer" in html and "Remote QA Kyiv" not in html

    _, _, html = request(app, "GET", "/", "market=ua")
    assert "Remote QA Kyiv" in html and "Python Developer" not in html
    assert 'Новые<span class="n">1</span>' in html
    assert '<input type="hidden" name="market" value="ua">' in html
    # links inside the Ukrainian tab keep the market
    assert 'href="/v/4?market=ua' in html


def test_market_switch_keeps_sort_but_drops_search(app):
    app, path = app
    _, _, html = request(app, "GET", "/", "q=python&city=Praha&sort=score")
    assert 'href="/?market=ua&status=new&sort=score"' in html


def test_unknown_market_falls_back_to_czech(app):
    app, path = app
    _add_ua(path, "u1", "Remote QA Kyiv")
    _, _, html = request(app, "GET", "/", "market=xx")
    assert "Python Developer" in html and "Remote QA Kyiv" not in html


def test_decision_in_ua_tab_moves_to_next_ua_vacancy(app):
    app, path = app
    _add_ua(path, "u1", "Remote QA One")
    _add_ua(path, "u2", "Remote QA Two")
    status, headers, _ = request(app, "POST", "/v/5/status",
                                 form={"status": "skip", "f_market": "ua"})  # fmt: skip
    assert status.startswith("303")
    assert headers["Location"].startswith("/v/4?") and "market=ua" in headers["Location"]


def test_list_marks_applicant_location(app):
    app, path = app
    conn = db.connect(path)
    db.upsert(conn, Vacancy(source="djinni", external_id="d1", title="UA only job",
                            url="https://ua/d1", market="ua", applicant_location="UA"))  # fmt: skip
    conn.close()
    _, _, html = request(app, "GET", "/", "market=ua")
    assert "UA only job" in html
    assert '· <span class="warn">кандидаты из: UA</span></div>' in html  # "UA only" stands out
    _, _, html = request(app, "GET", "/")
    assert "кандидаты из" not in html  # Czech rows have no location stated


def test_helpers_for_readable_meta():
    from datetime import date

    from jobservice.web import _date, _languages, _meta, _rows

    row = {"published_at": "2026-11-17T10:00:00Z", "published_raw": None}
    assert _date(row, today=date(2026, 11, 26)) == "17 ноя"
    assert _date(row, today=date(2027, 1, 2)) == "17 ноя 2026"
    assert _date({"published_at": None, "published_raw": None}) == ""
    assert _date({"published_at": "bad", "published_raw": "вчера"}) == "вчера"
    assert _meta("Firm", None, "", "Praha <b>") == "Firm · Praha &lt;b&gt;"
    assert _languages("english:professional,czech:") == "English — professional, Czech"
    assert _rows("short") == 8 and _rows("x" * 900 + "\n\n" + "y") == 13


def test_city_field_only_on_czech_tab(app):
    app, _ = app
    assert 'placeholder="Город"' in request(app, "GET", "/")[2]
    assert 'placeholder="Город"' not in request(app, "GET", "/", "market=ua")[2]
