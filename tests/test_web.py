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
