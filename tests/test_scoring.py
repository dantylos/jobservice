import json
import subprocess

import pytest

from jobservice import db, scoring
from jobservice.models import Vacancy

PROFILE = "AI CV ...\nQA CV ...\nHR CV ..."
GOOD = {
    "score_ai": 40,
    "score_qa": 82,
    "score_hr": 5,
    "entry_barrier": "low",
    "fits": ["pytest в трёх проектах"],
    "gaps": ["нет Playwright в проектах"],
}


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    for i, title in enumerate(["QA Tester", "AI Engineer", "Recruiter"], start=1):
        db.upsert(c, Vacancy(source="jobscz", external_id=str(i), title=title,
                             url=f"https://x/{i}", description_text=f"{title} ad"))  # fmt: skip
    return c


class FakeRunner:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.calls = []
        self.model_used = "fake-model"

    def __call__(self, system, prompt, schema):
        self.calls.append(prompt)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


def test_parse_score_picks_best_track():
    s = scoring.parse_score(GOOD)
    assert (s.best_track, s.score) == ("qa", 82)
    assert s.fits == ["pytest в трёх проектах"]


@pytest.mark.parametrize(
    "bad",
    [
        {**GOOD, "score_ai": 101},
        {**GOOD, "entry_barrier": "trivial"},
        {k: v for k, v in GOOD.items() if k != "gaps"},
        {**GOOD, "score_qa": "lots"},
    ],
)
def test_parse_score_rejects_bad_payload(bad):
    with pytest.raises(ValueError):
        scoring.parse_score(bad)


def test_parse_score_truncates_long_lists():
    s = scoring.parse_score({**GOOD, "fits": list("abcde")})
    assert s.fits == ["a", "b", "c"]


def test_vacancy_prompt_contains_profile_ad_and_metadata(conn):
    conn.execute("UPDATE vacancies SET salary_min = 40000, salary_currency = 'CZK', "
                 "salary_period = 'month', languages = 'czech:native' WHERE id = 1")  # fmt: skip
    row = db.get_vacancy(conn, 1)
    prompt = scoring.vacancy_prompt(row, PROFILE)
    assert PROFILE in prompt
    assert "title: QA Tester" in prompt
    assert "40000.0-? CZK per month" in prompt
    assert "czech:native" in prompt
    assert "QA Tester ad" in prompt


def test_vacancy_prompt_truncates_long_description(conn):
    conn.execute("UPDATE vacancies SET description_text = ? WHERE id = 1", ("x" * 20_000,))
    prompt = scoring.vacancy_prompt(db.get_vacancy(conn, 1), PROFILE)
    assert prompt.count("x") <= scoring.MAX_DESCRIPTION_CHARS + 10


def test_input_hash_changes_with_profile_and_text(conn):
    row = dict(db.get_vacancy(conn, 1))
    h = scoring.input_hash(row, PROFILE)
    assert h == scoring.input_hash(row, PROFILE)
    assert h != scoring.input_hash(row, PROFILE + "!")
    assert h != scoring.input_hash({**row, "description_text": "changed"}, PROFILE)


def test_pending_skips_decided_filtered_and_up_to_date(conn):
    db.set_status(conn, 2, "skip")
    conn.execute("UPDATE vacancies SET filter_reason = 'Уровень: senior' WHERE id = 3")
    db.upsert(conn, Vacancy(source="jobscz", external_id="4", title="Tester 2", url="https://x/4"))
    row4 = db.get_vacancy(conn, 4)
    scoring.save_score(conn, 4, scoring.parse_score(GOOD), "m", scoring.input_hash(row4, PROFILE))

    assert [r["id"] for r in scoring.pending(conn, PROFILE)] == [1]
    assert {r["id"] for r in scoring.pending(conn, PROFILE, rescore=True)} == {1, 4}
    # the profile changed: the stored score is stale
    assert {r["id"] for r in scoring.pending(conn, PROFILE + " v2")} == {1, 4}


