#!/usr/bin/env python3
from __future__ import annotations

import json
from textwrap import indent

from sqlalchemy import text

from app.db import engine, get_sql_metrics_snapshot


QUERIES = [
    (
        "admin_unsorted_page",
        text(
            """
            EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
            SELECT ap.id, ap.created_at, ap.status, ap.title, ap.subtitle, ap.ru_title, ap.source_id,
                   ap.published_at, ap.ml_recommendation_confidence, s.final_score
            FROM article_previews ap
            LEFT JOIN scores s ON s.article_id = ap.id
            WHERE ap.status NOT IN ('archived', 'published', 'selected_hourly', 'rejected')
            ORDER BY ap.created_at DESC, ap.id DESC
            LIMIT 25 OFFSET 0
            """
        ),
    ),
    (
        "admin_all_page",
        text(
            """
            EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
            SELECT ap.id, ap.created_at, ap.status, ap.title, ap.subtitle, ap.ru_title, ap.source_id,
                   ap.published_at, ap.ml_recommendation_confidence, s.final_score
            FROM article_previews ap
            LEFT JOIN scores s ON s.article_id = ap.id
            WHERE ap.status NOT IN ('published', 'selected_hourly')
            ORDER BY ap.created_at DESC, ap.id DESC
            LIMIT 25 OFFSET 0
            """
        ),
    ),
    (
        "admin_search",
        text(
            """
            EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
            SELECT ap.id, ap.created_at, ap.title, ap.subtitle, ap.ru_title
            FROM article_previews ap
            WHERE ap.title ILIKE '%gpt%' OR ap.subtitle ILIKE '%gpt%' OR ap.ru_title ILIKE '%gpt%'
            ORDER BY ap.created_at DESC
            LIMIT 25 OFFSET 0
            """
        ),
    ),
    (
        "v1_articles_page",
        text(
            """
            EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
            SELECT ap.id, ap.status, ap.title, ap.ru_title, ap.source_id, ap.published_at, ap.canonical_url, s.final_score
            FROM article_previews ap
            LEFT JOIN scores s ON s.article_id = ap.id
            ORDER BY ap.created_at DESC, ap.id DESC
            LIMIT 20 OFFSET 0
            """
        ),
    ),
    (
        "count_unsorted",
        text(
            """
            EXPLAIN (ANALYZE, BUFFERS, VERBOSE)
            SELECT count(*)
            FROM article_previews ap
            WHERE ap.status NOT IN ('archived', 'published', 'selected_hourly', 'rejected')
            """
        ),
    ),
]


def run() -> int:
    print("== SQL runtime snapshot (top slow from in-memory metrics) ==")
    snap = get_sql_metrics_snapshot(top_n=5)
    print(json.dumps(snap, ensure_ascii=False, indent=2))

    print("\n== EXPLAIN ANALYZE top 5 candidate queries ==")
    with engine.connect() as conn:
        for name, stmt in QUERIES:
            print(f"\n--- {name} ---")
            rows = conn.execute(stmt).fetchall()
            plan = "\n".join(str(r[0]) for r in rows)
            print(indent(plan, "  "))

    return 0


if __name__ == "__main__":
    raise SystemExit(run())
