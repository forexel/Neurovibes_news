from __future__ import annotations

import math

from sqlalchemy import select

from app.core.config import settings
from app.db import session_scope
from app.models import Article, ArticleStatus, DecisionMode, Score
from app.services.preference import get_active_profile, get_active_ranking_artifact, save_selection_decision


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _prob_from_artifact(features: dict, artifact: dict | None) -> float:
    if not artifact:
        base = float(features.get("base_final_score", 0.0))
        return max(0.0, min(1.0, base))

    names = artifact.get("feature_names", [])
    coef = artifact.get("coef", [])
    intercept = float(artifact.get("intercept", 0.0))
    z = intercept
    for i, name in enumerate(names):
        z += float(features.get(name, 0.0)) * float(coef[i])
    return _sigmoid(z)


def decide_and_maybe_publish(top_n: int = 5) -> dict:
    profile = get_active_profile()
    if not profile:
        return {"ok": False, "reason": "profile_missing"}

    with session_scope() as session:
        rows = session.execute(
            select(Article, Score)
            .join(Score, Score.article_id == Article.id)
            .where(Article.status.in_([ArticleStatus.SCORED, ArticleStatus.READY]))
            .order_by(Score.final_score.desc())
            .limit(top_n)
        ).all()

    if not rows:
        return {"ok": False, "reason": "no_candidates"}

    artifact = get_active_ranking_artifact()
    scored = []
    for a, s in rows:
        f = s.features or {}
        feats = {
            "freshness": float(f.get("freshness", s.freshness / 10.0)),
            "source_priority": float(f.get("source_priority", 0.0)),
            "entity_count": float(f.get("entity_count", 0.0)),
            "number_count": float(f.get("number_count", 0.0)),
            "trend_velocity": float(f.get("trend_velocity", 0.0)),
            "coverage": float(f.get("coverage", 0.0)),
            "significance": float(f.get("significance", s.significance / 10.0)),
            "relevance": float(f.get("relevance", s.relevance / 10.0)),
            "virality": float(f.get("virality", s.virality / 10.0)),
            "longevity": float(f.get("longevity", s.longevity / 10.0)),
            "scale": float(f.get("scale", s.scale / 10.0)),
            "novelty": float(f.get("novelty", s.uniqueness / 10.0)),
            "base_final_score": s.final_score,
            "hour": a.created_at.hour,
            "dow": a.created_at.weekday(),
        }
        p = _prob_from_artifact(feats, artifact)
        scored.append((a, s, p))

    scored.sort(key=lambda x: x[2], reverse=True)
    best = scored[0]
    second = scored[1] if len(scored) > 1 else None
    confidence = best[2]
    uncertainty = max(0.0, min(1.0, 1.0 - (confidence - (second[2] if second else 0.0))))

    if confidence >= settings.auto_publish_confidence_threshold and uncertainty <= settings.auto_publish_uncertainty_threshold:
        with session_scope() as session:
            article = session.get(Article, best[0].id)
            if article:
                article.status = ArticleStatus.READY
        save_selection_decision(
            chosen_article_id=best[0].id,
            rejected_article_ids=[x[0].id for x in scored[1:]],
            decision_mode=DecisionMode.AUTO,
            confidence=confidence,
            candidates=[{"article_id": x[0].id, "prob": x[2]} for x in scored],
        )
        return {"ok": True, "mode": "auto", "article_id": best[0].id, "confidence": confidence, "uncertainty": uncertainty}

    return {
        "ok": True,
        "mode": "manual",
        "reason": "threshold_not_met",
        "candidate_article_id": best[0].id,
        "confidence": confidence,
        "uncertainty": uncertainty,
    }
