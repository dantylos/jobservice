"""Local review page: list vacancies, read one, mark it apply / later / skip.

Plain WSGI on the standard library, bound to 127.0.0.1 only. Descriptions are shown
as escaped text (never the stored third-party HTML), so no foreign scripts run here.
"""

import contextlib
import logging
import re
from html import escape
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from wsgiref.simple_server import WSGIRequestHandler, make_server

from jobservice import db
from jobservice.db import FILTERED, STATUSES, VIEWS, Filters

log = logging.getLogger(__name__)

STATUS_LABELS = {
    "new": "Новые",
    "apply": "Откликнуться",
    "later": "Позже",
    "skip": "Пропущенные",
    FILTERED: "Отсеянные",
}
_PERIODS = {"month": "мес", "hour": "час", "day": "день", "year": "год", "week": "нед"}
_MODES = {"remote": "удалённо", "hybrid": "гибрид", "onsite": "в офисе"}
SOURCE_LABELS = {"jobscz": "Jobs.cz", "startupjobs": "StartupJobs"}

Response = tuple[str, list[tuple[str, str]], bytes]


def _first(params: dict[str, list[str]], key: str) -> str | None:
    value = (params.get(key) or [""])[0].strip()
    return value or None


def filters_from(params: dict[str, list[str]], prefix: str = "") -> Filters:
    status = _first(params, prefix + "status") or "new"
    return Filters(
        status=None if status == "all" else status if status in VIEWS else "new",
        source=_first(params, prefix + "source"),
        city=_first(params, prefix + "city"),
        q=_first(params, prefix + "q"),
    )


def filters_qs(f: Filters, **override: str) -> str:
    values = {"status": f.status or "all", "source": f.source, "city": f.city, "q": f.q}
    values |= override
    return urlencode({k: v for k, v in values.items() if v})


def _salary(row) -> str | None:
    low, high = row["salary_min"], row["salary_max"]
    if not (low or high):
        return None
    amount = "–".join(f"{x:,.0f}".replace(",", " ") for x in (low, high) if x)
    period = _PERIODS.get(row["salary_period"] or "month", row["salary_period"])
    return f"{amount} {row['salary_currency'] or ''}/{period}"


def _view(row) -> str:
    return FILTERED if row["status"] == "new" and row["filter_reason"] else row["status"]


def _date(row) -> str:
    if row["published_at"]:
        return row["published_at"][:10]
    return row["published_raw"] or "—"


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
h1 { font-size: 22px; margin: 0 0 16px; } h1 a { color: inherit; }
.tabs { display:flex; flex-wrap:wrap; gap:6px; margin-bottom:12px; }
.tabs a { padding:6px 12px; border-radius:999px; background:var(--card); color:var(--fg); }
.tabs a.on { background:var(--fg); color:var(--bg); }
.tabs .n { color:var(--muted); margin-left:4px; } .tabs a.on .n { color:inherit; opacity:.7; }
form.filters { display:flex; flex-wrap:wrap; gap:8px; margin-bottom:16px; }
input, select, button { font:inherit; padding:6px 10px; border:1px solid var(--line);
  border-radius:8px; background:var(--bg); color:var(--fg); }
input[name=q] { flex: 1 1 220px; }
button { cursor:pointer; background:var(--card); }
.list { list-style:none; margin:0; padding:0; border-top:1px solid var(--line); }
.list li { padding:10px 2px; border-bottom:1px solid var(--line); }
.list .t { font-weight:600; }
.meta { color:var(--muted); font-size:13px; }
.badge { font-size:12px; padding:1px 8px; border-radius:999px; border:1px solid currentColor; }
.s-filtered { color:var(--muted); } .s-apply { color:var(--apply); }
.s-later { color:var(--later); } .s-skip { color:var(--skip); }
.empty { color:var(--muted); padding:24px 0; }
.desc { white-space:pre-wrap; background:var(--card); padding:16px; border-radius:10px;
  margin:16px 0; overflow-wrap:anywhere; }
.note { color:var(--muted); font-size:13px; }
.actions { display:flex; flex-wrap:wrap; gap:8px; align-items:center; position:sticky;
  bottom:0; background:var(--bg); padding:12px 0 calc(12px + env(safe-area-inset-bottom, 0px));
  border-top:1px solid var(--line); }
