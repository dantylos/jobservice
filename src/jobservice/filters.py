"""Hard filters and cross-source de-duplication.

Every rule rejects only on clear evidence; when data is missing or ambiguous the
vacancy passes (the model scoring step can judge it later). The verdict is stored in
`vacancies.filter_reason` (None = passed) and recomputed from scratch on every run, so
changing the settings and running `jobservice filter` re-sorts the whole database.
"""

import os
import re
import sqlite3
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field

from jobservice.text import slugify

_HOURS_PER_MONTH = 160
_PERIOD_TO_MONTH = {"month": 1, "hour": _HOURS_PER_MONTH, "day": 20, "week": 4.33, "year": 1 / 12}


@dataclass(slots=True)
class FilterConfig:
    city: str | None = "Praha"
    allow_remote: bool = True
    min_salary_eur: float | None = 1500
    min_salary_eur_ua: float | None = 1000  # Ukrainian remote market
    eur_czk: float = 24.4
    eur_usd: float = 1.10  # ECB reference rate on 2026-11-26: 1.0975
    # Languages the candidate can work in; anything else explicitly required rejects.
    known_languages: set[str] = field(
        default_factory=lambda: {"english", "russian", "ukrainian", "czech"}
    )
    # Model score (see scoring.py): reject below this best-track score / with a high barrier.
    min_score: int | None = 50
    reject_high_barrier: bool = True

    @classmethod
    def from_env(cls, env: Mapping[str, str] = os.environ) -> "FilterConfig":
        cfg = cls()
        if "JOBS_CITY" in env:
            cfg.city = env["JOBS_CITY"].strip() or None
        if "JOBS_INCLUDE_REMOTE" in env:
            cfg.allow_remote = env["JOBS_INCLUDE_REMOTE"].strip().lower() in ("1", "true", "yes")
        if env.get("JOBS_MIN_SALARY_EUR", "").strip():
            cfg.min_salary_eur = float(env["JOBS_MIN_SALARY_EUR"])
        if "JOBS_MIN_SALARY_EUR_UA" in env:
            value = env["JOBS_MIN_SALARY_EUR_UA"].strip()
            cfg.min_salary_eur_ua = float(value) if value else None
        if env.get("JOBS_EUR_CZK", "").strip():
            cfg.eur_czk = float(env["JOBS_EUR_CZK"])
        if env.get("JOBS_EUR_USD", "").strip():
            cfg.eur_usd = float(env["JOBS_EUR_USD"])
        if "JOBS_MIN_SCORE" in env:
            value = env["JOBS_MIN_SCORE"].strip()
            cfg.min_score = int(value) if value else None
        if "JOBS_REJECT_HIGH_BARRIER" in env:
            value = env["JOBS_REJECT_HIGH_BARRIER"].strip().lower()
            cfg.reject_high_barrier = value in ("1", "true", "yes")
        return cfg


# ---- location -------------------------------------------------------------------------


def check_location(row: Mapping, cfg: FilterConfig) -> str | None:
    if not cfg.city or not row["city"]:
        return None
    modes = set((row["work_mode"] or "").split(","))
    if cfg.allow_remote and "remote" in modes:
        return None
    city_norm = slugify(row["city"])
    wanted = {slugify(cfg.city)}
    if wanted == {"praha"}:
        wanted.add("prague")
    if any(w in city_norm for w in wanted):
        return None
    return f"Место: {row['city']}"


# ---- salary ---------------------------------------------------------------------------


def monthly_eur(
    amount: float,
    currency: str | None,
    period: str | None,
    eur_czk: float,
    eur_usd: float | None = None,
):
    factor = _PERIOD_TO_MONTH.get(period or "month")
    if factor is None:
        return None
    monthly = amount * factor
    match (currency or "CZK").upper():
        case "EUR":
            return monthly
        case "CZK":
            return monthly / eur_czk
        case "USD" if eur_usd:
            return monthly / eur_usd
        case _:
            return None  # unknown currency: don't judge


def check_salary(row: Mapping, cfg: FilterConfig) -> str | None:
    ua = dict(row).get("market") == "ua"
    minimum = cfg.min_salary_eur_ua if ua else cfg.min_salary_eur
    if not minimum:
        return None
    top = row["salary_max"] or row["salary_min"]
    if not top:
        return None
    eur = monthly_eur(top, row["salary_currency"], row["salary_period"], cfg.eur_czk, cfg.eur_usd)
    if eur is not None and eur < minimum:
        return f"Зарплата до {eur:,.0f} €/мес".replace(",", " ")
    return None


