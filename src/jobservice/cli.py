import argparse
import logging
import os
import sys

from dotenv import load_dotenv

from jobservice import db
from jobservice.filters import FilterConfig, apply_filters
from jobservice.http import HttpClient
from jobservice.models import SearchFilter
from jobservice.sources import SOURCES

log = logging.getLogger("jobservice")


def _env_list(name: str) -> list[str]:
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jobservice", description="Job vacancy collector")
    sub = p.add_subparsers(dest="command", required=True)

    c = sub.add_parser("collect", help="collect vacancies into SQLite")
    c.add_argument(
        "-k",
        "--keyword",
        action="append",
        dest="keywords",
        help="search keyword, repeatable (env JOBS_KEYWORDS, comma-separated)",
    )
    c.add_argument("--city", help="city filter, e.g. Praha (env JOBS_CITY)")
    c.add_argument(
        "--source",
        action="append",
        dest="sources",
        choices=sorted(SOURCES),
        help="only these sources (default: all)",
    )
    c.add_argument(
        "--max-pages", type=int, help="result pages per keyword (env JOBS_MAX_PAGES, default 3)"
    )
    c.add_argument("--db", help="SQLite path (env JOBS_DB_PATH, default data/jobs.db)")
    c.add_argument(
        "--delay", type=float, help="seconds between requests (env JOBS_REQUEST_DELAY, default 1)"
    )
    c.add_argument("--refresh", action="store_true", help="re-download descriptions already stored")
    c.add_argument("-v", "--verbose", action="store_true")

    f = sub.add_parser("filter", help="re-apply hard filters to the whole database")
    f.add_argument("--db", help="SQLite path (env JOBS_DB_PATH, default data/jobs.db)")
    f.add_argument("-v", "--verbose", action="store_true")

    sc = sub.add_parser("score", help="score new vacancies against the profile with Claude Code")
    sc.add_argument("--limit", type=int, help="score at most N vacancies (e.g. a trial run)")
    sc.add_argument("--id", type=int, action="append", dest="ids", help="only this vacancy id")
    sc.add_argument("--rescore", action="store_true", help="redo scores that are up to date")
    sc.add_argument(
        "--profile", help="profile file (env JOBS_PROFILE_PATH, default data/profile.md)"
    )
    sc.add_argument("--model", help="Claude model (env JOBS_SCORE_MODEL, default: Claude Code's)")
    sc.add_argument("--db", help="SQLite path (env JOBS_DB_PATH, default data/jobs.db)")
    sc.add_argument("-v", "--verbose", action="store_true")

    s = sub.add_parser("serve", help="local review page on http://127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--db", help="SQLite path (env JOBS_DB_PATH, default data/jobs.db)")
    s.add_argument("-v", "--verbose", action="store_true")
    return p


def _db_path(args: argparse.Namespace) -> str:
    return args.db or os.getenv("JOBS_DB_PATH", "data/jobs.db")


def serve(args: argparse.Namespace) -> int:
    from jobservice.web import serve as run_server

    run_server(_db_path(args), port=args.port)
    return 0


def collect(args: argparse.Namespace) -> int:
    flt = SearchFilter(
        keywords=args.keywords or _env_list("JOBS_KEYWORDS"),
        city=args.city or os.getenv("JOBS_CITY") or None,
        max_pages=args.max_pages or int(os.getenv("JOBS_MAX_PAGES", "3")),
        include_remote=FilterConfig.from_env().allow_remote,
    )
    if not flt.keywords:
        log.error("no keywords: pass --keyword or set JOBS_KEYWORDS in .env")
        return 2

    conn = db.connect(_db_path(args))
    delay = args.delay if args.delay is not None else float(os.getenv("JOBS_REQUEST_DELAY", "1"))
    failed = False
    with HttpClient(delay=delay) as http:
        for name in args.sources or sorted(SOURCES):
            source = SOURCES[name](http)
            skip = set() if args.refresh else db.ids_with_description(conn, name)
            new = total = 0
            try:
                for vacancy in source.collect(flt, skip):
                    total += 1
                    new += db.upsert(conn, vacancy)
            except Exception:
                log.exception("%s: aborted after %d vacancies", name, total)
                failed = True
            log.info("%s: %d vacancies, %d new", name, total, new)
    _run_filters(conn, city=flt.city)
    conn.close()
    return 1 if failed else 0


def _run_filters(conn, city: str | None = None) -> None:
    cfg = FilterConfig.from_env()
    if city:
        cfg.city = city
    stats = apply_filters(conn, cfg)
    log.info("filters: %(passed)d of %(total)d passed, %(filtered)d filtered out", stats)


def filter_db(args: argparse.Namespace) -> int:
    conn = db.connect(_db_path(args))
    _run_filters(conn)
    conn.close()
    return 0


def score(args: argparse.Namespace) -> int:
    from jobservice import scoring

    path = args.profile or os.getenv("JOBS_PROFILE_PATH", "data/profile.md")
    try:
        profile = scoring.load_profile(path)
    except (OSError, ValueError) as e:
        log.error("profile: %s", e)
        return 2
    runner = scoring.runner_from_env()
    if args.model:
        runner.model = args.model
    conn = db.connect(_db_path(args))
    stats = scoring.score_pending(
        conn,
        runner,
        profile,
        limit=args.limit,
        rescore=args.rescore,
        ids=set(args.ids) if args.ids else None,
    )
    conn.close()
    log.info("scoring: %(scored)d scored, %(failed)d failed of %(pending)d pending", stats)
    return 1 if stats["failed"] else 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
    commands = {"collect": collect, "filter": filter_db, "score": score, "serve": serve}
    return commands[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
