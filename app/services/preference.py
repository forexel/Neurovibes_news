from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sqlalchemy import func, select

from app.core.config import settings
from app.db import session_scope
from app.models import (
    Article,
    ArticleStatus,
    AuditLog,
    DecisionMode,
    DriftMetric,
    EditorFeedback,
    ModelArtifact,
    PreferenceProfile,
    RankingExample,
    Score,
    SelectionDecision,
    TrainingEvent,
)
from app.services.runtime_settings import get_runtime_float
from app.services.llm import get_client, track_usage_from_response
from app.services.utils import stable_hash


MODEL_DIR = Path("app/static/models")
MODEL_DIR.mkdir(parents=True, exist_ok=True)

EDITOR_CHOICE_MODEL_NAME = "editor_choice"
EDITOR_CHOICE_FEATURES = [
    "freshness",
    "source_priority",
    "entity_count",
    "number_count",
    "trend_velocity",
    "coverage",
    "cluster_size",
    "duplicate_flag",
    "has_image",
    "content_short",
    "content_medium",
    "content_long",
    "significance",
    "relevance",
    "virality",
    "longevity",
    "scale",
    "novelty",
    "business_it",
    "geek_penalty",
    "rule_score",
    "uncertainty",
    "rank_by_rule_score",
    "delta_to_best_rule",
    "hour_sin",
    "hour_cos",
    "hours_since_published_norm",
    "published_so_far_today_norm",
    "hours_left_today_norm",
    # reason tags (one-hot)
    "tag_breakthrough",
    "tag_funding",
    "tag_product_release",
    "tag_benchmark",
    "tag_regulation",
    "tag_practical_tool",
    "tag_global_shift",
    "tag_hype",
    "tag_too_local",
    "tag_duplicate",
]
_TAGS = [
    "breakthrough",
    "funding",
    "product_release",
    "benchmark",
    "regulation",
    "practical_tool",
    "global_shift",
    "hype",
    "too_local",
    "duplicate",
]


def _sigmoid(z: float) -> float:
    z = float(z)
    if z >= 0:
        ez = np.exp(-z)
        return float(1.0 / (1.0 + ez))
    ez = np.exp(z)
    return float(ez / (1.0 + ez))


def _norm01(v: float | int | None, denom: float = 10.0) -> float:
    try:
        x = float(v or 0.0) / float(denom)
    except Exception:
        x = 0.0
    return float(max(0.0, min(1.0, x)))


def _guess_reason_tags(reason_text: str | None) -> list[str]:
    text = (reason_text or "").lower()
    tags: list[str] = []
    rules = {
        "breakthrough": ["прорыв", "breakthrough", "революц", "first", "впервые"],
        "funding": ["инвест", "funding", "m&a", "сделк", "acquire", "раунд"],
        "product_release": ["релиз", "release", "launch", "launched", "update", "версия"],
        "benchmark": ["benchmark", "бенчмарк", "точност", "latency", "скорость", "сравнен"],
        "regulation": ["регуля", "закон", "policy", "compliance", "безопас", "fraud", "мошенн"],
        "practical_tool": ["практич", "tool", "инструмент", "для бизнеса", "workflow", "use case"],
        "global_shift": ["рынок", "global", "стратег", "монопол", "platform shift", "сигнал"],
        "hype": ["хайп", "скучн", "мнение", "opinion", "noise", "неважно"],
        "too_local": ["локал", "india", "индия", "узко", "too local", "не для нашей"],
        "duplicate": ["дубл", "повтор", "duplicate", "already"],
    }
    for tag, keywords in rules.items():
        if any(k in text for k in keywords):
            tags.append(tag)
    return sorted(set(tags))


