#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select

from app.db import session_scope
from app.models import Article, Score, TrainingEvent


@dataclass
class Row:
    article_id: int
    label: int
    decision: str
    created_at: datetime
    reason_text: str
    title: str
    source_id: int
    source_name: str
    canonical_url: str
    content_mode: str
    score_10: float | None
    status: str


def _pick_latest(rows: list[TrainingEvent]) -> dict[int, TrainingEvent]:
    out: dict[int, TrainingEvent] = {}
    for row in rows:
        cur = out.get(int(row.article_id))
        if cur is None or (row.created_at or datetime.min) > (cur.created_at or datetime.min):
            out[int(row.article_id)] = row
    return out


def build(days_back: int, min_reason_len: int, max_rows: int) -> list[Row]:
    since = datetime.utcnow() - timedelta(days=max(1, int(days_back)))
    with session_scope() as session:
        raw = session.scalars(
            select(TrainingEvent)
            .where(
                TrainingEvent.created_at >= since,
                TrainingEvent.decision.in_(["publish", "top_pick", "hide", "delete", "defer", "skip"]),
            )
            .order_by(TrainingEvent.created_at.desc())
        ).all()

        by_article = _pick_latest(raw)
        positive: list[Row] = []
        negative: list[Row] = []

        for ev in by_article.values():
            reason = str(ev.reason_text or "").strip()
            if len(reason) < min_reason_len:
                continue
            article = session.get(Article, int(ev.article_id))
            if not article:
                continue
            score = session.get(Score, int(ev.article_id))
            item = Row(
                article_id=int(article.id),
                label=int(1 if int(ev.label or 0) == 1 else 0),
                decision=str(ev.decision or ""),
                created_at=ev.created_at or datetime.min,
                reason_text=reason,
                title=str(article.ru_title or article.title or ""),
                source_id=int(article.source_id or 0),
                source_name=str(getattr(article.source, "name", "") or ""),
                canonical_url=str(article.canonical_url or ""),
                content_mode=str(article.content_mode or ""),
                score_10=round(float(score.final_score or 0.0) * 10.0, 1) if score and score.final_score is not None else None,
                status=str(getattr(article.status, "value", article.status) or ""),
            )
            if item.label == 1:
                positive.append(item)
            else:
                negative.append(item)

    target_per_class = max(1, min(len(positive), len(negative), max_rows // 2))
    positive = positive[:target_per_class]
    negative = negative[:target_per_class]
    merged = positive + negative
    merged.sort(key=lambda x: x.created_at, reverse=True)
    return merged


def save_csv(rows: list[Row], out_path: Path) -> None:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.writer(fp)
        writer.writerow(
            [
                "article_id",
                "label",
                "decision",
                "created_at_utc",
                "title",
                "source_id",
                "source_name",
                "canonical_url",
                "content_mode",
                "score_10",
                "status",
                "reason_text",
            ]
        )
        for row in rows:
            writer.writerow(
                [
                    row.article_id,
                    row.label,
                    row.decision,
                    row.created_at.isoformat(),
                    row.title,
                    row.source_id,
                    row.source_name,
                    row.canonical_url,
                    row.content_mode,
                    row.score_10 if row.score_10 is not None else "",
                    row.status,
                    row.reason_text,
                ]
            )


def main() -> None:
    parser = argparse.ArgumentParser(description="Build balanced clean ML dataset from training_events.")
    parser.add_argument("--days-back", type=int, default=90)
    parser.add_argument("--min-reason-len", type=int, default=20)
    parser.add_argument("--max-rows", type=int, default=300)
    parser.add_argument("--out", type=str, default="artifacts/ml/clean_dataset.csv")
    args = parser.parse_args()

    rows = build(days_back=args.days_back, min_reason_len=args.min_reason_len, max_rows=args.max_rows)
    out = Path(args.out)
    save_csv(rows, out)
    print(f"ok rows={len(rows)} out={out}")


if __name__ == "__main__":
    main()