def test_score_pending_saves_scores(conn):
    runner = FakeRunner([GOOD, {**GOOD, "score_ai": 90}, GOOD])
    stats = scoring.score_pending(conn, runner, PROFILE)
    assert stats == {"pending": 3, "scored": 3, "failed": 0}
    rows = {r["vacancy_id"]: r for r in conn.execute("SELECT * FROM scores")}
    assert rows[2]["score"] == 90 and rows[2]["best_track"] == "ai"  # ids are scored newest first
    assert rows[1]["model"] == "fake-model"
    assert json.loads(rows[1]["gaps"]) == ["нет Playwright в проектах"]
    assert scoring.pending(conn, PROFILE) == []


def test_score_pending_respects_limit(conn):
    stats = scoring.score_pending(conn, FakeRunner([GOOD]), PROFILE, limit=1)
    assert stats == {"pending": 1, "scored": 1, "failed": 0}


def test_score_pending_continues_after_one_failure(conn):
    runner = FakeRunner([RuntimeError("boom"), {**GOOD, "score_qa": 200}, GOOD])
    stats = scoring.score_pending(conn, runner, PROFILE)
    assert stats == {"pending": 3, "scored": 1, "failed": 2}


def test_score_pending_stops_after_failures_in_a_row(conn):
    runner = FakeRunner([RuntimeError("limit")] * 3)
    stats = scoring.score_pending(conn, runner, PROFILE, max_failures=2)
    assert stats["failed"] == 2 and len(runner.calls) == 2


def test_score_pending_handles_timeout(conn):
    runner = FakeRunner([subprocess.TimeoutExpired("claude", 1), GOOD, GOOD])
    assert scoring.score_pending(conn, runner, PROFILE)["scored"] == 2


# --- ClaudeCodeRunner --------------------------------------------------------------------------


def test_command_has_no_tools_and_optional_model():
    cmd = scoring.ClaudeCodeRunner().command("sys", {"type": "object"})
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--tools") + 1] == ""
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == {"type": "object"}
    assert "--model" not in cmd
    cmd = scoring.ClaudeCodeRunner(model="sonnet").command("sys", {})
    assert cmd[cmd.index("--model") + 1] == "sonnet"


def test_parse_output_success():
    r = scoring.ClaudeCodeRunner()
    out = {"is_error": False, "structured_output": GOOD, "modelUsage": {"claude-x": {}}}
    assert r.parse_output(0, json.dumps(out), "") == GOOD
    assert r.model_used == "claude-x"


@pytest.mark.parametrize(
    "code, stdout",
    [
        (1, "not json"),
        (0, json.dumps({"is_error": True, "result": "usage limit"})),
        (0, json.dumps({"is_error": False, "result": "plain text"})),
        (1, json.dumps({"is_error": False, "structured_output": GOOD})),
    ],
)
def test_parse_output_errors(code, stdout):
    with pytest.raises(RuntimeError):
        scoring.ClaudeCodeRunner().parse_output(code, stdout, "err")


def test_runner_reads_model_from_env(monkeypatch):
    monkeypatch.setenv("JOBS_SCORE_MODEL", "sonnet")
    assert scoring.runner_from_env().model == "sonnet"
    monkeypatch.setenv("JOBS_SCORE_MODEL", "")
    assert scoring.runner_from_env().model is None


def test_load_profile_rejects_empty(tmp_path):
    p = tmp_path / "profile.md"
    p.write_text("  \n")
    with pytest.raises(ValueError):
        scoring.load_profile(p)


def test_cli_score_without_profile_fails(tmp_path):
    from jobservice.cli import main

    code = main(["score", "--db", str(tmp_path / "j.db"), "--profile", str(tmp_path / "none.md")])
    assert code == 2


def test_cli_score_runs_with_fake_runner(tmp_path, monkeypatch):
    from jobservice.cli import main

    (tmp_path / "p.md").write_text(PROFILE)
    path = tmp_path / "j.db"
    c = db.connect(path)
    db.upsert(c, Vacancy(source="jobscz", external_id="1", title="QA", url="https://x/1"))
    c.close()
    monkeypatch.setattr(scoring, "runner_from_env", lambda: FakeRunner([GOOD]))
    assert main(["score", "--db", str(path), "--profile", str(tmp_path / "p.md")]) == 0
    assert db.connect(path).execute("SELECT score FROM scores").fetchone()[0] == 82