def _feature_snapshot(article: Article, score: Score | None, reason_tags: list[str] | None = None) -> dict:
    f = (score.features or {}) if score and isinstance(score.features, dict) else {}
    now = datetime.utcnow()
    published = article.published_at or article.created_at or now
    age_hours = max(0.0, (now - published).total_seconds() / 3600.0)
    content_mode = (article.content_mode or "summary_only").strip().lower()
    content_short = 1.0 if content_mode == "summary_only" else 0.0
    content_long = 1.0 if content_mode == "full" else 0.0
    content_medium = 0.0 if (content_short or content_long) else 1.0
    rule_score = float(score.final_score or 0.0) if score else 0.0
    hour = int((article.created_at or now).hour)
    hours_left = max(0, 23 - hour)

    out = {
        "freshness": _norm01(f.get("freshness", score.freshness if score else 0.0)),
        "source_priority": _norm01(f.get("source_priority", 0.0), denom=1.0 if float(f.get("source_priority", 0.0) or 0.0) <= 1.0 else 10.0),
        "entity_count": _norm01(f.get("entity_count", 0.0), denom=1.0 if float(f.get("entity_count", 0.0) or 0.0) <= 1.0 else 10.0),
        "number_count": _norm01(f.get("number_count", 0.0), denom=1.0 if float(f.get("number_count", 0.0) or 0.0) <= 1.0 else 10.0),
        "trend_velocity": _norm01(f.get("trend_velocity", 0.0), denom=1.0 if float(f.get("trend_velocity", 0.0) or 0.0) <= 1.0 else 10.0),
        "coverage": _norm01(f.get("coverage", f.get("cross_source_coverage", 0.0)), denom=1.0 if float(f.get("coverage", f.get("cross_source_coverage", 0.0)) or 0.0) <= 1.0 else 10.0),
        "cluster_size": _norm01(f.get("cluster_size", 0.0), denom=1.0 if float(f.get("cluster_size", 0.0) or 0.0) <= 1.0 else 10.0),
        "duplicate_flag": 1.0 if article.status.value == "double" or bool(article.double_of_article_id) else 0.0,
        "has_image": 1.0 if (article.image_url or article.generated_image_path) else 0.0,
        "content_short": content_short,
        "content_medium": content_medium,
        "content_long": content_long,
        "significance": _norm01(f.get("significance", score.significance if score else 0.0)),
        "relevance": _norm01(f.get("relevance", score.relevance if score else 0.0)),
        "virality": _norm01(f.get("virality", score.virality if score else 0.0)),
        "longevity": _norm01(f.get("longevity", score.longevity if score else 0.0)),
        "scale": _norm01(f.get("scale", score.scale if score else 0.0)),
        "novelty": _norm01(f.get("novelty", score.uniqueness if score else 0.0)),
        "business_it": _norm01(f.get("business_it", 0.0), denom=1.0 if float(f.get("business_it", 0.0) or 0.0) <= 1.0 else 10.0),
        "geek_penalty": _norm01(f.get("geek_penalty", 1.0), denom=1.0),
        "rule_score": float(max(0.0, min(10.0, rule_score))) / 10.0,
        "uncertainty": _norm01(getattr(score, "uncertainty", None), denom=1.0 if float(getattr(score, "uncertainty", 0.0) or 0.0) <= 1.0 else 10.0),
        "rank_by_rule_score": 0.0,
        "delta_to_best_rule": 0.0,
        "hour_sin": float((np.sin(2.0 * np.pi * (hour / 24.0)) + 1.0) / 2.0),
        "hour_cos": float((np.cos(2.0 * np.pi * (hour / 24.0)) + 1.0) / 2.0),
        "hours_since_published_norm": float(max(0.0, min(1.0, age_hours / 24.0))),
        "published_so_far_today_norm": 0.0,
        "hours_left_today_norm": float(max(0.0, min(1.0, hours_left / 23.0 if 23 else 0.0))),
    }
    tags = set(reason_tags or [])
    for tag in _TAGS:
        out[f"tag_{tag}"] = 1.0 if tag in tags else 0.0
    return out


