import argparse
import logging
import os
import sys

from dotenv import load_dotenv

from jobservice import db
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
    return p


def collect(args: argparse.Namespace) -> int:
    flt = SearchFilter(
        keywords=args.keywords or _env_list("JOBS_KEYWORDS"),
        city=args.city or os.getenv("JOBS_CITY") or None,
        max_pages=args.max_pages or int(os.getenv("JOBS_MAX_PAGES", "3")),
    )
    if not flt.keywords:
        log.error("no keywords: pass --keyword or set JOBS_KEYWORDS in .env")
        return 2

    conn = db.connect(args.db or os.getenv("JOBS_DB_PATH", "data/jobs.db"))
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
    conn.close()
    return 1 if failed else 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not args.verbose:
        logging.getLogger("httpx").setLevel(logging.WARNING)
    return collect(args)


if __name__ == "__main__":
    sys.exit(main())
