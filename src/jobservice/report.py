"""Summary after a scheduled run (collect + score) and a desktop notification about it."""

import json
import logging
import sqlite3
import subprocess
import sys
from dataclasses import dataclass

log = logging.getLogger(__name__)

TRACK_LABELS = {"ai": "AI", "qa": "QA", "hr": "HR"}


@dataclass(slots=True)
class Summary:
    found: int  # vacancies first seen during this run
    passed: int  # ...of them, passed all filters including the score threshold
    to_review: int  # all new vacancies waiting for a decision
    best: sqlite3.Row | None  # best-scored vacancy waiting for a decision

    def message(self) -> str:
        text = (
            f"Новых вакансий: {self.found}, подходят: {self.passed}. "
            f"Ждут проверки: {self.to_review}."
        )
        if self.best is not None:
            track = TRACK_LABELS.get(self.best["best_track"], self.best["best_track"])
            company = f" ({self.best['company']})" if self.best["company"] else ""
            text += f" Лучшая: {track} {self.best['score']}, {self.best['title']}{company}."
        return text


_WAITING = "v.status = 'new' AND v.filter_reason IS NULL"


def summarize(conn: sqlite3.Connection, since: str) -> Summary:
    """`since` is the run's start time in the same ISO format as first_seen_at."""
    found, passed = conn.execute(
        f"SELECT COUNT(*), COALESCE(SUM({_WAITING}), 0) FROM vacancies v WHERE first_seen_at >= ?",
        (since,),
    ).fetchone()
    to_review = conn.execute(f"SELECT COUNT(*) FROM vacancies v WHERE {_WAITING}").fetchone()[0]
    best = conn.execute(
        "SELECT v.title, v.company, s.score, s.best_track FROM vacancies v "
        f"JOIN scores s ON s.vacancy_id = v.id WHERE {_WAITING} ORDER BY s.score DESC LIMIT 1"
    ).fetchone()
    return Summary(found, passed, to_review, best)


def notify_command(title: str, message: str) -> list[str]:
    # json.dumps gives a double-quoted string with \" and \\ escapes, which AppleScript accepts.
    # ensure_ascii=False: AppleScript does not understand \uXXXX escapes.
    msg, ttl = (json.dumps(x, ensure_ascii=False) for x in (message, title))
    script = f"display notification {msg} with title {ttl}"
    return ["osascript", "-e", script]


def notify(title: str, message: str) -> bool:
    """macOS notification; elsewhere just log. Returns True if a notification was shown."""
    if sys.platform != "darwin":
        log.info("notification skipped (not macOS): %s", message)
        return False
    try:
        subprocess.run(notify_command(title, message), check=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        log.warning("notification failed: %s", e)
        return False
    return True
