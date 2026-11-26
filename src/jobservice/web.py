"""Local review page: list vacancies, read one, mark it apply / later / skip.

Plain WSGI on the standard library, bound to 127.0.0.1 only. Descriptions are shown
as escaped text (never the stored third-party HTML), so no foreign scripts run here.
"""

import contextlib
import json
import logging
import os
import re
import subprocess
from datetime import date
from html import escape
from pathlib import Path
from socketserver import ThreadingMixIn
from urllib.parse import parse_qs, urlencode, urlsplit
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

from jobservice import db, pitch, scoring
from jobservice.db import FILTERED, MARKETS, STATUSES, Filters

log = logging.getLogger(__name__)

STATUS_LABELS = {
    "new": "Новые",
    "apply": "Откликнуться",
    "later": "Позже",
    "skip": "Пропущенные",
    FILTERED: "Отсеянные",
}
MARKET_LABELS = {"cz": "Чехия", "ua": "Украина (remote)"}
_PERIODS = {"month": "мес", "hour": "час", "day": "день", "year": "год", "week": "нед"}
_MODES = {"remote": "удалённо", "hybrid": "гибрид", "onsite": "в офисе"}
SOURCE_LABELS = {
    "jobscz": "Jobs.cz",
    "startupjobs": "StartupJobs",
    "dou": "DOU.ua",
    "djinni": "Djinni",
    "nofluffjobs": "NoFluffJobs",
    "euremotejobs": "EU Remote Jobs",
    "nextleveljobs": "Next Level Jobs",
}
TRACK_LABELS = {"ai": "AI", "qa": "QA", "hr": "HR"}
BARRIER_LABELS = {"low": "низкий порог", "medium": "средний порог", "high": "высокий порог"}
SORT_LABELS = {
    "date": "Сначала новые",
    "score": "По баллу",
    "ai": "По баллу AI",
    "qa": "По баллу QA",
    "hr": "По баллу HR",
    "barrier": "По порогу входа",
}

Response = tuple[str, list[tuple[str, str]], bytes]


def _first(params: dict[str, list[str]], key: str) -> str | None:
    value = (params.get(key) or [""])[0].strip()
    return value or None


def filters_from(params: dict[str, list[str]], prefix: str = "") -> Filters:
    status = _first(params, prefix + "status") or "new"
    market = _first(params, prefix + "market")
    return Filters(
        status=None if status == "all" else status if status in STATUSES else "new",
        market=market if market in MARKETS else "cz",
        source=_first(params, prefix + "source"),
        city=_first(params, prefix + "city"),
        q=_first(params, prefix + "q"),
        sort=sort if (sort := _first(params, prefix + "sort")) in db.SORTS else "date",
        include_filtered=False,
    )


def filters_qs(f: Filters, **override: str) -> str:
    values = {"market": None if f.market == "cz" else f.market, "status": f.status or "all",
              "source": f.source, "city": f.city, "q": f.q,
              "sort": None if f.sort == "date" else f.sort}  # fmt: skip
    values |= override
    return urlencode({k: v for k, v in values.items() if v})


def _salary(row) -> str | None:
    low, high = row["salary_min"], row["salary_max"]
    if not (low or high):
        return None
    amount = "–".join(f"{x:,.0f}".replace(",", " ") for x in (low, high) if x)
    period = _PERIODS.get(row["salary_period"] or "month", row["salary_period"])
    return f"{amount} {row['salary_currency'] or ''}/{period}"


def _score_class(score: int) -> str:
    return "hi" if score >= 60 else "mid" if score >= 35 else "lo"


def _score_badge(row, track: str | None = None) -> str:
    """'QA 66 · низкий порог' for the list; `track` shows that track's score instead."""
    if row["score"] is None:
        return ""
    track = track if track in TRACK_LABELS else row["best_track"]
    score = row[f"score_{track}"]
    return (
        f' <span class="score {_score_class(score)}">{TRACK_LABELS[track]} {score}</span>'
        f' <span class="meta">{BARRIER_LABELS.get(row["entry_barrier"], "")}</span>'
    )