def test_score_pending_only_given_ids(conn):
    runner = FakeRunner([GOOD])
    stats = scoring.score_pending(conn, runner, PROFILE, ids={2})
    assert stats["scored"] == 1
    assert [r[0] for r in conn.execute("SELECT vacancy_id FROM scores")] == [2]


def test_score_filtered_vacancies_are_still_rescored(conn):
    from jobservice.filters import SCORE_REASON

    conn.execute("UPDATE vacancies SET filter_reason = ? WHERE id = 1", (SCORE_REASON + "x",))
    conn.execute("UPDATE vacancies SET filter_reason = 'Место: Brno' WHERE id = 2")
    assert {r["id"] for r in scoring.pending(conn, PROFILE)} == {1, 3}


def test_cli_score_applies_score_filter(tmp_path, monkeypatch):
    from jobservice.cli import main

    (tmp_path / "p.md").write_text(PROFILE)
    path = tmp_path / "j.db"
    c = db.connect(path)
    db.upsert(c, Vacancy(source="jobscz", external_id="1", title="QA", url="https://x/1",
                         city="Praha"))  # fmt: skip
    c.close()
    monkeypatch.setenv("JOBS_MIN_SCORE", "90")
    monkeypatch.setattr(scoring, "runner_from_env", lambda: FakeRunner([GOOD]))
    assert main(["score", "--db", str(path), "--profile", str(tmp_path / "p.md")]) == 0
    reason = db.connect(path).execute("SELECT filter_reason FROM vacancies").fetchone()[0]
    assert reason == "Оценка: балл 82 < 90"


# ---- Ukrainian remote market ----------------------------------------------------------------


def _ua_row(**kw):
    return {"title": "QA", "company": "UA Firm", "city": None, "work_mode": "remote",
            "seniority": None, "languages": None, "salary_min": None, "salary_max": None,
            "salary_currency": None, "salary_period": None, "description_text": "Шукаємо QA",
            "market": "ua", "applicant_location": "UA"} | kw  # fmt: skip


def test_czech_hash_is_unchanged_by_the_ua_rules():
    import hashlib

    row = _ua_row(market="cz", applicant_location=None)
    legacy = hashlib.sha256("\x00".join(("1", PROFILE, "QA", "Шукаємо QA")).encode())
    assert scoring.input_hash(row, PROFILE) == legacy.hexdigest()[:16]  # no Czech re-scoring
    assert scoring.input_hash(_ua_row(), PROFILE) != scoring.input_hash(row, PROFILE)
    assert scoring.system_prompt(row) == scoring.SYSTEM_PROMPT


def test_ua_prompt_marks_location_requirements_without_penalty():
    row = _ua_row()
    assert scoring.prompt_version(row) == "1+ua1"
    system = scoring.system_prompt(row)
    assert system.startswith(scoring.SYSTEM_PROMPT) and "ФОП" in system
    assert "do not lower the scores" in system
    assert "applicants must live in (stated by the job board): UA" in scoring.vacancy_prompt(
        row, PROFILE
    )


def test_score_pending_uses_ua_prompt_and_stores_its_version(conn):
    db.upsert(conn, Vacancy(source="dou", external_id="u1", title="UA QA", url="https://ua/1",
                            market="ua", description_text="Шукаємо QA"))  # fmt: skip
    systems = []

    class Recording(FakeRunner):
        def __call__(self, system, prompt, schema):
            systems.append(system)
            return super().__call__(system, prompt, schema)

    scoring.score_pending(conn, Recording([GOOD] * 4), PROFILE)
    stored = dict(conn.execute("SELECT vacancy_id, prompt_version FROM scores").fetchall())
    ua_id = conn.execute("SELECT id FROM vacancies WHERE market = 'ua'").fetchone()[0]
    assert stored[ua_id] == "1+ua1"
    assert {v for k, v in stored.items() if k != ua_id} == {"1"}
    assert sum("ФОП" in s for s in systems) == 1
    assert scoring.pending(conn, PROFILE) == []  # nothing left: hashes match the versions
