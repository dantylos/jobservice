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
   Rejected ads are hidden from the review page but kept with the reason, never deleted
   (`jobservice filter --stats` prints a summary).
   Vacancies belong to a market, Czechia or Ukraine (remote), each with its own tab and
   salary minimum (`JOBS_MIN_SALARY_EUR`, `JOBS_MIN_SALARY_EUR_UA`).
   - **Location:** a chosen city or remote work; remote work only for other countries
     ("Poland only") is rejected on the Czech tab.
   - **Salary:** top of the range below a monthly minimum in EUR (CZK converted, hourly × 160).
   - **Seniority:** senior / lead / head / principal titles.
   - **Language:** Czech required above B1, or another language besides English, Russian and
     Ukrainian.
   - **Duplicates:** the same title and company on several sites. The copy with a decision wins,
     otherwise the one with the longest description.
3. **Score.** `jobservice score` sends each new ad that passed the filters, together with my
   profile (three base CVs for three tracks: AI, QA, HR), to Claude through Claude Code in
   headless mode (`claude -p`, no tools, JSON-schema output). The model returns a 0-100 score
   per track, the entry barrier, and up to three points on why the ad fits and what is missing.
   Scores are stored with a hash of the prompt version, profile and ad text, so only new or
   changed ads are scored again. Ukrainian remote ads get extra, separately versioned rules:
   a need to be in Ukraine or to work as a Ukrainian sole proprietor (FOP) is flagged, not
   penalised.
4. **Review.** `jobservice serve` opens a local page (127.0.0.1 only) with tabs for new, apply,
   later and skipped ads, text search, sorting by overall or per-track score or by entry barrier,
   and keyboard shortcuts: **A** apply, **L** later, **S** skip with a reason. Opening an ad
   also drafts a ready-to-paste answer to StartupJobs' "Jak mohu vaši firmu posunout vpřed?"
   (a short cover letter in the ad's language, facts only from my profile), with copy and
   regenerate buttons. I paste and send it myself.

## Sources

- **StartupJobs:** the JSON API the site itself uses.
- **DOU.ua** (Ukrainian remote market): the site's official RSS feeds of remote vacancies,
  per category (`JOBS_DOU_CATEGORIES`) and experience bucket (`JOBS_DOU_EXPERIENCE`).
- **Djinni** (Ukrainian remote market): its official RSS feeds (`JOBS_DJINNI_*`); company,
  salary and applicant location come from the schema.org JobPosting data on the vacancy page,
  fetched once per new vacancy.
- **NoFluffJobs:** its RSS feed of all offers; only offers whose link names Prague or remote
  work (and no senior title) are opened for the schema.org JobPosting data.
- **EU Remote Jobs:** the newest job sitemap; each new listing page is opened once for its
  JobPosting data. The site's terms forbid automated collection; I collect anyway, for my own
  job search only, at my own risk.
- **Next Level Jobs:** the job sitemap; each new page is opened once for its JobPosting data and
  experience level. At most `JOBS_NEXTLEVELJOBS_MAX_PAGES` pages per run.
- Vacancies from these three sites go to the Czech tab, or to the Ukrainian one when the job is
  in Ukraine or only for people living there.
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
uv run jobservice score --limit 10   # score new ads with Claude (needs data/profile.md)
uv run jobservice report             # collect + score + desktop notification
uv run jobservice serve              # http://127.0.0.1:8000
```

## Testing

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
```

The model call is replaced by a fake in tests, so the suite runs offline. Parsers are tested
against saved page fixtures (`tests/fixtures`), so a change in a site's
markup shows up as a failing test rather than as silently wrong data. Filters, the database
layer and the review page have their own tests. CI runs lint, formatting and the full suite on
every push.

## Roadmap

- [x] Collection from Jobs.cz and StartupJobs
- [x] Ukrainian remote vacancies from DOU.ua and Djinni (RSS)
- [x] Hard filters and duplicate merging
- [x] Local review page
- [x] NoFluffJobs (EU Remote Jobs and Next Level Jobs are off by default: slow, few junior fits;
  `jobservice collect --source euremotejobs` still runs them)
- [ ] More sources: Prace.cz, e-mail job alerts
- [x] LLM scoring of each ad against my profile, with "why it fits / what is missing"
- [ ] Tailored CV (DOCX + PDF) and cover letter for approved ads
- [ ] Sending applications by e-mail, only after explicit confirmation of each one

## Stack

Python 3.12, uv, SQLite, httpx, selectolax, standard-library WSGI for the review page,
pytest and Ruff. Built with an AI coding agent (Claude Code): I set the requirements and
review each step, and the tests guard the behaviour.
