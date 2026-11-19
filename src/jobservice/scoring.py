"""Score vacancies against the candidate profile with an LLM (Claude Code in headless mode)."""

import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

log = logging.getLogger(__name__)

# Bump when the prompt or schema changes: stored scores with another version get redone.
PROMPT_VERSION = "1"
TRACKS = ("ai", "qa", "hr")
BARRIERS = ("low", "medium", "high")
MAX_DESCRIPTION_CHARS = 8000

SCHEMA = {
    "type": "object",
    "properties": {
        "score_ai": {"type": "integer", "minimum": 0, "maximum": 100},
        "score_qa": {"type": "integer", "minimum": 0, "maximum": 100},
        "score_hr": {"type": "integer", "minimum": 0, "maximum": 100},
        "entry_barrier": {"type": "string", "enum": list(BARRIERS)},
        "fits": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
        "gaps": {"type": "array", "items": {"type": "string"}, "maxItems": 3},
    },
    "required": ["score_ai", "score_qa", "score_hr", "entry_barrier", "fits", "gaps"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = """\
You screen job ads for one candidate. You get the candidate profile (three base CVs for three
equal career tracks plus preferences) and one job ad. Estimate, for each track, how likely this
candidate is to be invited to an interview for THIS ad if they apply with that track's CV.

Tracks: "ai" = AI / ML / LLM engineering and AI automation; "qa" = software testing, QA and test
automation; "hr" = recruiting, talent acquisition and HR.

Scoring, 0-100 per track:
- 0-10: the ad is not this kind of role at all (e.g. a sales job scores 0-10 on every track).
- Judge required experience, seniority, languages and skills against the profile. The candidate
  is a 2026 graduate with ~6 months of freelance work, so be strict:
  * the ad asks for 2+ years of experience in the role, or a senior/lead level: at most 35;
  * Czech required at a fluent, native or "very good" level: at most 30 (their Czech is A2-B1);
  * another language they lack is required (e.g. German): at most 15.
- Reward ads open to juniors, graduates or career changers, English-speaking teams, and skills
  the profile proves with real projects.

entry_barrier: how hard the ad is to get into for an entry-level candidate in general
("low" = junior/graduate friendly, "high" = clearly needs years of experience or fluent Czech).

fits / gaps: up to 3 short points each, in Russian, concrete (name the requirement and the
matching or missing evidence). Do not invent facts that are not in the profile or the ad.
"""


@dataclass(slots=True)
class Score:
    score_ai: int
    score_qa: int
    score_hr: int
    entry_barrier: str
    fits: list[str]
    gaps: list[str]

    @property
    def best_track(self) -> str:
        return max(TRACKS, key=lambda t: getattr(self, f"score_{t}"))

    @property
    def score(self) -> int:
        return getattr(self, f"score_{self.best_track}")


def parse_score(data: dict) -> Score:
    """Validate the model output (the schema is enforced upstream, but don't trust it blindly)."""
    try:
        values = {f"score_{t}": int(data[f"score_{t}"]) for t in TRACKS}
        barrier = data["entry_barrier"]
        fits, gaps = list(data["fits"]), list(data["gaps"])
    except (KeyError, TypeError, ValueError) as e:
        raise ValueError(f"bad score payload: {data!r}") from e
    if any(not 0 <= v <= 100 for v in values.values()):
        raise ValueError(f"score out of range: {values}")
    if barrier not in BARRIERS:
        raise ValueError(f"unknown entry_barrier {barrier!r}")
    return Score(
        **values,
        entry_barrier=barrier,
        fits=[str(x) for x in fits[:3]],
        gaps=[str(x) for x in gaps[:3]],
    )


def vacancy_prompt(row: sqlite3.Row | dict, profile: str) -> str:
    salary = ""
    if row["salary_min"] or row["salary_max"]:
        salary = (
            f"{row['salary_min'] or '?'}-{row['salary_max'] or '?'} "
            f"{row['salary_currency'] or ''} per {row['salary_period'] or '?'}"
        )
    meta = {
        "title": row["title"],
        "company": row["company"],
        "city": row["city"],
        "work_mode": row["work_mode"],
        "seniority (parsed by the job board)": row["seniority"],
        "languages (parsed by the job board)": row["languages"],
        "salary": salary,
    }
    lines = [f"{k}: {v}" for k, v in meta.items() if v]
    description = (row["description_text"] or "")[:MAX_DESCRIPTION_CHARS]
    return (
        f"<candidate_profile>\n{profile.strip()}\n</candidate_profile>\n\n"
        f"<job_ad>\n" + "\n".join(lines) + f"\n\n{description}\n</job_ad>"
    )


def input_hash(row: sqlite3.Row | dict, profile: str) -> str:
    """Changes when the prompt version, the profile or the ad text changes."""
    parts = (PROMPT_VERSION, profile, row["title"] or "", row["description_text"] or "")
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:16]


# A model call: (system prompt, user prompt, JSON schema) -> parsed JSON object.
Runner = Callable[[str, str, dict], dict]


class ClaudeCodeRunner:
    """Runs `claude -p` with no tools and structured output; uses the local Claude Code login."""

    def __init__(self, model: str | None = None, timeout: float = 300, binary: str = "claude"):
        self.model = model
        self.timeout = timeout
        self.binary = binary
        self.model_used: str | None = None

    def command(self, system: str, schema: dict) -> list[str]:
        cmd = [
            self.binary,
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(schema),
            "--system-prompt",
            system,
            "--tools",
            "",
            "--strict-mcp-config",
            "--no-session-persistence",
        ]
        if self.model:
            cmd += ["--model", self.model]
        return cmd

    def __call__(self, system: str, prompt: str, schema: dict) -> dict:
        # Run outside the project so its CLAUDE.md is not added to the context.
        with tempfile.TemporaryDirectory() as cwd:
            proc = subprocess.run(
                self.command(system, schema),
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                cwd=cwd,
            )
        return self.parse_output(proc.returncode, proc.stdout, proc.stderr)

    def parse_output(self, returncode: int, stdout: str, stderr: str) -> dict:
        try:
            out = json.loads(stdout)
        except json.JSONDecodeError as e:
            raise RuntimeError(f"claude exited {returncode}: {(stderr or stdout)[:300]}") from e
        if returncode != 0 or out.get("is_error"):
            raise RuntimeError(f"claude error: {str(out.get('result'))[:300]}")
        if not isinstance(out.get("structured_output"), dict):
            raise RuntimeError(f"no structured output: {str(out.get('result'))[:300]}")
        models = list(out.get("modelUsage") or {})
        self.model_used = models[0] if models else self.model
        return out["structured_output"]


def runner_from_env() -> ClaudeCodeRunner:
    return ClaudeCodeRunner(model=os.getenv("JOBS_SCORE_MODEL") or None)


def load_profile(path: str | Path) -> str:
    text = Path(path).read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"profile {path} is empty")
    return text


