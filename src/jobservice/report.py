"""Summary after a scheduled run (collect + score) and a desktop notification about it."""

import json
import logging
import os
import smtplib
import sqlite3
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from email.message import EmailMessage

log = logging.getLogger(__name__)

TRACK_LABELS = {"ai": "AI", "qa": "QA", "hr": "HR"}
BARRIER_LABELS = {"low": "низкий", "medium": "средний", "high": "высокий"}
REVIEW_URL = "http://127.0.0.1:8000/?sort=score"


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


# --- e-mail (Gmail SMTP, only to the sender's own address) ---------------------------------------

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465  # implicit TLS


@dataclass(slots=True)
class EmailConfig:
    address: str
    app_password: str  # a Google "app password", not the account password

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "EmailConfig | None":
        address = env.get("JOBS_GMAIL_ADDRESS", "").strip()
        password = env.get("JOBS_GMAIL_APP_PASSWORD", "").replace(" ", "").strip()
        return cls(address, password) if address and password else None


def waiting(conn: sqlite3.Connection, limit: int = 15) -> list[sqlite3.Row]:
    """Vacancies waiting for a decision, best score first (unscored last)."""
    return conn.execute(
        "SELECT v.title, v.company, v.city, v.url, s.score, s.best_track, s.entry_barrier "
        f"FROM vacancies v LEFT JOIN scores s ON s.vacancy_id = v.id WHERE {_WAITING} "
        "ORDER BY s.score IS NULL, s.score DESC, v.id DESC LIMIT ?",
        (limit,),
    ).fetchall()


def email_subject(summary: Summary) -> str:
    return f"Отчёт по вакансиям: подходят {summary.passed}, ждут проверки {summary.to_review}"


def email_body(summary: Summary, rows: list[sqlite3.Row], errors: bool = False) -> str:
    lines = [summary.message(), ""]
    if errors:
        lines += ["Во время сбора или оценки были ошибки: ~/Library/Logs/jobservice-report.log", ""]
    if rows:
        lines.append("Ждут проверки:")
        for i, r in enumerate(rows, start=1):
            if r["score"] is None:
                score = "без оценки"
            else:
                track = TRACK_LABELS.get(r["best_track"], r["best_track"])
                barrier = BARRIER_LABELS.get(r["entry_barrier"], r["entry_barrier"])
                score = f"{track} {r['score']}, порог {barrier}"
            where = ", ".join(x for x in (r["company"], r["city"]) if x)
            lines += [f"{i}. {r['title']}" + (f" ({where})" if where else ""),
                      f"   {score}", f"   {r['url']}"]  # fmt: skip
        if summary.to_review > len(rows):
            lines.append(f"...и ещё {summary.to_review - len(rows)}.")
        lines.append("")
    lines.append(f"Страница проверки (после `uv run jobservice serve`): {REVIEW_URL}")
    return "\n".join(lines)


def build_email(cfg: EmailConfig, subject: str, body: str) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = cfg.address
    msg["To"] = cfg.address  # never anyone else: this is a report to myself
    msg["Subject"] = subject
    msg.set_content(body)
    return msg


def send_email(cfg: EmailConfig, subject: str, body: str, timeout: float = 30) -> None:
    msg = build_email(cfg, subject, body)
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=timeout) as smtp:
        smtp.login(cfg.address, cfg.app_password)
        smtp.send_message(msg, from_addr=cfg.address, to_addrs=[cfg.address])
