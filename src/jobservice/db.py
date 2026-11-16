import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from jobservice.models import Vacancy

SCHEMA = """
CREATE TABLE IF NOT EXISTS vacancies (
    id               INTEGER PRIMARY KEY,
    source           TEXT NOT NULL,
    external_id      TEXT NOT NULL,
    title            TEXT NOT NULL,
    company          TEXT,
    city             TEXT,
    url              TEXT NOT NULL,
    description_text TEXT,
    description_html TEXT,
    published_at     TEXT,
    published_raw    TEXT,
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    UNIQUE (source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_vacancies_published ON vacancies (published_at);
"""

# Fields that may be missing on a later run (e.g. description skipped because it's
# already stored) keep their previous value instead of being overwritten with NULL.
_UPSERT = """
INSERT INTO vacancies (source, external_id, title, company, city, url,
    description_text, description_html, published_at, published_raw,
    first_seen_at, last_seen_at)
VALUES (:source, :external_id, :title, :company, :city, :url,
    :description_text, :description_html, :published_at, :published_raw,
    :now, :now)
ON CONFLICT (source, external_id) DO UPDATE SET
    title            = excluded.title,
    company          = COALESCE(excluded.company, company),
    city             = COALESCE(excluded.city, city),
    url              = excluded.url,
    description_text = COALESCE(excluded.description_text, description_text),
    description_html = COALESCE(excluded.description_html, description_html),
    published_at     = COALESCE(excluded.published_at, published_at),
    published_raw    = COALESCE(excluded.published_raw, published_raw),
    last_seen_at     = excluded.last_seen_at
"""


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def ids_with_description(conn: sqlite3.Connection, source: str) -> set[str]:
    rows = conn.execute(
        "SELECT external_id FROM vacancies WHERE source = ? AND description_text IS NOT NULL",
        (source,),
    )
    return {r[0] for r in rows}


def upsert(conn: sqlite3.Connection, vacancy: Vacancy) -> bool:
    """Insert or update one vacancy. Returns True if it was new."""
    exists = conn.execute(
        "SELECT 1 FROM vacancies WHERE source = ? AND external_id = ?",
        (vacancy.source, vacancy.external_id),
    ).fetchone()
    now = datetime.now(UTC).isoformat(timespec="seconds")
    conn.execute(_UPSERT, {**asdict(vacancy), "now": now})
    conn.commit()
    return exists is None