def _score_block(v) -> str:
    if v["score"] is None:
        return ""
    scores = " ".join(
        f'<span class="score {_score_class(v[f"score_{t}"])}">{label} {v[f"score_{t}"]}</span>'
        for t, label in TRACK_LABELS.items()
    )

    def items(column: str) -> str:
        return "".join(f"<li>{_e(x)}</li>" for x in json.loads(v[column] or "[]"))

    return (
        f'<section class="fit"><div>{scores} '
        f'<span class="meta">{BARRIER_LABELS.get(v["entry_barrier"], "")}</span></div>'
        f'<div class="cols"><div><b>Почему подходит</b><ul>{items("fits")}</ul></div>'
        f"<div><b>Чего не хватает</b><ul>{items('gaps')}</ul></div></div></section>"
    )


PITCH_HEADINGS = {"startupjobs": "Jak mohu vaši firmu posunout vpřed?"}


def _pitch_block(conn, v) -> str:
    """Ready-to-paste application text; generated on open (via JS) unless already stored."""
    stored = pitch.get_pitch(conn, v["id"])
    auto = stored is None and _view(v) not in (FILTERED, "skip")
    if stored:
        status = f"Готово · {stored['language'].upper()} · {_e(stored['created_at'][:10])}"
    elif auto:
        status = "Генерирую текст, 10–20 секунд…"
    else:
        status = "Нажмите «Сгенерировать»"
    heading = PITCH_HEADINGS.get(v["source"], "Сопроводительный текст")
    button = "Сгенерировать заново" if stored else "Сгенерировать"
    return (
        f'<section class="pitch" data-id="{v["id"]}" data-auto="{int(auto)}">'
        f'<div><b>{_e(heading)}</b> <span class="meta" id="pitch-status">{status}</span></div>'
        f'<textarea id="pitch-text" rows="{_rows(stored["text"] if stored else "")}">'
        f"{_e(stored['text']) if stored else ''}</textarea>"
        '<div class="pitch-actions"><button type="button" id="pitch-copy">Копировать</button>'
        f'<button type="button" id="pitch-regen">{button}</button></div></section>'
    )


def _view(row) -> str:
    return FILTERED if row["status"] == "new" and row["filter_reason"] else row["status"]


_MONTHS = ("янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек")


def _date(row, today: date | None = None) -> str:
    """'17 ноя' (the year only when it is not the current one); '' when unknown."""
    raw = row["published_at"]
    try:
        d = date.fromisoformat(raw[:10]) if raw else None
    except ValueError:
        d = None
    if d is None:
        return row["published_raw"] or ""
    text = f"{d.day} {_MONTHS[d.month - 1]}"
    return text if d.year == (today or date.today()).year else f"{text} {d.year}"


