from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sqlalchemy import select

from app.core.config import settings
from app.db import session_scope
from app.models import (
    Article,
    AuditLog,
    DecisionMode,
    DriftMetric,
    EditorFeedback,
    ModelArtifact,
    PreferenceProfile,
    RankingExample,
    Score,
    SelectionDecision,
)
from app.services.llm import get_client, track_usage_from_response
from app.services.utils import stable_hash


MODEL_DIR = Path("app/static/models")
MODEL_DIR.mkdir(parents=True, exist_ok=True)


def rebuild_preference_profile(min_feedback: int = 20) -> dict:
    with session_scope() as session:
        feedbacks = session.scalars(select(EditorFeedback).order_by(EditorFeedback.created_at.asc())).all()
        deletion_logs = session.scalars(
            select(AuditLog)
            .where(AuditLog.action == "article_delete_feedback")
            .order_by(AuditLog.created_at.asc())
        ).all()

    textual_feedback: list[str] = [f"CHOSEN: {f.explanation_text}" for f in feedbacks]
    for log in deletion_logs:
        payload = log.payload or {}
        reason = str(payload.get("reason") or "").strip()
        title = str(payload.get("title") or "").strip()
        if reason:
            textual_feedback.append(f"DELETED: {reason}" + (f" | title: {title}" if title else ""))

    if len(textual_feedback) < min_feedback:
        return {"ok": False, "reason": f"need_at_least_{min_feedback}_feedback_items"}

    joined = "\n".join(f"- {x}" for x in textual_feedback[-800:])
    prompt = f"""
Ниже объяснения редактора, почему он выбирал новости, и почему удалял новости после ревью.
Сформируй preference profile для авто-выбора.
Верни текст в виде коротких правил (8-20 пунктов), на английском, без воды.

{joined}
"""

    if not settings.openrouter_api_key:
        profile_text = "- Prefer high significance and relevance\n- Avoid low trust sources"
    else:
        client = get_client()
        resp = client.chat.completions.create(
            model=settings.llm_text_model,
            messages=[
                {"role": "system", "content": "Extract editorial preference rules."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        track_usage_from_response(resp, operation="preference.rebuild_profile", model=settings.llm_text_model, kind="chat")
        profile_text = (resp.choices[0].message.content or "").strip()

    with session_scope() as session:
        for p in session.scalars(select(PreferenceProfile).where(PreferenceProfile.active.is_(True))).all():
            p.active = False
        session.add(PreferenceProfile(profile_text=profile_text, active=True))

    return {
        "ok": True,
        "profile_length": len(profile_text),
        "chosen_feedback_items": len(feedbacks),
        "deleted_feedback_items": len([x for x in deletion_logs if (x.payload or {}).get("reason")]),
    }


def get_active_profile() -> str:
    with session_scope() as session:
        profile = session.scalars(
            select(PreferenceProfile)
            .where(PreferenceProfile.active.is_(True))
            .order_by(PreferenceProfile.id.desc())
            .limit(1)
        ).first()
    return profile.profile_text if profile else ""


def save_selection_decision(chosen_article_id: int, rejected_article_ids: list[int], decision_mode: DecisionMode, confidence: float | None, candidates: list[dict] | None = None) -> int:
    with session_scope() as session:
        rec = SelectionDecision(
            chosen_article_id=chosen_article_id,
            rejected_article_ids=rejected_article_ids,
            decision_mode=decision_mode,
            confidence=confidence,
            candidates=candidates,
        )
        session.add(rec)
        session.flush()
        return rec.id


def build_ranking_dataset(days: int = 14) -> dict:
    cutoff = datetime.utcnow().timestamp() - days * 24 * 3600
    batch_id = stable_hash(f"rank-{datetime.utcnow().isoformat()}")[:12]
    created = 0

    with session_scope() as session:
        decisions = session.scalars(select(SelectionDecision).order_by(SelectionDecision.created_at.asc())).all()

        for d in decisions:
            if d.created_at.timestamp() < cutoff:
                continue
            ids = [d.chosen_article_id] + [int(x) for x in (d.rejected_article_ids or [])]
            for aid in ids:
                score = session.get(Score, aid)
                article = session.get(Article, aid)
                if not score or not article:
                    continue
                label = 1 if aid == d.chosen_article_id else 0
                f = score.features or {}
                feats = {
                    "freshness": float(f.get("freshness", score.freshness / 10.0)),
                    "source_priority": float(f.get("source_priority", 0.0)),
                    "entity_count": float(f.get("entity_count", 0.0)),
                    "number_count": float(f.get("number_count", 0.0)),
                    "trend_velocity": float(f.get("trend_velocity", 0.0)),
                    "coverage": float(f.get("coverage", 0.0)),
                    "significance": float(f.get("significance", score.significance / 10.0)),
                    "relevance": float(f.get("relevance", score.relevance / 10.0)),
                    "virality": float(f.get("virality", score.virality / 10.0)),
                    "longevity": float(f.get("longevity", score.longevity / 10.0)),
                    "scale": float(f.get("scale", score.scale / 10.0)),
                    "novelty": float(f.get("novelty", score.uniqueness / 10.0)),
                    "base_final_score": score.final_score,
                    "hour": article.created_at.hour,
                    "dow": article.created_at.weekday(),
                }
                session.add(
                    RankingExample(
                        article_id=aid,
                        batch_id=batch_id,
                        context_hour=article.created_at.hour,
                        context_day_of_week=article.created_at.weekday(),
                        topic=(article.tags[0] if isinstance(article.tags, list) and article.tags else None),
                        label=label,
                        features=feats,
                    )
                )
                created += 1

    return {"ok": True, "batch_id": batch_id, "created": created}


def train_ranking_model(batch_id: str) -> dict:
    with session_scope() as session:
        rows = session.scalars(select(RankingExample).where(RankingExample.batch_id == batch_id)).all()

    if len(rows) < 20:
        return {"ok": False, "reason": "not_enough_examples"}

    feature_names = [
        "freshness",
        "source_priority",
        "entity_count",
        "number_count",
        "trend_velocity",
        "coverage",
        "significance",
        "relevance",
        "virality",
        "longevity",
        "scale",
        "novelty",
        "base_final_score",
        "hour",
        "dow",
    ]

    x = np.array([[float(r.features.get(k, 0.0)) for k in feature_names] for r in rows], dtype=float)
    y = np.array([int(r.label) for r in rows], dtype=int)

    model = LogisticRegression(max_iter=1000)
    model.fit(x, y)
    probs = model.predict_proba(x)[:, 1]
    auc = float(roc_auc_score(y, probs)) if len(np.unique(y)) > 1 else 0.5

    artifact = {
        "feature_names": feature_names,
        "coef": model.coef_[0].tolist(),
        "intercept": float(model.intercept_[0]),
        "batch_id": batch_id,
        "auc_train": auc,
    }
    version = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    path = MODEL_DIR / f"ranking_{version}.json"
    path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")

    with session_scope() as session:
        for m in session.scalars(select(ModelArtifact).where(ModelArtifact.name == "ranking", ModelArtifact.active.is_(True))).all():
            m.active = False
        session.add(
            ModelArtifact(
                name="ranking",
                version=version,
                artifact_path=str(path),
                metrics={"auc_train": auc, "n": len(rows)},
                active=True,
            )
        )

    return {"ok": True, "version": version, "auc_train": auc, "n": len(rows)}


def get_active_ranking_artifact() -> dict | None:
    with session_scope() as session:
        model = session.scalars(
            select(ModelArtifact)
            .where(ModelArtifact.name == "ranking", ModelArtifact.active.is_(True))
            .order_by(ModelArtifact.id.desc())
            .limit(1)
        ).first()
    if not model:
        return None
    path = Path(model.artifact_path)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def detect_preference_drift(window: int = 200, threshold: float = 0.22) -> dict:
    with session_scope() as session:
        fb = session.scalars(select(EditorFeedback).order_by(EditorFeedback.created_at.desc()).limit(window)).all()

    if len(fb) < 30:
        return {"ok": False, "reason": "not_enough_feedback"}

    conf = [f.confidence for f in fb if isinstance(f.confidence, int)]
    if len(conf) < 10:
        return {"ok": False, "reason": "not_enough_confidence_values"}

    split = len(conf) // 2
    old_avg = float(np.mean(conf[split:]))
    new_avg = float(np.mean(conf[:split]))
    delta = abs(new_avg - old_avg) / 10.0
    drifted = delta >= threshold

    with session_scope() as session:
        session.add(
            DriftMetric(
                metric_name="editor_confidence_shift",
                value=delta,
                threshold=threshold,
                drifted=drifted,
            )
        )

    return {"ok": True, "drifted": drifted, "delta": delta, "old_avg": old_avg, "new_avg": new_avg}