.actions .apply { border-color:var(--apply); color:var(--apply); }
.actions .later { border-color:var(--later); color:var(--later); }
.actions .skip { border-color:var(--skip); color:var(--skip); }
.actions input { flex: 1 1 200px; }
kbd { font-size:11px; color:var(--muted); }
"""

KEYS_JS = """
document.addEventListener('keydown', e => {
  if (e.target.matches('input, select, textarea') || e.metaKey || e.ctrlKey) return;
  const b = document.querySelector('button[data-key="' + e.key + '"]');
  if (b) { e.preventDefault(); b.click(); }
});
"""


def page(title: str, body: str) -> bytes:
    return (
        "<!doctype html><html lang=ru><head><meta charset=utf-8>"
        '<meta name=viewport content="width=device-width, initial-scale=1">'
        f"<title>{_e(title)}</title><style>{CSS}</style></head>"
        f"<body><main>{body}</main><script>{KEYS_JS}</script></body></html>"
    ).encode()


def render_list(conn, f: Filters) -> bytes:
    counts = db.status_counts(conn, f)
    tabs = []
    for key in (*VIEWS, "all"):
        n = sum(counts.values()) if key == "all" else counts[key]
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
    filters = (
        '<form class="filters" method="get" action="/">'
        f'<input type="hidden" name="status" value="{_e(f.status or "all")}">'
        f'<input name="q" value="{_e(f.q)}" placeholder="Поиск: должность, компания, текст">'
        f'<input name="city" value="{_e(f.city)}" placeholder="Город">'
        f'<select name="source"><option value="">Все сайты</option>{sources}</select>'
        "<button>Найти</button></form>"
    )

    rows = db.list_vacancies(conn, f)
    items = []
    for r in rows:
        badge = ""
        view = _view(r)
        if view != "new" and f.status is None:
            badge = f' <span class="badge s-{view}">{STATUS_LABELS[view]}</span>'
        why = r["status_reason"] or (r["filter_reason"] if view == FILTERED else None)
        reason = f" · {_e(why)}" if why else ""
        items.append(
            f'<li><a class="t" href="/v/{r["id"]}?{filters_qs(f)}">{_e(r["title"])}</a>{badge}'
            f'<div class="meta">{_e(r["company"]) or "—"} · {_e(r["city"]) or "—"} · '
            f"{_e(_date(r))} · {SOURCE_LABELS.get(r['source'], r['source'])}{reason}</div></li>"
        )
    listing = (
        f'<ul class="list">{"".join(items)}</ul>'
        if items
        else ('<p class="empty">Ничего не найдено.</p>')
    )
    return page("Вакансии", f'<h1><a href="/">Вакансии</a></h1><nav class="tabs">'
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
        (v["languages"] or "").replace(",", ", "),
    ]
    facts_html = " · ".join(_e(x) for x in facts if x)
    body = (
        f'<p><a href="/?{filters_qs(f)}">← К списку</a></p>'
        f"<h1>{_e(v['title'])}</h1>"
        f'<div class="meta">{_e(v["company"]) or "—"} · {_e(v["city"]) or "—"} · '
        f"{_e(_date(v))} · {SOURCE_LABELS.get(v['source'], v['source'])} · "
        f'<a href="{_e(v["url"])}" target="_blank" rel="noopener noreferrer">'
        "Открыть на сайте ↗</a></div>"
        + (f'<div class="meta">{facts_html}</div>' if facts_html else "")
        + f"{status}{note}"
        f'<div class="desc">{_e(v["description_text"]) or "Описание не сохранено."}</div>'
        f"{actions}"
    )
    return page(v["title"], body)


class App:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = db_path
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
            return _text("404 Not Found", "Not found")
        finally:
            conn.close()

    def _set_status(self, conn, environ, vacancy_id: int) -> Response:
        origin = environ.get("HTTP_ORIGIN")
        if origin and urlsplit(origin).netloc != environ.get("HTTP_HOST"):
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


def _html(body: bytes) -> Response:
    return "200 OK", [("Content-Type", "text/html; charset=utf-8")], body


def _text(status: str, message: str) -> Response:
    return status, [("Content-Type", "text/plain; charset=utf-8")], message.encode()


class _QuietHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args) -> None:
        log.debug(format, *args)


def serve(db_path: str | Path, port: int = 8000) -> None:
    app = App(db_path)
    with make_server("127.0.0.1", port, app, handler_class=_QuietHandler) as httpd:
        log.info("Open http://127.0.0.1:%d (Ctrl+C to stop)", port)
        with contextlib.suppress(KeyboardInterrupt):
            httpd.serve_forever()
