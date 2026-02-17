from __future__ import annotations

import argparse

from app.db import init_db
from app.services.bootstrap import seed_sources
from app.services.ingestion import run_ingestion


def main() -> None:
    parser = argparse.ArgumentParser(description="Read RSS sources and store articles")
    parser.add_argument("--days-back", type=int, default=30, help="how far back to accept feed entries")
    args = parser.parse_args()

    init_db()
    seed_sources()
    result = run_ingestion(days_back=args.days_back)
    print(result)


if __name__ == "__main__":
    main()
