import sqlite3
from dataclasses import asdict, dataclass, fields
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
CREATE TABLE IF NOT EXISTS scores (
    vacancy_id     INTEGER PRIMARY KEY REFERENCES vacancies (id) ON DELETE CASCADE,
    score          INTEGER NOT NULL,
    best_track     TEXT NOT NULL,
    score_ai       INTEGER NOT NULL,
    score_qa       INTEGER NOT NULL,
    score_hr       INTEGER NOT NULL,
    entry_barrier  TEXT NOT NULL,
    fits           TEXT NOT NULL,
    gaps           TEXT NOT NULL,
    model          TEXT,
    prompt_version TEXT NOT NULL,
    input_hash     TEXT NOT NULL,
    scored_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scores_score ON scores (score);
"""

_FIELDS = [f.name for f in fields(Vacancy)]
_ALWAYS_UPDATE = {"title", "url"}

# Fields that may be missing on a later run (e.g. description skipped because it's
# already stored) keep their previous value instead of being overwritten with NULL.
_UPSERT = (
    f"INSERT INTO vacancies ({', '.join(_FIELDS)}, first_seen_at, last_seen_at) "
    f"VALUES ({', '.join(':' + f for f in _FIELDS)}, :now, :now) "
    "ON CONFLICT (source, external_id) DO UPDATE SET "
    + ", ".join(
        f"{f} = excluded.{f}" if f in _ALWAYS_UPDATE else f"{f} = COALESCE(excluded.{f}, {f})"
        for f in _FIELDS
        if f not in ("source", "external_id")
    )
    + ", last_seen_at = excluded.last_seen_at"
)


STATUSES = ("new", "apply", "later", "skip")

# Columns added after the first release; connect() adds them to older databases.
_MIGRATIONS = {
    "status": "ALTER TABLE vacancies ADD COLUMN status TEXT NOT NULL DEFAULT 'new'",
    "status_reason": "ALTER TABLE vacancies ADD COLUMN status_reason TEXT",
    "status_at": "ALTER TABLE vacancies ADD COLUMN status_at TEXT",
    "salary_min": "ALTER TABLE vacancies ADD COLUMN salary_min REAL",
    "salary_max": "ALTER TABLE vacancies ADD COLUMN salary_max REAL",
    "salary_currency": "ALTER TABLE vacancies ADD COLUMN salary_currency TEXT",
    "salary_period": "ALTER TABLE vacancies ADD COLUMN salary_period TEXT",
    "work_mode": "ALTER TABLE vacancies ADD COLUMN work_mode TEXT",
    "seniority": "ALTER TABLE vacancies ADD COLUMN seniority TEXT",
    "languages": "ALTER TABLE vacancies ADD COLUMN languages TEXT",
    # Why the hard filters rejected this vacancy (None = passed). Recomputed by filters.py.
    "filter_reason": "ALTER TABLE vacancies ADD COLUMN filter_reason TEXT",
}


def connect(path: str | Path) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(vacancies)")}
    for column, ddl in _MIGRATIONS.items():
        if column not in columns:
            conn.execute(ddl)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_vacancies_status ON vacancies (status)")
    conn.commit()
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
    conn.execute(_UPSERT, {**asdict(vacancy), "now": _now()})
    conn.commit()
    return exists is None


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# Pseudo-status for the list view: still "new", but rejected by the hard filters.
FILTERED = "filtered"
VIEWS = (*STATUSES, FILTERED)


@dataclass(slots=True)
class Filters:
    status: str | None = "new"  # a value from VIEWS; None = everything
    source: str | None = None
    city: str | None = None
    q: str | None = None  # substring of title, company or description
    sort: str = "date"  # a key of SORTS


def _where(f: Filters, *, with_status: bool = True) -> tuple[str, list]:
    clauses, params = [], []
    if with_status and f.status:
        clauses.append(_STATUS_SQL[f.status])
    if f.source:
        clauses.append("source = ?")
        params.append(f.source)
    if f.city:
        clauses.append("city LIKE ?")
        params.append(f"%{f.city}%")
    if f.q:
        clauses.append("(title LIKE ? OR company LIKE ? OR description_text LIKE ?)")
        params += [f"%{f.q}%"] * 3
    return (" WHERE " + " AND ".join(clauses) if clauses else ""), params


# "new" hides filtered vacancies; decided ones show regardless of filters.
_STATUS_SQL = {
    "new": "status = 'new' AND filter_reason IS NULL",
    FILTERED: "status = 'new' AND filter_reason IS NOT NULL",
    **{s: f"status = '{s}'" for s in STATUSES if s != "new"},
}
_VIEW_SQL = (
    f"CASE WHEN status = 'new' AND filter_reason IS NOT NULL THEN '{FILTERED}' ELSE status END"
)

# Newest first; vacancies without a date go last. published_at mixes dates and
# datetimes, so compare only the date part.
_ORDER = " ORDER BY published_at IS NULL, substr(published_at, 1, 10) DESC, id DESC"
_BY_DATE = _ORDER.removeprefix(" ORDER BY ")

# Score sorts put unscored vacancies last, then fall back to the date order.
SORTS = {
    "date": _ORDER,
    **{
        key: f" ORDER BY {col} IS NULL, {col} DESC, {_BY_DATE}"
        for key, col in (("score", "score"), ("ai", "score_ai"), ("qa", "score_qa"),
                         ("hr", "score_hr"))
    },
}  # fmt: skip

_FROM = " FROM vacancies LEFT JOIN scores ON scores.vacancy_id = vacancies.id"
_SCORE_COLUMNS = "score, best_track, score_ai, score_qa, score_hr, entry_barrier"


def list_vacancies(conn: sqlite3.Connection, f: Filters, limit: int = 500) -> list[sqlite3.Row]:
    where, params = _where(f)
    sql = (
        "SELECT id, source, title, company, city, published_at, published_raw, status, "
        f"status_reason, filter_reason, {_SCORE_COLUMNS}"
        + _FROM
        + where
        + SORTS.get(f.sort, _ORDER)
        + " LIMIT ?"
    )
    return conn.execute(sql, [*params, limit]).fetchall()


def status_counts(conn: sqlite3.Connection, f: Filters) -> dict[str, int]:
    where, params = _where(f, with_status=False)
    rows = conn.execute(f"SELECT {_VIEW_SQL}, COUNT(*) FROM vacancies{where} GROUP BY 1", params)
    return dict.fromkeys(VIEWS, 0) | {r[0]: r[1] for r in rows}


def get_vacancy(conn: sqlite3.Connection, vacancy_id: int) -> sqlite3.Row | None:
    sql = f"SELECT vacancies.*, {_SCORE_COLUMNS}, fits, gaps" + _FROM + " WHERE id = ?"
    return conn.execute(sql, (vacancy_id,)).fetchone()


def next_vacancy_id(conn: sqlite3.Connection, f: Filters, current_id: int) -> int | None:
    """The vacancy after `current_id` in the filtered list (call before changing status)."""
    ids = [r["id"] for r in list_vacancies(conn, f, limit=100_000)]
    if current_id not in ids:
        return ids[0] if ids else None
    pos = ids.index(current_id)
    return ids[pos + 1] if pos + 1 < len(ids) else None


def set_status(
    conn: sqlite3.Connection, vacancy_id: int, status: str, reason: str | None = None
) -> None:
    if status not in STATUSES:
        raise ValueError(f"unknown status {status!r}")
    conn.execute(
        "UPDATE vacancies SET status = ?, status_reason = ?, status_at = ? WHERE id = ?",
        (status, reason or None, _now(), vacancy_id),
    )
    conn.commit()