# ---- seniority ------------------------------------------------------------------------

_SENIOR_TITLE = re.compile(
    r"\b(senior|sr|lead|head|principal|staff|director|chief|vp|architect|vedouci|reditel\w*)\b"
)
_NOT_SENIOR = re.compile(r"\blead(s)?[ -](generation|gen)\b")


def check_seniority(row: Mapping, cfg: FilterConfig) -> str | None:
    levels = {x for x in (row["seniority"] or "").split(",") if x}
    if levels:
        if levels & {"junior", "medior"}:
            return None
        return "Уровень: " + ", ".join(sorted(levels))
    title = slugify(row["title"] or "").replace("-", " ")
    title = _NOT_SENIOR.sub("", title)
    m = _SENIOR_TITLE.search(title)
    return f"Уровень: {m.group(1)} (в названии)" if m else None


# ---- languages ------------------------------------------------------------------------

# StartupJobs levels: beginner, intermediate, professional, native.
_STRONG_LEVELS = {
    "professional",
    "advanced",
    "proficient",
    "fluent",
    "native",
    "expert",
    "c1",
    "c2",
}
_LANG_ALIASES = {
    "czech": "czech", "cestina": "czech", "slovak": "czech", "slovencina": "czech",
    "english": "english", "anglictina": "english",
    "russian": "russian", "rustina": "russian",
    "ukrainian": "ukrainian", "ukrajinstina": "ukrainian",
}  # fmt: skip

# Czech required above B1. Text is pre-normalized: lowercase ASCII, single spaces.
_CZECH = r"(cestin\w*|cesk\w* jazyk\w*|cesky\b)"
_CZECH_STRONG = [
    re.compile(rf"\b(plynul\w*|vyborn\w*|perfektn\w*|rodil\w*)( (a|i))?( \w+){{0,2}} {_CZECH}"),
    re.compile(r"\b(cestin\w*|cesk\w* jazyk\w*)\b[^.;,/]{0,40}\b(c1|c2|rodil\w*|plynul\w*)"),
    re.compile(
        r"\b(fluent|native|excellent|proficient)\b( (in|level|command of))?( \w+){0,2} czech\b"
    ),
    re.compile(r"\bczech\b( language)?[^.;,/]{0,25}\b(fluent|native|c1|c2|mother tongue)\b"),
]
_CZECH_OPTIONAL = re.compile(
    r"\b(not (required|necessary|needed|a must)|is a (plus|bonus|advantage)|nice to have|"
    r"neni (nutn|podmink)\w*|vyhodou)\b"
)
_OTHER_LANG = (
    r"(german|deutsch|nemcin\w*|nemeck\w*|french|francouzst\w*|polish|polstin\w*|hungarian|"
    r"madarstin\w*|italian|italstin\w*|spanish|spanelstin\w*|dutch|nizozemstin\w*|"
    r"portuguese|portugalstin\w*|romanian|rumunstin\w*|bulgarian|bulharstin\w*|"
    r"croatian|chorvatstin\w*|serbian|srbstin\w*|slovenian|slovinstin\w*|greek|rectin\w*|"
    r"turkish|turectin\w*|swedish|svedstin\w*|danish|danstin\w*|norwegian|norstin\w*|"
    r"finnish|finstin\w*|japanese|japonstin\w*|chinese|cinstin\w*|korean|korejstin\w*|"
    r"arabic|arabstin\w*|hebrew|hebrejstin\w*)"
)
_TITLE_OTHER_LANG = re.compile(rf"\b{_OTHER_LANG}\b")
# In a title, "Czech speaking" / "s češtinou" means fluent Czech is required.
_TITLE_CZECH = re.compile(
    r"\b(czech|slovak|cesky|slovensky)[ -]?(speak\w*|native)\b|\bnative czech\b"
    r"|\bs (cestinou|slovenstinou)\b"
)
_OTHER_REQUIRED = re.compile(
    rf"\b(fluent|native|plynul\w*|aktivn\w*|vyborn\w*)( \w+){{0,2}} {_OTHER_LANG}"
    rf"|\b{_OTHER_LANG}\b[^.;]{{0,30}}\b(required|must|fluent|c1|c2|b2|podminkou|nutn\w*|plynul\w*)"
)


def _normalize(text: str) -> str:
    """Lowercase ASCII without diacritics; punctuation is kept (regexes stop at it)."""
    ascii_text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", ascii_text.lower())