def _rows(text: str, width: int = 90) -> int:
    """Rough height of a text in lines, so the whole letter shows even without JavaScript."""
    lines = sum(max(1, -(-len(line) // width)) for line in text.split("\n"))
    return max(8, lines + 1)


def _meta(*parts: str | None) -> str:
    """Escaped, non-empty parts joined with " · " (missing fields are left out, not "—")."""
    return " · ".join(_e(p) for p in parts if p)


def _languages(value: str | None) -> str:
    """'english:professional,czech:' -> 'English — professional, Czech'."""
    out = []
    for item in (value or "").split(","):
        name, _, level = item.partition(":")
        if name.strip():
            out.append(name.strip().capitalize() + (f" — {level.strip()}" if level.strip() else ""))
    return ", ".join(out)


def _location(value: str | None) -> str:
    """Applicant location; "UA only" is highlighted, it may rule out living in Prague."""
    if not value:
        return ""
    cls = "warn" if {x.strip() for x in value.split(",")} == {"UA"} else ""
    return f'<span class="{cls}">кандидаты из: {_e(value)}</span>'


def _e(value) -> str:
    return escape(str(value)) if value is not None else ""


CSS = """
:root { --bg:#fff; --fg:#1d1d1f; --muted:#6e6e73; --line:#e5e5ea; --card:#f6f6f8;
  --accent:#0a66c2; --apply:#1a7f37; --later:#9a6700; --skip:#b42318; }
@media (prefers-color-scheme: dark) { :root { --bg:#16161a; --fg:#ececf1; --muted:#9a9aa3;
  --line:#2c2c33; --card:#1f1f25; --accent:#6aa9ff; --apply:#4ac26b; --later:#d4a72c;
  --skip:#f47067; } }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--fg);
  font:15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }
main { max-width: 960px; margin: 0 auto; padding: 24px 16px 64px; }
a { color: var(--accent); text-decoration: none; } a:hover { text-decoration: underline; }
h1 { font-size: 22px; margin: 0 0 16px; line-height:1.3; } h1 a { color: inherit; }
h1.top { margin-bottom:28px; }
.tabs { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:24px; }
.tabs a { padding:5px 12px; border-radius:999px; background:var(--card); color:var(--fg);
  font-size:14px; }
.tabs a.on { background:var(--fg); color:var(--bg); }
.tabs .n { color:var(--muted); margin-left:4px; } .tabs a.on .n { color:inherit; opacity:.7; }
/* Markets: the main switch, larger and outlined; statuses below are small pills. */
.tabs.markets { gap:8px; margin-bottom:20px; }
.tabs.markets a { font-size:16px; font-weight:600; padding:9px 18px; border-radius:10px;
  background:transparent; border:1px solid var(--line); }
.tabs.markets a.on { background:var(--accent); border-color:var(--accent); color:var(--bg); }
.warn { color:var(--later); font-weight:600; }
h2.sec { font-size:15px; margin:28px 0 8px; }
form.filters { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:24px; }
input, select, button { font:inherit; padding:6px 10px; border:1px solid var(--line);
  border-radius:8px; background:var(--bg); color:var(--fg); }
input[name=q] { flex: 1 1 220px; }
button { cursor:pointer; background:var(--card); }
.list { list-style:none; margin:0; padding:0; border-top:1px solid var(--line); }
.list li { padding:12px 2px; border-bottom:1px solid var(--line); }
.list .meta { margin-top:3px; }
.list .t { font-weight:600; }
.meta { color:var(--muted); font-size:13px; }
.badge { font-size:12px; padding:1px 8px; border-radius:999px; border:1px solid currentColor; }
.s-filtered { color:var(--muted); } .s-apply { color:var(--apply); }
.s-later { color:var(--later); } .s-skip { color:var(--skip); }
.empty { color:var(--muted); padding:24px 0; }
.desc { white-space:pre-wrap; background:var(--card); padding:16px; border-radius:10px;
  margin:0 0 24px; overflow-wrap:anywhere; }
.facts { margin-top:4px; }
.note { color:var(--muted); font-size:13px; }
.actions { display:flex; flex-wrap:wrap; gap:8px; align-items:center; position:sticky;
  bottom:0; background:var(--bg); padding:12px 0 calc(12px + env(safe-area-inset-bottom, 0px));
  border-top:1px solid var(--line); }
.actions .apply { border-color:var(--apply); color:var(--apply); }
.actions .later { border-color:var(--later); color:var(--later); }
.actions .skip { border-color:var(--skip); color:var(--skip); }
.actions input { flex: 1 1 200px; }
kbd { font-size:11px; color:var(--muted); }
.score { font-size:12px; font-weight:600; padding:1px 8px; border-radius:6px;
  background:var(--card); white-space:nowrap; }
.score.hi { color:var(--apply); } .score.mid { color:var(--later); }
.score.lo { color:var(--muted); }
.fit { border:1px solid var(--line); border-radius:10px; padding:14px 18px; margin:24px 0; }
.fit .cols { display:grid; grid-template-columns:repeat(auto-fit, minmax(260px, 1fr));
  gap:8px 24px; margin-top:8px; }
.fit ul { margin:4px 0 0; padding-left:18px; } .fit li { margin:2px 0; }
.pitch { border:1px solid var(--line); border-radius:10px; padding:14px 18px; margin:24px 0; }
.pitch textarea { width:100%; margin:8px 0; font:inherit; padding:10px; border-radius:8px;
  border:1px solid var(--line); background:var(--bg); color:var(--fg); resize:vertical; }
.pitch-actions { display:flex; gap:8px; }
"""

KEYS_JS = """
document.addEventListener('keydown', e => {
  if (e.target.matches('input, select, textarea') || e.metaKey || e.ctrlKey) return;
  const b = document.querySelector('button[data-key="' + e.key + '"]');
  if (b) { e.preventDefault(); b.click(); }
});
"""


PITCH_JS = """
const box = document.querySelector('.pitch');
if (box) {
  const text = document.getElementById('pitch-text');
  const status = document.getElementById('pitch-status');
  const regen = document.getElementById('pitch-regen');
  const copy = document.getElementById('pitch-copy');
  // The whole letter is visible: the field grows with its text.
  function fit() { text.style.height = 'auto'; text.style.height = (text.scrollHeight + 2) + 'px'; }
  text.addEventListener('input', fit);
  fit();
  async function generate() {
    regen.disabled = true;
    status.textContent = 'Генерирую текст, 10–20 секунд…';
    try {
      const r = await fetch('/v/' + box.dataset.id + '/pitch', {method: 'POST'});
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || r.status);
      text.value = d.text;
      fit();
      status.textContent = 'Готово · ' + d.language.toUpperCase();
      regen.textContent = 'Сгенерировать заново';
    } catch (e) {
      status.textContent = 'Ошибка: ' + e.message;
    } finally {
      regen.disabled = false;
    }
  }
  regen.addEventListener('click', generate);
  copy.addEventListener('click', async () => {
    await navigator.clipboard.writeText(text.value);
    copy.textContent = 'Скопировано';
    setTimeout(() => { copy.textContent = 'Копировать'; }, 1500);
  });
  if (box.dataset.auto === '1') generate();
}
"""


def page(title: str, body: str, script: str = "") -> bytes:
    return (
        "<!doctype html><html lang=ru><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<title>{_e(title)}</title><style>{CSS}</style></head>"
        f"<body><main>{body}</main><script>{KEYS_JS}{script}</script></body></html>"
    ).encode()


def render_list(conn, f: Filters) -> bytes:
    # Market tabs: the number is the new vacancies waiting for a decision there.
    waiting = db.market_counts(conn)
    # Switching market starts from its "new" list; only the sort order carries over.
    markets = "".join(
        f'<a class="{"on" if f.market == m else ""}" '
        f'href="/?{filters_qs(Filters(market=m, sort=f.sort), market=m)}">'
        f'{MARKET_LABELS[m]}<span class="n">{waiting[m]}</span></a>'
        for m in MARKETS
    )
    counts = db.status_counts(conn, f)
    tabs = []
    # Vacancies rejected by the filters are not shown at all (they stay in the database).
    for key in (*STATUSES, "all"):
        n = sum(counts[s] for s in STATUSES) if key == "all" else counts[key]
        label = "Все" if key == "all" else STATUS_LABELS[key]
        on = " on" if (f.status or "all") == key else ""
        tabs.append(
            f'<a class="{on.strip()}" href="/?{filters_qs(f, status=key)}">'
            f'{label}<span class="n">{n}</span></a>'
        )

    sources = "".join(
        f'<option value="{k}"{" selected" if f.source == k else ""}>{v}</option>'
        for k, v in SOURCE_LABELS.items()
    )
    sorts = "".join(
        f'<option value="{k}"{" selected" if f.sort == k else ""}>{v}</option>'
        for k, v in SORT_LABELS.items()
    )
    filters = (
        '<form class="filters" method="get" action="/">'
        f'<input type="hidden" name="market" value="{_e(f.market)}">'
        f'<input type="hidden" name="status" value="{_e(f.status or "all")}">'
        f'<input name="q" value="{_e(f.q)}" placeholder="Поиск: должность, компания, текст">'
        + (
            f'<input name="city" value="{_e(f.city)}" placeholder="Город">'
            if f.market != "ua"
            else ""
        )  # Ukrainian vacancies are all remote
        + f'<select name="source"><option value="">Все сайты</option>{sources}</select>'
        f'<select name="sort">{sorts}</select>'
        "<button>Найти</button></form>"
    )

    rows = db.list_vacancies(conn, f)
    items = []
    for r in rows:
        badge = ""
        view = _view(r)
        if view != "new" and f.status is None:
            badge = f' <span class="badge s-{view}">{STATUS_LABELS[view]}</span>'
        meta = _meta(r["company"], r["city"], _date(r), SOURCE_LABELS.get(r["source"], r["source"]),
                     r["status_reason"])  # fmt: skip
        if r["applicant_location"]:
            meta += " · " + _location(r["applicant_location"])
        items.append(
            f'<li><a class="t" href="/v/{r["id"]}?{filters_qs(f)}">{_e(r["title"])}</a>'
            f'{_score_badge(r, f.sort)}{badge}<div class="meta">{meta}</div></li>'
        )
    listing = (
        f'<ul class="list">{"".join(items)}</ul>'
        if items
        else ('<p class="empty">Ничего не найдено.</p>')
    )
    return page("Вакансии", f'<h1 class="top"><a href="/">Вакансии</a></h1>'
                f'<nav class="tabs markets">{markets}</nav><nav class="tabs">'
                f'{"".join(tabs)}</nav>{filters}{listing}')  # fmt: skip


def render_detail(conn, vacancy_id: int, f: Filters) -> bytes | None:
    v = db.get_vacancy(conn, vacancy_id)
    if v is None:
        return None
    status = ""
    if _view(v) == FILTERED:
        status = (
            f'<p><span class="badge s-{FILTERED}">Отсеяна</span>: {_e(v["filter_reason"])}. '
            "Можно всё равно откликнуться или пропустить.</p>"
        )
    elif v["status"] != "new":
        reason = f": {_e(v['status_reason'])}" if v["status_reason"] else ""
        status = (
            f'<p><span class="badge s-{v["status"]}">{STATUS_LABELS[v["status"]]}</span>'
            f"{reason}</p>"
        )
    note = ""
    if v["source"] == "jobscz" and not v["description_html"]:
        note = (
            '<p class="note">У этой вакансии сохранено только краткое описание, '
            "полный текст на сайте.</p>"
        )
    hidden = "".join(
        f'<input type="hidden" name="f_{k}" value="{_e(val)}">'
        for k, vals in parse_qs(filters_qs(f)).items()
        for val in vals
    )
    actions = (
        f'<form class="actions" method="post" action="/v/{v["id"]}/status">{hidden}'
        '<button class="apply" name="status" value="apply" data-key="a">'
        "Откликаться <kbd>A</kbd></button>"
        '<button class="later" name="status" value="later" data-key="l">'
        "Позже <kbd>L</kbd></button>"
        '<input name="reason" placeholder="Причина (для «Пропустить»)" '
        f'value="{_e(v["status_reason"])}">'
        '<button class="skip" name="status" value="skip" data-key="s">'
        "Пропустить <kbd>S</kbd></button>"
        '<button name="status" value="new" data-key="n">Вернуть в новые</button></form>'
    )
    facts = [
        _salary(v),
        ", ".join(_MODES.get(m, m) for m in (v["work_mode"] or "").split(",") if m),
        (v["seniority"] or "").replace(",", ", "),
        _languages(v["languages"]),
    ]
    facts_html = _meta(*facts)
    if v["applicant_location"]:
        facts_html += (" · " if facts_html else "") + _location(v["applicant_location"])
    body = (
        f'<p><a href="/?{filters_qs(f)}">← К списку</a></p>'
        f"<h1>{_e(v['title'])}</h1>"
        f'<div class="meta">{
            _meta(v["company"], v["city"], _date(v), SOURCE_LABELS.get(v["source"], v["source"]))
        } · '
        f'<a href="{_e(v["url"])}" target="_blank" rel="noopener noreferrer">'
        "Открыть на сайте ↗</a></div>"
        + (f'<div class="meta facts">{facts_html}</div>' if facts_html else "")
        + f"{status}{_score_block(v)}{_pitch_block(conn, v)}{note}"
        '<h2 class="sec">Описание вакансии</h2>'
        f'<div class="desc">{_e(v["description_text"]) or "Описание не сохранено."}</div>'
        f"{actions}"
    )
    return page(v["title"], body, PITCH_JS)


class App:
    def __init__(
        self,
        db_path: str | Path,
        runner: scoring.Runner | None = None,
        profile_path: str | Path | None = None,
    ) -> None:
        self.db_path = db_path
        self.runner = runner
        self.profile_path = profile_path or os.getenv("JOBS_PROFILE_PATH", "data/profile.md")
        db.connect(db_path).close()  # create / migrate once at startup

    def __call__(self, environ, start_response):
        status, headers, body = self.handle(environ)
        start_response(status, [*headers, ("Content-Length", str(len(body)))])
        return [body]

    def handle(self, environ) -> Response:
        method = environ["REQUEST_METHOD"]
        path = environ.get("PATH_INFO", "/")
        query = parse_qs(environ.get("QUERY_STRING", ""))
        conn = db.connect(self.db_path)
        try:
            if method == "GET" and path == "/":
                return _html(render_list(conn, filters_from(query)))
            if method == "GET" and (m := re.fullmatch(r"/v/(\d+)", path)):
                html = render_detail(conn, int(m.group(1)), filters_from(query))
                return _html(html) if html else _text("404 Not Found", "Вакансия не найдена")
            if method == "POST" and (m := re.fullmatch(r"/v/(\d+)/status", path)):
                return self._set_status(conn, environ, int(m.group(1)))
            if method == "POST" and (m := re.fullmatch(r"/v/(\d+)/pitch", path)):
                return self._make_pitch(conn, environ, int(m.group(1)))
            return _text("404 Not Found", "Not found")
        finally:
            conn.close()

    def _make_pitch(self, conn, environ, vacancy_id: int) -> Response:
        if not _same_origin(environ):
            return _json("403 Forbidden", {"error": "cross-origin request refused"})
        v = db.get_vacancy(conn, vacancy_id)
        if v is None:
            return _json("404 Not Found", {"error": "вакансия не найдена"})
        try:
            profile = scoring.load_profile(self.profile_path)
        except (OSError, ValueError) as e:
            return _json("500 Internal Server Error", {"error": f"профиль: {e}"})
        runner = self.runner or scoring.runner_from_env()
        try:
            p = pitch.generate(v, profile, runner)
        except (RuntimeError, ValueError, subprocess.TimeoutExpired) as e:
            log.warning("pitch for vacancy %d: %s", vacancy_id, e)
            return _json("502 Bad Gateway", {"error": str(e)[:300]})
        model = getattr(runner, "model_used", None)
        pitch.save_pitch(conn, vacancy_id, p, model, pitch.input_hash(v, profile))
        return _json("200 OK", {"text": p.text, "language": p.language})

    def _set_status(self, conn, environ, vacancy_id: int) -> Response:
        if not _same_origin(environ):
            return _text("403 Forbidden", "Cross-origin request refused")
        if db.get_vacancy(conn, vacancy_id) is None:
            return _text("404 Not Found", "Вакансия не найдена")
        size = int(environ.get("CONTENT_LENGTH") or 0)
        form = parse_qs(environ["wsgi.input"].read(size).decode())
        status = _first(form, "status")
        if status not in STATUSES:
            return _text("400 Bad Request", "Unknown status")
        f = filters_from(form, prefix="f_")
        next_id = db.next_vacancy_id(conn, f, vacancy_id)
        reason = _first(form, "reason") if status == "skip" else None
        db.set_status(conn, vacancy_id, status, reason)
        location = f"/v/{next_id}?{filters_qs(f)}" if next_id else f"/?{filters_qs(f)}"
        return "303 See Other", [("Location", location)], b""


def _same_origin(environ) -> bool:
    origin = environ.get("HTTP_ORIGIN")
    return not origin or urlsplit(origin).netloc == environ.get("HTTP_HOST")


def _json(status: str, data: dict) -> Response:
    body = json.dumps(data, ensure_ascii=False).encode()
    return status, [("Content-Type", "application/json; charset=utf-8")], body


def _html(body: bytes) -> Response:
    return "200 OK", [("Content-Type", "text/html; charset=utf-8")], body


def _text(status: str, message: str) -> Response:
    return status, [("Content-Type", "text/plain; charset=utf-8")], message.encode()


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args) -> None:
        log.debug(format, *args)


class _ThreadingServer(ThreadingMixIn, WSGIServer):
    """A slow request (text generation takes seconds) must not block other pages."""

    daemon_threads = True


def serve(db_path: str | Path, port: int = 8000) -> None:
    app = App(db_path)
    with make_server(
        "127.0.0.1", port, app, server_class=_ThreadingServer, handler_class=_QuietHandler
    ) as httpd:
        log.info("Open http://127.0.0.1:%d (Ctrl+C to stop)", port)
        with contextlib.suppress(KeyboardInterrupt):
            httpd.serve_forever()