def _candidate_ids_for_article(session, article: Article) -> list[int]:
    hour_bucket = article.selected_hour_bucket_utc
    if hour_bucket is None:
        # approximate from created/published time; align to UTC hour
        base_dt = article.published_at or article.created_at or datetime.utcnow()
        hour_bucket = base_dt.replace(minute=0, second=0, microsecond=0)
    latest = session.scalars(select(SelectionDecision).order_by(SelectionDecision.id.desc()).limit(50)).all()
    for d in latest:
        ids = [int(d.chosen_article_id)] + [int(x) for x in (d.rejected_article_ids or []) if str(x).isdigit() or isinstance(x, int)]
        if int(article.id) in ids:
            return ids
    return [int(article.id)]


def log_training_event(
    *,
    article_id: int,
    decision: str,
    label: int,
    reason_text: str | None = None,
    reason_tags: list[str] | None = None,
    user_id: int | None = None,
    override: bool = False,
    final_outcome: str | None = None,
) -> dict:
    decision = (decision or "").strip().lower()
    if decision not in {"publish", "top_pick", "hide", "delete", "defer", "skip"}:
        return {"ok": False, "error": "bad_decision"}
    tags = sorted(set([t for t in (reason_tags or []) if t]))
    if not tags and reason_text:
        tags = _guess_reason_tags(reason_text)

    with session_scope() as session:
        article = session.get(Article, int(article_id))
        if not article:
            return {"ok": False, "error": "article_not_found"}
        score = session.get(Score, int(article_id))
        features = _feature_snapshot(article, score, tags)
        candidate_ids = _candidate_ids_for_article(session, article)

        # enrich context-dependent fields using current candidate set
        if candidate_ids:
            score_rows = session.scalars(select(Score).where(Score.article_id.in_(candidate_ids))).all()
            rule_scores = sorted([float(s.final_score or 0.0) for s in score_rows], reverse=True)
            current_rule = float(score.final_score or 0.0) if score else 0.0
            if rule_scores:
                best = rule_scores[0]
                features["delta_to_best_rule"] = float(max(0.0, min(1.0, (best - current_rule) / 10.0)))
                # 1-based rank normalized to [0..1], better rank => closer to 1
                try:
                    rank = 1 + sum(1 for rs in rule_scores if rs > current_rule)
                except Exception:
                    rank = len(rule_scores)
                features["rank_by_rule_score"] = float(max(0.0, min(1.0, 1.0 - ((rank - 1) / max(1, len(rule_scores) - 1)))))

        # daily context for "best of bad" behavior
        day_start = (article.created_at or datetime.utcnow()).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        published_count = session.scalar(
            select(func.count()).select_from(Article).where(
                Article.status == ArticleStatus.PUBLISHED,
                Article.updated_at >= day_start,
                Article.updated_at < day_end,
            )
        ) or 0
        features["published_so_far_today_norm"] = float(max(0.0, min(1.0, float(published_count) / 5.0)))

        # ML snapshot at decision
        ml_score = None
        ml_meta = predict_editor_choice_prob(features)
        if ml_meta.get("ok"):
            ml_score = float(ml_meta.get("prob") or 0.0)

        published_time = article.published_at or article.created_at
        event_time = datetime.utcnow()
        delay_minutes = None
        if published_time:
            try:
                delay_minutes = max(0, int((event_time - published_time).total_seconds() // 60))
            except Exception:
                delay_minutes = None
        hour_bucket = (article.selected_hour_bucket_utc or (published_time or event_time).replace(minute=0, second=0, microsecond=0))

        rec = TrainingEvent(
            user_id=user_id,
            article_id=int(article_id),
            decision=decision,
            label=int(1 if label else 0),
            hour_bucket=hour_bucket,
            candidate_set_ids=[int(x) for x in candidate_ids],
            features_json=features,
            reason_text=(reason_text or "").strip() or None,
            reason_tags=tags or None,
            rule_score=float(score.final_score or 0.0) if score else None,
            ml_score_at_decision=ml_score,
            model_version=(ml_meta.get("version") if ml_meta.get("ok") else None),
            override=bool(override),
            event_time=event_time,
            article_published_at=published_time,
            delay_minutes=delay_minutes,
            final_outcome=final_outcome,
        )
        session.add(rec)
        session.flush()
        return {"ok": True, "id": int(rec.id), "reason_tags": tags, "model_version": rec.model_version}


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


def build_editor_choice_dataset(days_back: int = 30) -> dict:
    cutoff = datetime.utcnow() - timedelta(days=max(1, int(days_back or 30)))
    with session_scope() as session:
        rows = session.scalars(
            select(TrainingEvent)
            .where(
                TrainingEvent.created_at >= cutoff,
                TrainingEvent.decision.in_(["publish", "hide", "delete", "defer"]),
            )
            .order_by(TrainingEvent.created_at.asc())
        ).all()

    if not rows:
        return {"ok": False, "reason": "no_training_events"}

    X: list[list[float]] = []
    y: list[int] = []
    meta: list[dict] = []
    for r in rows:
        feats = dict(r.features_json or {})
        # If reason tags were updated later, ensure one-hot is present.
        for tag in _TAGS:
            feats.setdefault(f"tag_{tag}", 1.0 if tag in set(r.reason_tags or []) else 0.0)
        vec = [float(feats.get(k, 0.0) or 0.0) for k in EDITOR_CHOICE_FEATURES]
        X.append(vec)
        # label target for "quality/choose eventually": publish=1, others=0.
        y.append(int(r.label))
        meta.append(
            {
                "event_id": int(r.id),
                "created_at": r.created_at.isoformat(),
                "article_id": int(r.article_id),
                "decision": r.decision,
            }
        )
    return {"ok": True, "X": X, "y": y, "meta": meta, "n": len(rows)}


def train_editor_choice_model(days_back: int = 30, min_samples: int = 40) -> dict:
    ds = build_editor_choice_dataset(days_back=days_back)
    if not ds.get("ok"):
        return ds
    X = np.array(ds["X"], dtype=float)
    y = np.array(ds["y"], dtype=int)
    n = int(len(y))
    if n < int(min_samples):
        return {"ok": False, "reason": "not_enough_samples", "n": n, "min_samples": int(min_samples)}
    if len(np.unique(y)) < 2:
        return {"ok": False, "reason": "need_both_classes", "n": n}

    split = max(1, min(n - 1, int(n * 0.8)))
    X_train, X_val = X[:split], X[split:]
    y_train, y_val = y[:split], y[split:]
    if len(np.unique(y_train)) < 2:
        return {"ok": False, "reason": "train_split_one_class", "n": n}

    model = LogisticRegression(max_iter=1000, class_weight="balanced")
    model.fit(X_train, y_train)

    p_train = model.predict_proba(X_train)[:, 1]
    p_val = model.predict_proba(X_val)[:, 1] if len(X_val) else np.array([])
    auc_train = float(roc_auc_score(y_train, p_train)) if len(np.unique(y_train)) > 1 else 0.5
    auc_val = float(roc_auc_score(y_val, p_val)) if (len(y_val) and len(np.unique(y_val)) > 1) else None

    # Simple precision@1 and ndcg@5 over hourly groups from meta timestamps
    meta = ds.get("meta") or []
    val_meta = meta[split:]
    grouped: dict[str, list[tuple[float, int]]] = {}
    for i, row in enumerate(val_meta):
        if i >= len(p_val):
            break
        key = str(row.get("created_at", ""))[:13]  # YYYY-MM-DDTHH
        grouped.setdefault(key, []).append((float(p_val[i]), int(y_val[i])))
    p1_hits = 0
    ndcg5_vals: list[float] = []
    for _, items in grouped.items():
        if not items:
            continue
        ranked = sorted(items, key=lambda x: x[0], reverse=True)
        p1_hits += 1 if ranked[0][1] == 1 else 0
        dcg = 0.0
        idcg = 0.0
        topk = ranked[:5]
        ideal = sorted([lbl for _, lbl in items], reverse=True)[:5]
        for idx, (_, lbl) in enumerate(topk, start=1):
            dcg += (float(lbl) / np.log2(idx + 1))
        for idx, lbl in enumerate(ideal, start=1):
            idcg += (float(lbl) / np.log2(idx + 1))
        ndcg5_vals.append(float(dcg / idcg) if idcg > 0 else 0.0)
    precision_at_1 = (p1_hits / len(grouped)) if grouped else None
    ndcg5 = (float(np.mean(ndcg5_vals)) if ndcg5_vals else None)

    artifact = {
        "feature_names": EDITOR_CHOICE_FEATURES,
        "coef": model.coef_[0].tolist(),
        "intercept": float(model.intercept_[0]),
        "trained_at": datetime.utcnow().isoformat(),
        "days_back": int(days_back),
        "n_train": int(len(y_train)),
        "n_val": int(len(y_val)),
        "metrics": {
            "roc_auc_train": auc_train,
            "roc_auc_val": auc_val,
            "precision_at_1": precision_at_1,
            "ndcg_at_5": ndcg5,
        },
    }
    version = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    path = MODEL_DIR / f"editor_choice_{version}.json"
    path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")

    with session_scope() as session:
        for m in session.scalars(
            select(ModelArtifact).where(ModelArtifact.name == EDITOR_CHOICE_MODEL_NAME, ModelArtifact.active.is_(True))
        ).all():
            m.active = False
        session.add(
            ModelArtifact(
                name=EDITOR_CHOICE_MODEL_NAME,
                version=version,
                artifact_path=str(path),
                metrics=artifact["metrics"] | {"n_train": len(y_train), "n_val": len(y_val)},
                active=True,
            )
        )
    return {"ok": True, "version": version, **artifact["metrics"], "n": n}


def get_active_editor_choice_artifact() -> dict | None:
    with session_scope() as session:
        row = session.scalars(
            select(ModelArtifact)
            .where(ModelArtifact.name == EDITOR_CHOICE_MODEL_NAME, ModelArtifact.active.is_(True))
            .order_by(ModelArtifact.id.desc())
            .limit(1)
        ).first()
    if not row:
        return None
    p = Path(row.artifact_path)
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    data["_version"] = row.version
    return data


def predict_editor_choice_prob(features: dict | None) -> dict:
    artifact = get_active_editor_choice_artifact()
    if not artifact:
        return {"ok": False, "reason": "no_model"}
    names = artifact.get("feature_names") or []
    coef = artifact.get("coef") or []
    if not names or not coef or len(names) != len(coef):
        return {"ok": False, "reason": "bad_artifact"}
    feats = dict(features or {})
    x = np.array([float(feats.get(k, 0.0) or 0.0) for k in names], dtype=float)
    z = float(np.dot(x, np.array(coef, dtype=float)) + float(artifact.get("intercept") or 0.0))
    prob = _sigmoid(z)
    uncertainty = float(1.0 - abs(prob - 0.5) * 2.0)
    return {"ok": True, "prob": prob, "uncertainty": uncertainty, "version": artifact.get("_version")}


def blended_editor_score(rule_score_0_10: float, features: dict | None) -> dict:
    """
    Blend current rule score with editor-choice probability.
    Weight is runtime-configurable and can be increased gradually.
    """
    rule_norm = float(max(0.0, min(10.0, float(rule_score_0_10 or 0.0)))) / 10.0
    ml = predict_editor_choice_prob(features)
    if not ml.get("ok"):
        return {"ok": False, "final": rule_norm, "rule": rule_norm}
    w = float(max(0.0, min(0.8, get_runtime_float("ml_editor_choice_weight", default=0.1))))
    final = ((1.0 - w) * rule_norm) + (w * float(ml["prob"]))
    return {
        "ok": True,
        "final": float(max(0.0, min(1.0, final))),
        "rule": rule_norm,
        "ml_prob": float(ml["prob"]),
        "uncertainty": float(ml.get("uncertainty") or 0.0),
        "weight": w,
        "version": ml.get("version"),
    }


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


def backfill_training_and_restore_unreasoned_archived(
    *,
    restore_status: ArticleStatus = ArticleStatus.INBOX,
    max_articles: int = 50000,
) -> dict:
    """
    1) Backfill historical delete/hide reasons from archived articles (+ audit logs fallback) into training_events.
    2) Restore archived/rejected articles that have no deletion/hide reason.
    """
    scanned = 0
    backfilled = 0
    restored = 0
    already_present = 0
    errors = 0

    with session_scope() as session:
        articles = session.scalars(
            select(Article)
            .where(Article.status.in_([ArticleStatus.ARCHIVED, ArticleStatus.REJECTED]))
            .order_by(Article.updated_at.desc())
            .limit(max_articles)
        ).all()

        # Build audit fallback map for delete reasons (latest wins).
        audit_reason_by_article: dict[int, str] = {}
        logs = session.scalars(
            select(AuditLog)
            .where(AuditLog.action == "article_delete_feedback")
            .order_by(AuditLog.created_at.desc())
            .limit(max_articles * 2)
        ).all()
        for log in logs:
            try:
                aid = int(str(log.entity_id or "0"))
            except Exception:
                continue
            if aid <= 0 or aid in audit_reason_by_article:
                continue
            payload = log.payload or {}
            reason = str(payload.get("reason") or "").strip()
            if reason:
                audit_reason_by_article[aid] = reason

        for a in articles:
            scanned += 1
            kind = str(a.archived_kind or "").strip().lower()
            reason = str(a.archived_reason or "").strip()
            if not reason and kind == "delete":
                reason = audit_reason_by_article.get(int(a.id), "").strip()

            # Keep archived with reason; backfill into training_events.
            if reason:
                decision = "delete" if kind == "delete" else "hide"
                exists = session.scalars(
                    select(TrainingEvent.id)
                    .where(
                        TrainingEvent.article_id == int(a.id),
                        TrainingEvent.decision == decision,
                        TrainingEvent.reason_text == reason,
                    )
                    .limit(1)
                ).first()
                if exists:
                    already_present += 1
                    continue
                try:
                    score = session.get(Score, int(a.id))
                    features = _feature_snapshot(a, score, _guess_reason_tags(reason))
                    ml_meta = predict_editor_choice_prob(features)
                    published_time = a.published_at or a.created_at
                    event_time = a.archived_at or a.updated_at or datetime.utcnow()
                    delay_minutes = None
                    if published_time:
                        try:
                            delay_minutes = max(0, int((event_time - published_time).total_seconds() // 60))
                        except Exception:
                            delay_minutes = None
                    session.add(
                        TrainingEvent(
                            user_id=None,
                            article_id=int(a.id),
                            decision=decision,
                            label=0,
                            hour_bucket=(a.selected_hour_bucket_utc or (published_time or event_time).replace(minute=0, second=0, microsecond=0)),
                            candidate_set_ids=[int(a.id)],
                            features_json=features,
                            reason_text=reason,
                            reason_tags=_guess_reason_tags(reason) or None,
                            rule_score=(float(score.final_score or 0.0) if score else None),
                            ml_score_at_decision=(float(ml_meta.get("prob")) if ml_meta.get("ok") else None),
                            model_version=(ml_meta.get("version") if ml_meta.get("ok") else None),
                            override=False,
                            event_time=event_time,
                            article_published_at=published_time,
                            delay_minutes=delay_minutes,
                            final_outcome=("deleted" if decision == "delete" else "hidden"),
                            created_at=event_time,
                        )
                    )
                    backfilled += 1
                except Exception:
                    errors += 1
                continue

            # No reason: restore back to manual queue.
            a.status = restore_status
            a.archived_kind = None
            a.archived_reason = None
            a.archived_at = None
            a.updated_at = datetime.utcnow()
            restored += 1

    return {
        "ok": True,
        "scanned_archived": scanned,
        "backfilled_training_events": backfilled,
        "already_present": already_present,
        "restored_without_reason": restored,
        "errors": errors,
        "restore_status": str(restore_status.value if isinstance(restore_status, ArticleStatus) else restore_status),
    }
