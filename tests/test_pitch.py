import json

import pytest
from test_web import request

from jobservice import db, pitch
from jobservice.models import Vacancy
from jobservice.web import App

PROFILE = "AI CV ...\nQA CV ...\nHR CV ..."
GOOD = {"language": "en", "text": "I would start by testing your release pipeline..."}


class FakeRunner:
    def __init__(self, *outputs):
        self.outputs = list(outputs)
        self.prompts = []
        self.model_used = "fake-model"

    def __call__(self, system, prompt, schema):
        self.prompts.append(prompt)
        out = self.outputs.pop(0)
        if isinstance(out, Exception):
            raise out
        return out


@pytest.fixture
def setup(tmp_path):
    path = tmp_path / "jobs.db"
    conn = db.connect(path)
    db.upsert(conn, Vacancy(source="startupjobs", external_id="1", title="QA Tester",
                            url="https://x/1", company="Acme",
                            description_text="Hledáme testera"))  # fmt: skip
    db.upsert(conn, Vacancy(source="jobscz", external_id="2", title="Recruiter",
                            url="https://x/2", description_text="Recruiter ad"))  # fmt: skip
    conn.close()
    profile = tmp_path / "profile.md"
    profile.write_text(PROFILE)
    return path, profile


def make_app(setup, *outputs):
    path, profile = setup
    runner = FakeRunner(*outputs)
    return App(path, runner=runner, profile_path=profile), runner


def test_parse_pitch_valid_and_trimmed():
    p = pitch.parse_pitch({"language": "cs", "text": "  Text  "})
    assert (p.language, p.text) == ("cs", "Text")


@pytest.mark.parametrize(
    "bad",
    [
        {"language": "de", "text": "x"},
        {"language": "en", "text": "   "},
        {"language": "en", "text": "x" * (pitch.MAX_CHARS + 1)},
        {"text": "x"},
    ],
)
def test_parse_pitch_rejects(bad):
    with pytest.raises(ValueError):
        pitch.parse_pitch(bad)


def test_prompt_has_profile_ad_and_track_hint():
    row = {"title": "QA", "company": "Acme", "city": "Praha", "description_text": "ad text",
           "best_track": "qa"}  # fmt: skip
    prompt = pitch.pitch_prompt(row, PROFILE)
    assert PROFILE in prompt and "ad text" in prompt and "company: Acme" in prompt
    assert "Best-fitting track by an earlier screening: qa" in prompt
    assert "Best-fitting" not in pitch.pitch_prompt({**row, "best_track": None}, PROFILE)


def test_system_prompt_keeps_czech_level_honest():
    assert "A2-B1" in pitch.SYSTEM_PROMPT
    assert "Never invent facts" in pitch.SYSTEM_PROMPT


def test_generate_endpoint_saves_and_returns_text(setup):
    app, runner = make_app(setup, GOOD)
    status, headers, body = request(app, "POST", "/v/1/pitch")
    assert status == "200 OK" and headers["Content-Type"].startswith("application/json")
    assert json.loads(body) == {"text": GOOD["text"], "language": "en"}
    assert "Hledáme testera" in runner.prompts[0]

    conn = db.connect(setup[0])
    row = pitch.get_pitch(conn, 1)
    assert (row["text"], row["model"]) == (GOOD["text"], "fake-model")
    conn.close()


def test_regenerate_replaces_stored_text(setup):
    app, _ = make_app(setup, GOOD, {"language": "cs", "text": "Nový text"})
    request(app, "POST", "/v/1/pitch")
    request(app, "POST", "/v/1/pitch")
    conn = db.connect(setup[0])
    assert pitch.get_pitch(conn, 1)["text"] == "Nový text"
    conn.close()


def test_model_error_returns_502_and_saves_nothing(setup):
    app, _ = make_app(setup, RuntimeError("claude error: You've hit your limit"))
    status, _, body = request(app, "POST", "/v/1/pitch")
    assert status == "502 Bad Gateway" and "limit" in json.loads(body)["error"]
    conn = db.connect(setup[0])
    assert pitch.get_pitch(conn, 1) is None
    conn.close()


def test_pitch_endpoint_refuses_cross_origin_and_unknown(setup):
    app, runner = make_app(setup, GOOD)
    status, _, _ = request(app, "POST", "/v/1/pitch", headers={"HTTP_ORIGIN": "https://evil.x"})
    assert status == "403 Forbidden"
    assert request(app, "POST", "/v/99/pitch")[0] == "404 Not Found"
    assert runner.prompts == []


def test_missing_profile_is_reported(setup, tmp_path):
    path, _ = setup
    app = App(path, runner=FakeRunner(GOOD), profile_path=tmp_path / "missing.md")
    status, _, body = request(app, "POST", "/v/1/pitch")
    assert status == "500 Internal Server Error" and "профиль" in json.loads(body)["error"]


def test_detail_auto_generates_for_new_vacancy_with_startupjobs_heading(setup):
    app, _ = make_app(setup)
    _, _, html = request(app, "GET", "/v/1")
    assert 'data-auto="1"' in html
    assert "Jak mohu vaši firmu posunout vpřed?" in html
    _, _, html = request(app, "GET", "/v/2")
    assert "Сопроводительный текст" in html  # jobs.cz: generic heading


def test_detail_shows_stored_text_without_regenerating(setup):
    app, _ = make_app(setup, {"language": "en", "text": "<b>stored</b>"})
    request(app, "POST", "/v/1/pitch")
    _, _, html = request(app, "GET", "/v/1")
    assert 'data-auto="0"' in html
    assert "&lt;b&gt;stored&lt;/b&gt;" in html
    assert "Сгенерировать заново" in html


@pytest.mark.parametrize("skipped", ["status", "filter"])
def test_no_auto_generation_for_skipped_or_filtered(setup, skipped):
    path, _ = setup
    conn = db.connect(path)
    if skipped == "status":
        db.set_status(conn, 1, "skip", "не моё")
    else:
        conn.execute("UPDATE vacancies SET filter_reason = 'Уровень: senior' WHERE id = 1")
        conn.commit()
    conn.close()
    app, _ = make_app(setup)
    _, _, html = request(app, "GET", "/v/1")
    assert 'data-auto="0"' in html and "Нажмите «Сгенерировать»" in html


@pytest.mark.parametrize("lang", ["uk", "ru"])
def test_ukrainian_and_russian_pitches_are_accepted(lang):
    assert pitch.parse_pitch({"language": lang, "text": "Текст"}).language == lang


def test_ua_market_hint_and_language_rules():
    row = {"title": "QA", "company": "UA", "city": None, "description_text": "Шукаємо QA",
           "best_track": "qa", "market": "ua"}  # fmt: skip
    assert "Market: remote job from a Ukrainian job board." in pitch.pitch_prompt(row, PROFILE)
    assert "Market:" not in pitch.pitch_prompt({**row, "market": "cz"}, PROFILE)
    assert 'Ukrainian ad ->\n  Ukrainian ("uk")' in pitch.SYSTEM_PROMPT
    assert "remotely from Prague" in pitch.SYSTEM_PROMPT