def check_languages(row: Mapping, cfg: FilterConfig) -> str | None:
    # A language in the title is a requirement: "HR Specialist with Italian", "s němčinou".
    title = _normalize(row["title"] or "")
    if _TITLE_CZECH.search(title):
        return "Чешский выше B1 (в названии)"
    if m := _TITLE_OTHER_LANG.search(title):
        return f"Нужен язык: {m.group(1)} (в названии)"

    for item in filter(None, (row["languages"] or "").split(",")):
        lang, _, level = item.partition(":")
        lang = _LANG_ALIASES.get(slugify(lang), slugify(lang))
        if level.lower() not in _STRONG_LEVELS:
            continue
        if lang == "czech":
            return "Чешский выше B1"
        if lang not in cfg.known_languages:
            return f"Нужен язык: {lang}"

    text = _normalize(row["description_text"] or "")
    for pattern in _CZECH_STRONG:
        for m in pattern.finditer(text):
            tail = text[m.end() : m.end() + 40]
            if not _CZECH_OPTIONAL.search(tail):
                return "Чешский выше B1"
    if m := _OTHER_REQUIRED.search(text):
        lang = next(g for g in m.groups() if g and re.fullmatch(_OTHER_LANG, g))
        return f"Нужен язык: {lang}"
    return None


# ---- model score -----------------------------------------------------------------------

# Reasons from the score check start with this prefix, so scoring can still re-score them.
SCORE_REASON = "Оценка: "


def check_score(row: Mapping, cfg: FilterConfig) -> str | None:
    """Unscored vacancies pass; the model judges them on the next `jobservice score`."""
    score = row["score"] if "score" in row.keys() else None  # noqa: SIM118 (sqlite3.Row)
    if score is None:
        return None
    if cfg.reject_high_barrier and row["entry_barrier"] == "high":
        return SCORE_REASON + "высокий порог входа"
    if cfg.min_score is not None and score < cfg.min_score:
        return SCORE_REASON + f"балл {score} < {cfg.min_score}"
    return None


# ---- all together ---------------------------------------------------------------------

CHECKS = (check_location, check_seniority, check_salary, check_languages, check_score)


def evaluate(row: Mapping, cfg: FilterConfig) -> str | None:
    for check in CHECKS:
        if reason := check(row, cfg):
            return reason
    return None


_GENDER = re.compile(r"\((?:[mfzwd](?: ?/ ?[mfzwd])*|all genders)\)")
_COMPANY_SUFFIX = re.compile(r"\b(s r o|a s|spol|k s|v o s|se|z s|gmbh|ltd|inc|llc)\b")


def dedup_key(row: Mapping) -> str:
    title = _GENDER.sub("", _normalize(row["title"] or ""))
    company = re.sub(r"[^a-z0-9]+", " ", _normalize(row["company"] or ""))
    company = _COMPANY_SUFFIX.sub("", company)
    return re.sub(r"[^a-z0-9]+", "", title) + "|" + re.sub(r"[^a-z0-9]+", "", company)


def apply_filters(conn: sqlite3.Connection, cfg: FilterConfig) -> dict[str, int]:
    """Recompute filter_reason for every vacancy. Returns counts for logging."""
    rows = conn.execute(
        "SELECT vacancies.*, scores.score, scores.entry_barrier FROM vacancies "
        "LEFT JOIN scores ON scores.vacancy_id = vacancies.id"
    ).fetchall()
    reasons = {r["id"]: evaluate(r, cfg) for r in rows}

    # Duplicates: keep one per (title, company). Prefer one you already decided on,
    # then the longest description, then the oldest record.
    groups: dict[str, list] = {}
    for r in rows:
        groups.setdefault(dedup_key(r), []).append(r)
    for group in groups.values():
        if len(group) < 2:
            continue
        group.sort(key=lambda r: (r["status"] == "new", -len(r["description_text"] or ""), r["id"]))
        keep = group[0]["id"]
        for r in group[1:]:
            reasons[r["id"]] = reasons[r["id"]] or f"Дубль #{keep}"

    conn.executemany(
        "UPDATE vacancies SET filter_reason = ? WHERE id = ?",
        [(reason, vid) for vid, reason in reasons.items()],
    )
    conn.commit()
    passed = sum(1 for v in reasons.values() if v is None)
    return {"total": len(rows), "passed": passed, "filtered": len(rows) - passed}
