from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.core.config import settings
from app.db import session_scope
from app.models import Article, ArticleStatus, DecisionMode, Score
from app.services.content_generation import generate_image_card, generate_ru_summary
from app.services.embedding_dedup import process_embeddings_and_dedup
from app.services.ingestion import enrich_summary_only_articles, run_ingestion_fast
from app.services.llm import get_client, track_usage_from_response
from app.services.preference import get_active_profile, save_selection_decision
from app.services.scoring import run_scoring
from app.services.telegram_context import telegram_timezone_name
from app.services.runtime_settings import get_runtime_str


def _audience_adjusted_score(article: Article, score: Score) -> float:
    raw = float(score.final_score or 0.0)
    features = score.features if isinstance(score.features, dict) else {}
    domain = str(features.get("domain") or "").strip().lower()
    business_it = float(features.get("business_it") or 0.0)  # stored as 0..1
    geek_penalty = float(features.get("geek_penalty") or 1.0)

    mult = geek_penalty
    if domain == "research":
        mult *= 0.78
    if business_it < 0.65:
        mult *= 0.90
    if article.content_mode == "summary_only":
        mult *= 0.85
    return raw * mult


def _get_tz_name() -> str:
    return (telegram_timezone_name() or get_runtime_str("timezone_name") or "Europe/Moscow").strip()


def _hour_bucket_utc(dt_utc: datetime, tz_name: str) -> datetime:
    """
    Compute "hour bucket start" in UTC (naive) aligned to the user's timezone hour.
    dt_utc must be naive UTC datetime.
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Moscow")
    local = dt_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    local_bucket = local.replace(minute=0, second=0, microsecond=0)
    bucket_utc = local_bucket.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return bucket_utc


def _hourly_candidates(limit: int = 50) -> list[tuple[Article, Score]]:
    now = datetime.utcnow()
    hour_ago = now - timedelta(hours=1)
    day_ago = now - timedelta(hours=24)
    three_days_ago = now - timedelta(days=3)

    with session_scope() as session:
        selected_clusters = set(
            row[0]
            for row in session.execute(
                select(Article.cluster_key).where(
                    Article.status.in_([ArticleStatus.SELECTED_HOURLY, ArticleStatus.PUBLISHED]),
                    Article.updated_at >= day_ago,
                    Article.cluster_key.is_not(None),
                )
            ).all()
        )

        base = (
            select(Article, Score)
            .join(Score, Score.article_id == Article.id)
            .where(
                Article.status.in_([ArticleStatus.SCORED, ArticleStatus.REVIEW, ArticleStatus.READY]),
                Article.status != ArticleStatus.DOUBLE,
            )
            .options(joinedload(Article.source))
            .order_by(Score.final_score.desc())
            .limit(limit)
        )
        rows = session.execute(
            base.where((Article.created_at >= hour_ago) | (Article.published_at >= hour_ago))
        ).all()

        # Fallback: if last-hour window is empty, use last 24h so Selected Hour
        # still gets a reasonable candidate in low-news periods.
        if not rows:
            rows = session.execute(
                base.where((Article.created_at >= day_ago) | (Article.published_at >= day_ago))
            ).all()
        # Fallback 2: low-news mode, search last 3 days.
        if not rows:
            rows = session.execute(
                base.where((Article.created_at >= three_days_ago) | (Article.published_at >= three_days_ago))
            ).all()
        # Fallback 3: if still empty, use best available scored/review/ready items.
        if not rows:
            rows = session.execute(base).all()

    filtered = [(a, s) for a, s in rows if a.cluster_key not in selected_clusters]
    filtered.sort(key=lambda x: _audience_adjusted_score(x[0], x[1]), reverse=True)
    return filtered


def _choose_with_profile(candidates: list[tuple[Article, Score]], top_n: int = 3) -> tuple[int, dict]:
    ranked = candidates[:top_n]
    if not ranked:
        return 0, {"mode": "none", "reason": "no_candidates"}

    profile = get_active_profile()
    if not profile or not settings.openrouter_api_key:
        return ranked[0][0].id, {
            "mode": "fallback_by_score",
            "top_drivers": ranked[0][1].features.get("top_drivers", []) if ranked[0][1].features else [],
        }

    payload = []
    for a, s in ranked:
        payload.append(
            {
                "article_id": a.id,
                "title": a.title,
                "source": a.source.name if a.source else "Unknown",
                "score": s.final_score,
                "features": s.features,
            }
        )

    prompt = f"""
