from __future__ import annotations

import math
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select

from app.api_dependencies import get_current_user, require_roles
from app.db import session_scope
from app.models import (
    Article,
    ArticleEmbedding,
    ArticleStatus,
    ContentVersion,
    DecisionMode,
    EditorFeedback,
    ModelArtifact,
    PublishJob,
    PublishStatus,
    RawPageSnapshot,
    Score,
    SelectionDecision,
    Source,
    SourceHealthMetric,
    User,
    UserRole,
)
from app.services.audit import audit
from app.services.auth import create_access_token, verify_password
from app.services.auto_decision import decide_and_maybe_publish
from app.services.preference import (
    build_ranking_dataset,
    detect_preference_drift,
    rebuild_preference_profile,
    save_selection_decision,
    train_ranking_model,
)


router = APIRouter(prefix="/v1", tags=["v1"])


class LoginIn(BaseModel):
    email: str
    password: str


class StructuredFeedbackIn(BaseModel):
    explanation_text: str = Field(min_length=5, max_length=5000)
    reason_codes: list[str] = Field(default_factory=list)
    confidence: int = Field(ge=1, le=10)
    liked_aspects: str = ""
    disliked_aspects: str = ""


class BulkStatusIn(BaseModel):
    article_ids: list[int] = Field(min_length=1, max_length=300)
    status: str


class DecisionIn(BaseModel):
    chosen_article_id: int
    rejected_article_ids: list[int] = Field(default_factory=list)
    confidence: float | None = None


class TrainIn(BaseModel):
    days: int = Field(default=14, ge=3, le=180)


@router.post("/auth/login")
def login(body: LoginIn) -> dict:
    with session_scope() as session:
        user = session.scalars(select(User).where(User.email == body.email, User.is_active.is_(True))).first()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="invalid_credentials")
    token = create_access_token(user)
    return {"access_token": token, "token_type": "bearer", "role": user.role.value}


@router.get("/me")
def me(user: User = Depends(get_current_user)) -> dict:
    return {"id": user.id, "email": user.email, "role": user.role.value}


