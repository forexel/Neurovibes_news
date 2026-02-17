from __future__ import annotations

import json
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import joinedload

from app.core.config import settings
from app.db import session_scope
from app.models import Article, ArticleStatus, DecisionMode, Score
from app.services.content_generation import generate_image_card, generate_ru_summary
from app.services.embedding_dedup import process_embeddings_and_dedup
from app.services.ingestion import enrich_summary_only_articles, run_ingestion
from app.services.llm import get_client, track_usage_from_response
from app.services.preference import get_active_profile, save_selection_decision
from app.services.scoring import run_scoring


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


def auto_select_by_profile(top_n: int = 5) -> dict:
    candidates = _hourly_candidates(limit=max(top_n, 5))
    if not candidates:
        return {"ok": False, "reason": "no_candidates"}
    selected_id, explain = _choose_with_profile(candidates[:top_n], top_n=top_n)
    return {"ok": True, "article_id": selected_id, **explain}


def run_hourly_cycle(backfill_days: int = 1) -> dict:
    ingest = run_ingestion(days_back=backfill_days)
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