Choose 1 article for this hour.
Preference profile:
{profile}

Candidates:
{json.dumps(payload, ensure_ascii=False)}

Return JSON only:
{{"article_id": int, "confidence": 0-1, "reason": "short"}}
"""
    try:
        client = get_client()
        resp = client.chat.completions.create(
            model=settings.llm_text_model,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        track_usage_from_response(resp, operation="pipeline.choose_with_profile", model=settings.llm_text_model, kind="chat")
        data = json.loads(resp.choices[0].message.content or "{}")
        picked = int(data.get("article_id") or ranked[0][0].id)
        confidence = float(data.get("confidence", 0.5))
        if picked not in {x[0].id for x in ranked}:
            picked = ranked[0][0].id
        return picked, {"mode": "profile", "confidence": confidence, "reason": data.get("reason", "")}
    except Exception:
        return ranked[0][0].id, {
            "mode": "fallback_by_score_on_error",
            "top_drivers": ranked[0][1].features.get("top_drivers", []) if ranked[0][1].features else [],
        }


def pick_hourly_top() -> int | None:
    candidates = _hourly_candidates(limit=50)
    if not candidates:
        return None

    top3 = candidates[:3]
    selected_id, explain = _choose_with_profile(top3, top_n=3)

    with session_scope() as session:
        article = session.get(Article, selected_id)
        if not article:
            return None
        article.status = ArticleStatus.SELECTED_HOURLY
        # Save the hour-bucket so we can backfill 24h later and avoid duplicates per hour.
        tz_name = _get_tz_name()
        article.selected_hour_bucket_utc = _hour_bucket_utc(datetime.utcnow(), tz_name)
        article.updated_at = datetime.utcnow()

    rejected = [a.id for a, _ in top3 if a.id != selected_id]
    save_selection_decision(
        chosen_article_id=selected_id,
        rejected_article_ids=rejected,
        decision_mode=DecisionMode.AUTO if explain.get("mode") == "profile" else DecisionMode.MANUAL,
        confidence=explain.get("confidence"),
        candidates=[
            {
                "article_id": a.id,
                "score": s.final_score,
                "top_drivers": (s.features or {}).get("top_drivers", []),
                "novelty_reason": (s.features or {}).get("novelty_reason", ""),
            }
            for a, s in top3
        ],
    )

    return selected_id


def pick_hourly_backfill(hours_back: int = 24, per_hour: int = 1) -> dict:
    """
    Backfill Selected Hour for the last N hours: pick up to `per_hour` articles for each
    hour bucket (aligned to user's timezone), skipping already-filled buckets.

    Returns: {"ok": True, "hours": int, "selected": int, "filled_buckets": int, "selected_ids": [...]}
    """
    hours = max(1, min(int(hours_back or 24), 168))  # cap at 7 days
    per_hour_n = max(1, min(int(per_hour or 1), 3))
    tz_name = _get_tz_name()
    now_utc = datetime.utcnow()
    current_bucket = _hour_bucket_utc(now_utc, tz_name)

    selected_ids: list[int] = []
    filled_buckets = 0

    # Start from the oldest hour so review chat receives messages in chronological order.
    buckets = [current_bucket - timedelta(hours=i) for i in range(hours - 1, -1, -1)]

    # Backward-compat: older rows may have status=selected_hourly but bucket is NULL.
    # Assign a bucket from updated_at so they can participate in backlog sending and dedup.
    cutoff = now_utc - timedelta(hours=hours)
    with session_scope() as session:
        old_rows = session.scalars(
            select(Article).where(
                Article.status == ArticleStatus.SELECTED_HOURLY,
                Article.selected_hour_bucket_utc.is_(None),
                Article.updated_at >= cutoff,
            )
        ).all()
        for a in old_rows:
            a.selected_hour_bucket_utc = _hour_bucket_utc(a.updated_at or now_utc, tz_name)
            a.updated_at = datetime.utcnow()

    # Track clusters already selected/published in last 24h + during this run.
    day_ago = now_utc - timedelta(hours=24)
    selected_clusters: set[str] = set()
    with session_scope() as session:
        selected_clusters.update(
            row[0]
            for row in session.execute(
                select(Article.cluster_key).where(
                    Article.status.in_([ArticleStatus.SELECTED_HOURLY, ArticleStatus.PUBLISHED]),
                    Article.updated_at >= day_ago,
                    Article.cluster_key.is_not(None),
                )
            ).all()
        )

    for bucket_start in buckets:
        bucket_end = bucket_start + timedelta(hours=1)
        with session_scope() as session:
            exists = session.scalars(
                select(Article.id).where(
                    Article.status == ArticleStatus.SELECTED_HOURLY,
                    Article.selected_hour_bucket_utc == bucket_start,
                )
            ).first()
            if exists:
                filled_buckets += 1
                continue

            base = (
                select(Article, Score)
                .join(Score, Score.article_id == Article.id)
                .where(
                    Article.status.in_([ArticleStatus.SCORED, ArticleStatus.REVIEW, ArticleStatus.READY]),
                    Article.status != ArticleStatus.DOUBLE,
                    Article.status != ArticleStatus.ARCHIVED,
                    Article.status != ArticleStatus.REJECTED,
                    Article.status != ArticleStatus.PUBLISHED,
                    Article.status != ArticleStatus.SELECTED_HOURLY,
                    ((Article.created_at >= bucket_start) & (Article.created_at < bucket_end))
                    | ((Article.published_at >= bucket_start) & (Article.published_at < bucket_end)),
                )
                .options(joinedload(Article.source))
                .order_by(Score.final_score.desc())
                .limit(200)
            )
            rows = session.execute(base).all()

            # If that hour has no candidates, just skip (no forced fallback across hours).
            if not rows:
                continue

        # Filter by cluster_key dedup against existing selections/publishes.
        filtered = [(a, s) for a, s in rows if (a.cluster_key not in selected_clusters)]
        if not filtered:
            continue
        filtered.sort(key=lambda x: _audience_adjusted_score(x[0], x[1]), reverse=True)

        picked_batch = filtered[:per_hour_n]
        for a, _s in picked_batch:
            with session_scope() as session:
                art = session.get(Article, int(a.id))
                if not art:
                    continue
                # Double-check bucket isn't filled by a parallel request.
                exists2 = session.scalars(
                    select(Article.id).where(
                        Article.status == ArticleStatus.SELECTED_HOURLY,
                        Article.selected_hour_bucket_utc == bucket_start,
                    )
                ).first()
                if exists2:
                    break
                art.status = ArticleStatus.SELECTED_HOURLY
                art.selected_hour_bucket_utc = bucket_start
                art.updated_at = datetime.utcnow()
                selected_ids.append(int(art.id))
                if art.cluster_key:
                    selected_clusters.add(str(art.cluster_key))
                filled_buckets += 1
                # Only 1 per bucket by default; if per_hour>1 we'll fill more.
                if per_hour_n == 1:
                    break

    return {
        "ok": True,
        "hours": hours,
        "per_hour": per_hour_n,
        "selected": len(selected_ids),
        "filled_buckets": filled_buckets,
        "selected_ids": selected_ids,
        "tz": tz_name,
    }


def auto_select_by_profile(top_n: int = 5) -> dict:
    candidates = _hourly_candidates(limit=max(top_n, 5))
    if not candidates:
        return {"ok": False, "reason": "no_candidates"}
    selected_id, explain = _choose_with_profile(candidates[:top_n], top_n=top_n)
    return {"ok": True, "article_id": selected_id, **explain}


def run_hourly_cycle(backfill_days: int = 1) -> dict:
    # Fast ingestion: do not fetch full pages here.
    # Full text is handled by the explicit enrich step.
    ingest = run_ingestion_fast(days_back=backfill_days, max_entries=200)
    enrich = enrich_summary_only_articles(limit=300, days_back=30)
    embedded = process_embeddings_and_dedup(limit=300)
    scored = run_scoring(limit=300)
    top_id = pick_hourly_top()

    if top_id:
        generate_ru_summary(top_id)
        generate_image_card(top_id)

    return {
        "ingestion": ingest,
        "enrich_summary_only": enrich,
        "embedded": embedded,
        "scored": scored,
        "top_article_id": top_id,
    }