@router.get("/articles")
def list_articles(
    page: int = 1,
    page_size: int = 20,
    q: str | None = None,
    status: str | None = None,
    user: User = Depends(get_current_user),
) -> dict:
    page = max(1, page)
    page_size = max(1, min(100, page_size))

    with session_scope() as session:
        query = select(Article)
        count_query = select(func.count(Article.id))

        if status:
            query = query.where(Article.status == status)
            count_query = count_query.where(Article.status == status)

        if q:
            pattern = f"%{q}%"
            cond = or_(Article.title.ilike(pattern), Article.subtitle.ilike(pattern), Article.ru_title.ilike(pattern))
            query = query.where(cond)
            count_query = count_query.where(cond)

        total = int(session.scalar(count_query) or 0)
        rows = session.scalars(
            query.order_by(Article.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
        ).all()

        items = []
        for a in rows:
            s = session.get(Score, a.id)
            items.append(
                {
                    "id": a.id,
                    "status": a.status,
                    "double_of_article_id": a.double_of_article_id,
                    "title": a.title,
                    "ru_title": a.ru_title,
                    "source_id": a.source_id,
                    "published_at": a.published_at,
                    "score": s.final_score if s else None,
                    "canonical_url": a.canonical_url,
                }
            )

    audit("list_articles", "article", "*", {"page": page, "page_size": page_size, "q": q, "status": status}, user.id)
    return {"items": items, "page": page, "page_size": page_size, "total": total}


@router.post("/articles/bulk/status")
def bulk_status(body: BulkStatusIn, user: User = Depends(require_roles(UserRole.ADMIN, UserRole.EDITOR))) -> dict:
    try:
        status = ArticleStatus(body.status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid_status") from exc

    updated = 0
    with session_scope() as session:
        for aid in body.article_ids:
            article = session.get(Article, aid)
            if not article:
                continue
            article.status = status
            article.updated_at = datetime.utcnow()
            updated += 1

    audit("bulk_status", "article", "bulk", {"updated": updated, "status": status.value}, user.id)
    return {"ok": True, "updated": updated}


@router.get("/articles/{article_id}/score-breakdown")
def score_breakdown(article_id: int, user: User = Depends(get_current_user)) -> dict:
    with session_scope() as session:
        score = session.get(Score, article_id)
        if not score:
            raise HTTPException(status_code=404, detail="score_not_found")
    return {
        "significance": score.significance,
        "freshness": score.freshness,
        "relevance": score.relevance,
        "virality": score.virality,
        "uniqueness": score.uniqueness,
        "source_trust": score.source_trust,
        "longevity": score.longevity,
        "scale": score.scale,
        "final_score": score.final_score,
        "reasoning": score.reasoning,
        "features": score.features,
        "uncertainty": score.uncertainty,
    }


@router.get("/articles/{article_id}/neighbors")
def similarity_neighbors(article_id: int, top_k: int = 5, user: User = Depends(get_current_user)) -> list[dict]:
    top_k = max(1, min(30, top_k))
    with session_scope() as session:
        emb = session.get(ArticleEmbedding, article_id)
        if not emb:
            return []
        rows = session.execute(
            select(ArticleEmbedding, Article)
            .join(Article, ArticleEmbedding.article_id == Article.id)
            .where(Article.id != article_id)
            .order_by(ArticleEmbedding.embedding.cosine_distance(emb.embedding))
            .limit(top_k)
        ).all()

    out = []
    for e, a in rows:
        out.append({"article_id": a.id, "title": a.title, "status": a.status, "canonical_url": a.canonical_url})
    return out


@router.get("/articles/{article_id}/versions")
def versions(article_id: int, user: User = Depends(get_current_user)) -> list[dict]:
    with session_scope() as session:
        rows = session.scalars(
            select(ContentVersion).where(ContentVersion.article_id == article_id).order_by(ContentVersion.version_no.desc())
        ).all()
    return [
        {
            "id": v.id,
            "version_no": v.version_no,
            "ru_title": v.ru_title,
            "ru_summary": v.ru_summary,
            "short_hook": v.short_hook,
            "image_path": v.image_path,
            "quality_report": v.quality_report,
            "selected_by_editor": v.selected_by_editor,
            "created_at": v.created_at,
        }
        for v in rows
    ]


@router.post("/articles/{article_id}/feedback")
def add_structured_feedback(article_id: int, body: StructuredFeedbackIn, user: User = Depends(require_roles(UserRole.ADMIN, UserRole.EDITOR, UserRole.REVIEWER))) -> dict:
    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            raise HTTPException(status_code=404, detail="article_not_found")
        session.add(
            EditorFeedback(
                article_id=article_id,
                explanation_text=body.explanation_text,
                reason_codes=body.reason_codes,
                confidence=body.confidence,
                liked_aspects=body.liked_aspects,
                disliked_aspects=body.disliked_aspects,
            )
        )

    audit("structured_feedback", "article", str(article_id), body.model_dump(), user.id)
    return {"ok": True}


@router.post("/decisions")
def log_decision(body: DecisionIn, user: User = Depends(require_roles(UserRole.ADMIN, UserRole.EDITOR))) -> dict:
    did = save_selection_decision(
        chosen_article_id=body.chosen_article_id,
        rejected_article_ids=body.rejected_article_ids,
        decision_mode=DecisionMode.MANUAL,
        confidence=body.confidence,
        candidates=None,
    )
    audit("selection_decision", "article", str(body.chosen_article_id), body.model_dump(), user.id)
    return {"ok": True, "decision_id": did}


@router.post("/trainer/build")
def trainer_build(body: TrainIn, user: User = Depends(require_roles(UserRole.ADMIN, UserRole.EDITOR))) -> dict:
    out = build_ranking_dataset(days=body.days)
    audit("trainer_build", "ranking_examples", "batch", out, user.id)
    return out


@router.post("/trainer/train/{batch_id}")
def trainer_train(batch_id: str, user: User = Depends(require_roles(UserRole.ADMIN))) -> dict:
    out = train_ranking_model(batch_id=batch_id)
    audit("trainer_train", "model", batch_id, out, user.id)
    return out


@router.post("/trainer/drift")
def trainer_drift(user: User = Depends(require_roles(UserRole.ADMIN, UserRole.EDITOR))) -> dict:
    out = detect_preference_drift()
    audit("trainer_drift", "drift", "latest", out, user.id)
    return out


@router.post("/decision/auto")
def auto_decision(user: User = Depends(require_roles(UserRole.ADMIN, UserRole.EDITOR))) -> dict:
    out = decide_and_maybe_publish(top_n=5)
    audit("auto_decision", "article", str(out.get("article_id") or out.get("candidate_article_id") or "none"), out, user.id)
    return out


@router.get("/analytics/overview")
def analytics_overview(user: User = Depends(require_roles(UserRole.ADMIN, UserRole.EDITOR, UserRole.REVIEWER))) -> dict:
    with session_scope() as session:
        published = int(session.scalar(select(func.count(Article.id)).where(Article.status == ArticleStatus.PUBLISHED)) or 0)
        ready = int(session.scalar(select(func.count(Article.id)).where(Article.status == ArticleStatus.READY)) or 0)
        manual_feedback = int(session.scalar(select(func.count(EditorFeedback.id))) or 0)
        auto_models = session.scalars(
            select(ModelArtifact).where(ModelArtifact.name == "ranking", ModelArtifact.active.is_(True)).limit(1)
        ).all()
        latest_health = session.scalars(
            select(SourceHealthMetric).order_by(SourceHealthMetric.id.desc()).limit(20)
        ).all()
        publish_success = int(session.scalar(select(func.count(PublishJob.id)).where(PublishJob.status == PublishStatus.SUCCESS)) or 0)
        publish_total = int(session.scalar(select(func.count(PublishJob.id))) or 0)
        decisions = session.scalars(select(SelectionDecision).order_by(SelectionDecision.id.desc()).limit(300)).all()
        chosen_ids = [int(d.chosen_article_id) for d in decisions]
        score_by_article = {}
        if chosen_ids:
            for s in session.scalars(select(Score).where(Score.article_id.in_(chosen_ids))).all():
                score_by_article[s.article_id] = s

    avg_success_rate = (
        sum(float(x.success_rate) for x in latest_health) / len(latest_health) if latest_health else 0.0
    )
    precision_hits = 0
    ndcg_sum = 0.0
    override_count = 0
    considered = 0
    for d in decisions:
        candidates = d.candidates or []
        if not candidates:
            continue
        considered += 1
        ranked = sorted(candidates, key=lambda x: float(x.get("score", x.get("prob", 0.0))), reverse=True)
        top_id = int(ranked[0]["article_id"]) if ranked else None
        if top_id == d.chosen_article_id:
            precision_hits += 1
            ndcg_sum += 1.0
        else:
            rank_pos = next((i + 1 for i, x in enumerate(ranked) if int(x["article_id"]) == d.chosen_article_id), None)
            if rank_pos:
                ndcg_sum += 1.0 / math.log2(rank_pos + 1)
        if d.decision_mode == DecisionMode.MANUAL:
            override_count += 1

    novelty_values = []
    for d in decisions:
        s = score_by_article.get(int(d.chosen_article_id))
        if s and isinstance(s.features, dict):
            n = s.features.get("novelty")
            if isinstance(n, (int, float)):
                novelty_values.append(float(n))
    return {
        "published": published,
        "ready": ready,
        "feedback_count": manual_feedback,
        "active_model": auto_models[0].version if auto_models else None,
        "source_health_avg_success": avg_success_rate,
        "publish_success_rate": (publish_success / publish_total) if publish_total else 0.0,
        "precision_at_1": (precision_hits / considered) if considered else None,
        "ndcg_at_5": (ndcg_sum / considered) if considered else None,
        "human_override_rate": (override_count / considered) if considered else None,
        "avg_novelty_selected": (sum(novelty_values) / len(novelty_values)) if novelty_values else None,
        "ctr": None,
        "engagement_rate": None,
    }


@router.get("/articles/{article_id}/source-diff")
def source_diff(article_id: int, user: User = Depends(get_current_user)) -> dict:
    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            raise HTTPException(status_code=404, detail="article_not_found")
        peers = session.scalars(
            select(Article)
            .where(Article.cluster_key == article.cluster_key, Article.id != article.id)
            .order_by(Article.created_at.desc())
            .limit(5)
        ).all()

    return {
        "article_id": article.id,
        "cluster_key": article.cluster_key,
        "source_title": article.title,
        "peers": [{"id": p.id, "title": p.title, "source_id": p.source_id, "url": p.canonical_url} for p in peers],
    }


@router.get("/sources/health")
def source_health(user: User = Depends(get_current_user)) -> list[dict]:
    with session_scope() as session:
        rows = session.execute(
            select(SourceHealthMetric, Source)
            .join(Source, SourceHealthMetric.source_id == Source.id)
            .order_by(SourceHealthMetric.id.desc())
            .limit(100)
        ).all()
    return [
        {
            "source": s.name,
            "success_rate": m.success_rate,
            "avg_latency_ms": m.avg_latency_ms,
            "parse_quality_avg": m.parse_quality_avg,
            "stale_minutes": m.stale_minutes,
            "last_error": m.last_error,
            "created_at": m.created_at,
        }
        for m, s in rows
    ]


@router.get("/articles/{article_id}/snapshots")
def snapshots(article_id: int, user: User = Depends(get_current_user)) -> list[dict]:
    with session_scope() as session:
        rows = session.scalars(
            select(RawPageSnapshot).where(RawPageSnapshot.article_id == article_id).order_by(RawPageSnapshot.id.desc())
        ).all()
    return [
        {
            "id": r.id,
            "url": r.url,
            "final_url": r.final_url,
            "status_code": r.status_code,
            "latency_ms": r.latency_ms,
            "parse_quality": r.parse_quality,
            "fetched_at": r.fetched_at,
        }
        for r in rows
    ]