# --- storage (table `scores` is created in db.py) ----------------------------------------------


def save_score(
    conn: sqlite3.Connection, vacancy_id: int, s: Score, model: str | None, ihash: str
) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO scores VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            vacancy_id,
            s.score,
            s.best_track,
            s.score_ai,
            s.score_qa,
            s.score_hr,
            s.entry_barrier,
            json.dumps(s.fits, ensure_ascii=False),
            json.dumps(s.gaps, ensure_ascii=False),
            model,
            PROMPT_VERSION,
            ihash,
            datetime.now(UTC).isoformat(timespec="seconds"),
        ),
    )
    conn.commit()


def pending(conn: sqlite3.Connection, profile: str, rescore: bool = False) -> list[sqlite3.Row]:
    """New vacancies that passed the hard filters and have no up-to-date score."""
    rows = conn.execute(
        "SELECT v.*, s.input_hash AS stored_hash FROM vacancies v "
        "LEFT JOIN scores s ON s.vacancy_id = v.id "
        "WHERE v.status = 'new' AND v.filter_reason IS NULL ORDER BY v.id DESC"
    ).fetchall()
    return [r for r in rows if rescore or r["stored_hash"] != input_hash(r, profile)]


def score_pending(
    conn: sqlite3.Connection,
    runner: Runner,
    profile: str,
    limit: int | None = None,
    rescore: bool = False,
    max_failures: int = 3,
    ids: set[int] | None = None,
) -> dict[str, int]:
    """Score up to `limit` pending vacancies. Stops after `max_failures` errors in a row
    (e.g. usage limit reached), so a broken setup doesn't burn through the whole queue."""
    todo = [r for r in pending(conn, profile, rescore) if ids is None or r["id"] in ids][:limit]
    stats = {"pending": len(todo), "scored": 0, "failed": 0}
    streak = 0
    for row in todo:
        try:
            s = parse_score(runner(SYSTEM_PROMPT, vacancy_prompt(row, profile), SCHEMA))
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
            stats["failed"] += 1
            streak += 1
            log.warning("vacancy %d (%s): %s", row["id"], row["title"], e)
            if streak >= max_failures:
                log.error("stopping after %d failures in a row", streak)
                break
            continue
        streak = 0
        model = getattr(runner, "model_used", None)
        save_score(conn, row["id"], s, model, input_hash(row, profile))
        stats["scored"] += 1
        log.info("%3d %-2s %s | %s", s.score, s.best_track, row["title"], row["company"] or "")
    return stats
