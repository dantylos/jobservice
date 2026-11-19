# jobservice

[![CI](https://github.com/dantylos/jobservice/actions/workflows/ci.yml/badge.svg)](https://github.com/dantylos/jobservice/actions/workflows/ci.yml)

English · [Русский](README.ru.md)

A personal job-search pipeline for the Czech market. It collects job ads from
[Jobs.cz](https://www.jobs.cz) and [StartupJobs.cz](https://www.startupjobs.cz) into SQLite,
drops ads that clearly do not fit, and serves a local page where I review each ad and decide:
apply, later or skip.

## What it does

1. **Collect.** Search results and full ad pages from each source go into one SQLite table.
   Re-running updates existing rows (key: `source` + `external_id`) instead of duplicating them,
   and skips descriptions already stored. Requests are rate-limited with retries and backoff.
2. **Filter.** Hard rules reject an ad only on clear evidence, so an ad with missing data passes.
   Rejected ads are kept with the reason, never deleted.
   - **Location:** a chosen city or remote work.
   - **Salary:** top of the range below a monthly minimum in EUR (CZK converted, hourly × 160).
   - **Seniority:** senior / lead / head / principal titles.
   - **Language:** Czech required above B1, or another language besides English, Russian and
     Ukrainian.
   - **Duplicates:** the same title and company on several sites. The copy with a decision wins,
     otherwise the one with the longest description.
3. **Review.** `jobservice serve` opens a local page (127.0.0.1 only) with tabs for new, apply,
   later and skipped ads, text search, and keyboard shortcuts: **A** apply, **L** later,
   **S** skip with a reason.

## Sources

- **StartupJobs:** the JSON API the site itself uses.
- **Jobs.cz:** search-result HTML, then each ad page (JSON-LD where available). Ads on
  employer-branded templates (`*.jobs.cz/detail-pozice`) are rendered by JavaScript, so only the
  short `og:description` summary is stored for them.

## Usage

```sh
uv sync
cp .env.example .env                 # keywords, city, filters, database path

uv run jobservice collect            # settings from .env
uv run jobservice collect -k python -k tester --city Praha --max-pages 2
uv run jobservice filter             # re-apply filters after changing settings
uv run jobservice serve              # http://127.0.0.1:8000
```

## Testing

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

Parsers are tested against saved page fixtures (`tests/fixtures`), so a change in a site's
markup shows up as a failing test rather than as silently wrong data. Filters, the database
layer and the review page have their own tests. CI runs lint, formatting and the full suite on
every push.

## Roadmap

- [x] Collection from Jobs.cz and StartupJobs
- [x] Hard filters and duplicate merging
- [x] Local review page
- [ ] More sources: Prace.cz, e-mail job alerts
- [ ] LLM scoring of each ad against my profile, with "why it fits / what is missing"
- [ ] Tailored CV (DOCX + PDF) and cover letter for approved ads
- [ ] Sending applications by e-mail, only after explicit confirmation of each one

## Stack

Python 3.12, uv, SQLite, httpx, selectolax, standard-library WSGI for the review page,
pytest and Ruff. Built with an AI coding agent (Claude Code): I set the requirements and
review each step, and the tests guard the behaviour.
