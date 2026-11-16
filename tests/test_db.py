from jobservice import db
from jobservice.models import Vacancy


def make(**kw) -> Vacancy:
    base = dict(source="jobscz", external_id="1", title="Dev", url="https://x/1")
    return Vacancy(**{**base, **kw})


def test_upsert_insert_then_update_keeps_known_fields():
    conn = db.connect(":memory:")
    assert db.upsert(conn, make(description_text="text", description_html="<p>text</p>",
                                published_at="2026-11-10")) is True  # fmt: skip
    assert db.ids_with_description(conn, "jobscz") == {"1"}

    # second run skipped the detail page: description/date are None
    assert db.upsert(conn, make(title="Dev (updated)")) is False

    row = conn.execute("SELECT * FROM vacancies").fetchone()
    assert row["title"] == "Dev (updated)"
    assert row["description_text"] == "text"
    assert row["description_html"] == "<p>text</p>"
    assert row["published_at"] == "2026-11-10"
    assert row["first_seen_at"] <= row["last_seen_at"]
    assert conn.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 1


def test_same_id_different_source_is_separate():
    conn = db.connect(":memory:")
    db.upsert(conn, make())
    db.upsert(conn, make(source="startupjobs"))
    assert conn.execute("SELECT COUNT(*) FROM vacancies").fetchone()[0] == 2
    assert db.ids_with_description(conn, "jobscz") == set()
