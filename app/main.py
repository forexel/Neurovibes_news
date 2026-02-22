from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from html import escape
from zoneinfo import ZoneInfo

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.core.config import settings
from app.db import init_db, session_scope
from app.models import (
    Article,
    ArticleEmbedding,
    ArticleStatus,
    ContentVersion,
    DailySelection,
    EditorFeedback,
    PublishJob,
    RawPageSnapshot,
    Score,
    ScoreParameter,
    Source,
    LLMUsageLog,
    TelegramBotKV,
    User,
    UserWorkspace,
)
from app.api_v1 import router as v1_router
from app.services.bootstrap import seed_sources
from app.services.content_generation import (
    generate_image_card,
    generate_image_prompt,
    generate_ru_summary,
    translate_article_full_style,
    translate_article_text,
)
from app.services.object_storage import upload_generated_image
from app.services.pipeline import auto_select_by_profile, pick_hourly_top, run_hourly_cycle
from app.services.ingestion import (
    enrich_article_from_source,
    enrich_summary_only_articles,
    geo_check_sources,
    run_ingestion,
    run_ingestion_fast,
)
from app.services.embedding_dedup import process_embeddings_and_dedup
from app.services.scoring import prune_bad_articles, prune_non_ai_articles, run_scoring, score_article_by_id
from app.services.topic_filter import passes_ai_topic_filter
from app.services.preference import rebuild_preference_profile
from app.services.telegram_publisher import publish_article, send_test_message
from app.services.telegram_review import (
    poll_review_updates,
    send_hourly_backfill_for_review,
    send_hourly_top_for_review,
    send_selected_backlog_for_review,
)
from app.services.audit import audit
from app.services.auth import create_access_token, decode_token, hash_password, verify_password
from app.services.llm import get_client, get_workspace_api_key, set_user_api_key, track_usage_from_response
from app.services.user_secrets import encrypt_secret
from app.services.telegram_context import load_workspace_telegram_context
from app.services.runtime_settings import (
    RUNTIME_DEFAULTS,
    delete_runtime_setting,
    get_runtime_float,
    list_runtime_settings,
    upsert_runtime_setting,
)

app = FastAPI(title="Neurovibes News API", version="0.3.0")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.include_router(v1_router)

SCORING_JOBS: dict[str, dict] = {}
SCORING_LOCK = threading.Lock()
ENRICH_JOBS: dict[str, dict] = {}
ENRICH_LOCK = threading.Lock()
PRUNE_JOBS: dict[str, dict] = {}
PRUNE_LOCK = threading.Lock()
PIPELINE_JOBS: dict[str, dict] = {}
PIPELINE_LOCK = threading.Lock()


def _get_session_user(request: Request) -> User | None:
    token = (request.cookies.get("nv_session") or "").strip()
    if not token:
        return None
    try:
        payload = decode_token(token)
        user_id = int(payload.get("sub"))
    except Exception:
        return None
    with session_scope() as session:
        return session.scalars(select(User).where(User.id == user_id, User.is_active.is_(True))).first()


@app.middleware("http")
async def _user_llm_key_middleware(request: Request, call_next):
    user = _get_session_user(request)
    if user is None:
        set_user_api_key(None)
        load_workspace_telegram_context(None)
        return await call_next(request)
    # Load API key once per request and set it for get_client().
    set_user_api_key(get_workspace_api_key(user.id))
    load_workspace_telegram_context(user.id)
    return await call_next(request)


def _require_session_user(request: Request) -> User:
    user = _get_session_user(request)
    if user is None:
        raise HTTPException(status_code=401, detail="auth_required")
    return user


class FeedbackIn(BaseModel):
    explanation_text: str = Field(min_length=5, max_length=5000)


class RunPipelineIn(BaseModel):
    backfill_days: int = Field(default=1, ge=1, le=60)


class StatusIn(BaseModel):
    status: str


class AggregateIn(BaseModel):
    period: str = Field(pattern="^(hour|day|week|month)$")


class RunScoringIn(BaseModel):
    limit: int = Field(default=300, ge=1, le=5000)

@app.post("/articles/{article_id}/score")
def score_single(article_id: int) -> dict:
    out = score_article_by_id(article_id)
    if not out.get("ok"):
        if out.get("error") == "article_not_found":
            raise HTTPException(status_code=404, detail="Article not found")
        raise HTTPException(status_code=400, detail=str(out.get("error") or "score_failed"))
    return out

class EnrichFullTextIn(BaseModel):
    days_back: int = Field(default=30, ge=1, le=365)
    limit: int = Field(default=2000, ge=1, le=50000)

class PruneIn(BaseModel):
    days_back: int = Field(default=30, ge=1, le=365)
    limit: int = Field(default=50000, ge=1, le=500000)
    archive_summary_only: bool = True
    archive_non_ai: bool = True
    archive_low_relevance: bool = True


class ImagePromptIn(BaseModel):
    prompt: str = Field(min_length=10, max_length=4000)


class DeleteIn(BaseModel):
    reason: str = Field(min_length=5, max_length=2000)


class TextOverrideIn(BaseModel):
    text: str = Field(min_length=50, max_length=200000)


class RuEditIn(BaseModel):
    ru_title: str = Field(min_length=1, max_length=300)
    ru_summary: str = Field(min_length=10, max_length=20000)


class SchedulePublishIn(BaseModel):
    publish_at: str | None = None


class SourceActiveIn(BaseModel):
    is_active: bool


class SourceAddIn(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    rss_url: str = Field(min_length=8, max_length=1024)
    priority_rank: int = Field(default=50, ge=1, le=999)
    kind: str = Field(default="rss", pattern="^(rss|html)$")


class SourceUpdateIn(BaseModel):
    name: str = Field(min_length=2, max_length=255)
    rss_url: str = Field(min_length=8, max_length=1024)
    priority_rank: int = Field(default=50, ge=1, le=999)
    kind: str = Field(default="rss", pattern="^(rss|html)$")
    is_active: bool | None = None


class ScoreParamUpsertIn(BaseModel):
    key: str = Field(min_length=2, max_length=64)
    title: str = Field(min_length=2, max_length=128)
    description: str = Field(default="", max_length=4000)
    weight: float = Field(ge=0.0, le=1.0)
    influence_rule: str = Field(default="", max_length=4000)
    is_active: bool = True


class RuntimeSettingUpsertIn(BaseModel):
    key: str = Field(min_length=2, max_length=128)
    value: str = Field(default="", max_length=12000)
    scope: str = Field(default="global", pattern="^(global|topic)$")
    topic_key: str | None = Field(default=None, max_length=128)


class SetupStep1In(BaseModel):
    channel_name: str = Field(min_length=2, max_length=255)
    channel_theme: str = Field(min_length=10, max_length=6000)
    # Sources are managed in /sources. Keep this optional for backward compatibility.
    sources_text: str = Field(default="", max_length=20000)
    openrouter_api_key: str | None = Field(default=None, max_length=400)


class SetupStep2In(BaseModel):
    audience_description: str = Field(min_length=10, max_length=6000)


class SetupTelegramIn(BaseModel):
    telegram_bot_token: str | None = Field(default=None, max_length=512)
    telegram_review_chat_id: str = Field(default="", max_length=255)
    telegram_channel_id: str = Field(default="", max_length=255)
    telegram_signature: str = Field(default="", max_length=255)
    timezone_name: str = Field(default="Europe/Moscow", max_length=64)

@app.on_event("startup")
def on_startup() -> None:
    init_db()
    seed_sources()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/login", response_class=HTMLResponse)
def login_page() -> str:
    return """
<!doctype html>
<html lang='ru'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>Login</title>
  <link rel="stylesheet" href="/static/app.css?v=3">
</head>
<body class="auth">
  <form class='card' method='post' action='/login'>
    <h2>Sign In</h2>
    <input name='login' type='text' placeholder='Login' required />
    <input name='password' type='password' placeholder='Password' required />
    <button type='submit'>Login</button>
    <p>No account? <a href='/register'>Registration</a></p>
  </form>
</body>
</html>
"""


@app.post("/login")
def login_submit(login: str = Form(...), password: str = Form(...)) -> RedirectResponse:
    email = (login or "").strip().lower()
    with session_scope() as session:
        user = session.scalars(select(User).where(User.email == email, User.is_active.is_(True))).first()
    if not user or not verify_password(password, user.password_hash):
        return RedirectResponse(url="/login", status_code=303)
    token = create_access_token(user)
    resp = RedirectResponse(url="/", status_code=303)
    resp.set_cookie("nv_session", token, httponly=True, samesite="lax", max_age=60 * 60 * 24 * 7)
    return resp


@app.get("/register", response_class=HTMLResponse)
def register_page() -> str:
    return """
<!doctype html>
<html lang='ru'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>Create User</title>
  <link rel="stylesheet" href="/static/app.css?v=3">
</head>
<body class="auth">
  <form class='card' method='post' action='/register'>
    <h2>Sign Up</h2>
    <input name='login' type='text' placeholder='Login' required />
    <input name='password' type='password' placeholder='Password (min 6)' minlength='6' required />
    <button type='submit'>Sign Up</button>
    <p>Already have account? <a href='/login'>Login</a></p>
  </form>
</body>
</html>
"""


@app.post("/register")
def register_submit(login: str = Form(...), password: str = Form(...)) -> RedirectResponse:
    email = (login or "").strip().lower()
    email = email.strip().lower()
    if len(password or "") < 6:
        return RedirectResponse(url="/register", status_code=303)
    with session_scope() as session:
        exists = session.scalars(select(User).where(User.email == email)).first()
        if exists:
            return RedirectResponse(url="/login", status_code=303)
        user = User(email=email, password_hash=hash_password(password), is_active=True)
        session.add(user)
        session.flush()
        session.add(UserWorkspace(user_id=user.id, onboarding_step=1, onboarding_completed=False))
    return RedirectResponse(url="/login", status_code=303)


@app.get("/logout")
def logout() -> RedirectResponse:
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie("nv_session")
    return resp


@app.get("/config")
def config() -> dict[str, str]:
    return {
        "app_env": settings.app_env,
        "text_model": settings.llm_text_model,
        "image_model": settings.llm_image_model,
        "embedding_model": settings.embedding_model,
        "llm_base_url": settings.openrouter_base_url,
        "llm_api_key_set": "true" if bool(settings.openrouter_api_key) else "false",
    }


@app.post("/pipeline/run")
def pipeline_run(body: RunPipelineIn) -> dict:
    return run_hourly_cycle(backfill_days=body.backfill_days)


@app.post("/pipeline/start")
def pipeline_start(body: RunPipelineIn, request: Request) -> dict:
    _require_session_user(request)
    job_id = uuid.uuid4().hex
    with PIPELINE_LOCK:
        PIPELINE_JOBS[job_id] = {
            "job_id": job_id,
            "status": "running",
            "stage": "starting",
            "stage_detail": None,
            "processed": 0,
            "total": 0,
            "started_at": datetime.utcnow().isoformat(),
            "finished_at": None,
            "error": None,
            "result": None,
        }

    def _set_state(stage: str, processed: int | None = None, total: int | None = None, detail: str | None = None) -> None:
        with PIPELINE_LOCK:
            job = PIPELINE_JOBS.get(job_id)
            if job:
                job["stage"] = stage
                if processed is not None:
                    job["processed"] = int(processed)
                if total is not None:
                    job["total"] = int(total)
                if detail is not None:
                    job["stage_detail"] = detail

    def _run() -> None:
        try:
            logger = logging.getLogger("nv.pipeline")

            logger.info("pipeline geo check start")
            def _geo_progress(i: int, total: int, name: str) -> None:
                _set_state("geo/check", processed=i, total=total, detail=name)
                if total and (i == total or i % 5 == 0):
                    logger.info("pipeline geo check %s/%s (%s)", i, total, name)

            geo = geo_check_sources(timeout_s=12, progress_cb=_geo_progress)

            read_total = 0
            read_done = 0
            save_done = 0
            last_read: dict[str, int] = {}
            last_save: dict[str, int] = {}
            last_log_read = 0
            last_log_save = 0

            def _ing_progress(phase: str, cur: int, total: int, source_name: str) -> None:
                nonlocal read_total, read_done, save_done, last_log_read, last_log_save
                if phase == "read":
                    if cur == 0:
                        read_total += int(total or 0)
                    prev = last_read.get(source_name, 0)
                    delta = max(0, int(cur) - int(prev))
                    last_read[source_name] = int(cur)
                    read_done += delta
                    _set_state("ingestion/read", processed=read_done, total=max(read_total, 1), detail=source_name)
                    if read_done == read_total or (read_done - last_log_read) >= 25:
                        last_log_read = read_done
                        logger.info("pipeline ingestion read %s/%s (%s)", read_done, read_total, source_name)
                elif phase == "save":
                    prev = last_save.get(source_name, 0)
                    delta = max(0, int(cur) - int(prev))
                    last_save[source_name] = int(cur)
                    save_done += delta
                    _set_state("ingestion/save", processed=save_done, total=max(read_total, 1), detail=source_name)
                    if save_done == read_total or (save_done - last_log_save) >= 10:
                        last_log_save = save_done
                        logger.info("pipeline ingestion saved %s/%s (%s)", save_done, read_total, source_name)

            logger.info("pipeline start backfill_days=%s", body.backfill_days)
            ingest = run_ingestion_fast(
                days_back=body.backfill_days,
                max_entries=200,
                status_cb=lambda s: _set_state("ingestion/source", detail=str(s)),
                progress_cb=_ing_progress,
            )

            logger.info("pipeline enrich start")
            def _enrich_progress(processed: int, total: int) -> None:
                _set_state("enrich/full_text", processed=processed, total=total)
                if total and (processed == total or processed % 25 == 0):
                    logger.info("pipeline enrich %s/%s", processed, total)

            enrich = enrich_summary_only_articles(limit=300, days_back=30, progress_cb=_enrich_progress)

            logger.info("pipeline dedup start")
            _set_state("dedup/embeddings", processed=0, total=1)
            embedded = process_embeddings_and_dedup(limit=300)
            _set_state("dedup/embeddings", processed=1, total=1)

            logger.info("pipeline scoring start")
            def _score_progress(processed: int, total: int) -> None:
                _set_state("scoring", processed=processed, total=total)
                if total and (processed == total or processed % 25 == 0):
                    logger.info("pipeline scoring %s/%s", processed, total)

            def _ru_progress(processed: int, total: int) -> None:
                _set_state("translate/preview", processed=processed, total=total)
                if total and (processed == total or processed % 10 == 0):
                    logger.info("pipeline translate preview %s/%s", processed, total)

            scored = run_scoring(limit=300, progress_cb=_score_progress, ru_progress_cb=_ru_progress)

            logger.info("pipeline pick hourly top")
            _set_state("pick/hourly_top", processed=0, total=1)
            top_id = pick_hourly_top()
            _set_state("pick/hourly_top", processed=1, total=1)

            if top_id:
                logger.info("pipeline prepare ru article_id=%s", top_id)
                _set_state("prepare/ru_post", processed=0, total=1, detail=str(top_id))
                generate_ru_summary(int(top_id))
                _set_state("prepare/ru_post", processed=1, total=1, detail=str(top_id))
                logger.info("pipeline prepare image article_id=%s", top_id)
                _set_state("prepare/image", processed=0, total=1, detail=str(top_id))
                generate_image_card(int(top_id))
                _set_state("prepare/image", processed=1, total=1, detail=str(top_id))

            result = {
                "geo_check": geo,
                "ingestion": ingest,
                "enrich_summary_only": enrich,
                "embedded": embedded,
                "scored": scored,
                "top_article_id": top_id,
            }
            with PIPELINE_LOCK:
                job = PIPELINE_JOBS.get(job_id)
                if job:
                    job["status"] = "done"
                    job["stage"] = "done"
                    job["stage_detail"] = None
                    job["result"] = result
                    job["finished_at"] = datetime.utcnow().isoformat()
        except Exception as exc:
            try:
                logging.getLogger("nv.pipeline").exception("pipeline failed")
            except Exception:
                pass
            with PIPELINE_LOCK:
                job = PIPELINE_JOBS.get(job_id)
                if job:
                    job["status"] = "error"
                    job["error"] = str(exc)
                    job["finished_at"] = datetime.utcnow().isoformat()

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "job_id": job_id}


@app.get("/pipeline/jobs/{job_id}")
def pipeline_job_status(job_id: str, request: Request) -> dict:
    _require_session_user(request)
    with PIPELINE_LOCK:
        job = PIPELINE_JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job_not_found")
        return dict(job)


@app.post("/scoring/run")
def scoring_run(body: RunScoringIn) -> dict:
    processed = run_scoring(limit=body.limit)
    top_id = pick_hourly_top()
    return {"ok": True, "scored": processed, "top_article_id": top_id}


@app.post("/scoring/prune-non-ai")
def scoring_prune_non_ai(body: RunScoringIn) -> dict:
    pruned = prune_non_ai_articles(limit=body.limit)
    return {"ok": True, "archived_non_ai": pruned}


@app.post("/scoring/start")
def scoring_start(body: RunScoringIn) -> dict:
    user = _get_session_user(Request)  # placeholder
    job_id = uuid.uuid4().hex
    with SCORING_LOCK:
        SCORING_JOBS[job_id] = {
            "job_id": job_id,
            "status": "running",
            "processed": 0,
            "total": 0,
            "started_at": datetime.utcnow().isoformat(),
            "finished_at": None,
            "error": None,
        }

    def _progress(processed: int, total: int) -> None:
        with SCORING_LOCK:
            job = SCORING_JOBS.get(job_id)
            if not job:
                return
            job["processed"] = int(processed)
            job["total"] = int(total)

    def _run() -> None:
        try:
            scored = run_scoring(limit=body.limit, progress_cb=_progress)
            top_id = pick_hourly_top()
            with SCORING_LOCK:
                job = SCORING_JOBS.get(job_id)
                if job:
                    job["status"] = "done"
                    job["scored"] = int(scored)
                    job["top_article_id"] = top_id
                    job["finished_at"] = datetime.utcnow().isoformat()
        except Exception as exc:
            with SCORING_LOCK:
                job = SCORING_JOBS.get(job_id)
                if job:
                    job["status"] = "error"
                    job["error"] = str(exc)
                    job["finished_at"] = datetime.utcnow().isoformat()

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "job_id": job_id}


@app.get("/scoring/jobs/{job_id}")
def scoring_job_status(job_id: str) -> dict:
    with SCORING_LOCK:
        job = SCORING_JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job_not_found")
        return dict(job)


@app.post("/content/enrich/start")
def content_enrich_start(body: EnrichFullTextIn) -> dict:
    job_id = uuid.uuid4().hex
    with ENRICH_LOCK:
        ENRICH_JOBS[job_id] = {
            "job_id": job_id,
            "status": "running",
            "processed": 0,
            "total": 0,
            "upgraded_to_full": 0,
            "blocked": 0,
            "paywalled_or_thin": 0,
            "too_short": 0,
            "other": 0,
            "last_article_id": None,
            "last_reason": None,
            "started_at": datetime.utcnow().isoformat(),
            "finished_at": None,
            "error": None,
        }

    min_dt = datetime.utcnow() - timedelta(days=body.days_back)
    with session_scope() as session:
        ids = [
            int(x)
            for x in session.scalars(
                select(Article.id)
                .where(
                    Article.content_mode == "summary_only",
                    Article.created_at >= min_dt,
                    Article.status != ArticleStatus.DOUBLE,
                    Article.status != ArticleStatus.ARCHIVED,
                )
                .order_by(Article.created_at.desc())
                .limit(body.limit)
            ).all()
        ]

    with ENRICH_LOCK:
        job = ENRICH_JOBS.get(job_id)
        if job:
            job["total"] = int(len(ids))

    def _run() -> None:
        try:
            upgraded = 0
            blocked = 0
            paywalled = 0
            too_short = 0
            other = 0

            total = len(ids)
            for i, article_id in enumerate(ids, start=1):
                out = enrich_article_from_source(article_id)
                reason = out.get("reason")
                if out.get("updated"):
                    upgraded += 1
                else:
                    if isinstance(reason, str) and reason.startswith("blocked_http_"):
                        blocked += 1
                    elif out.get("paywalled_or_thin") or reason == "paywalled_or_thin":
                        paywalled += 1
                    elif reason == "extracted_too_short":
                        too_short += 1
                    else:
                        other += 1

                with ENRICH_LOCK:
                    job = ENRICH_JOBS.get(job_id)
                    if not job:
                        continue
                    job["processed"] = int(i)
                    job["total"] = int(total)
                    job["upgraded_to_full"] = int(upgraded)
                    job["blocked"] = int(blocked)
                    job["paywalled_or_thin"] = int(paywalled)
                    job["too_short"] = int(too_short)
                    job["other"] = int(other)
                    job["last_article_id"] = int(article_id)
                    job["last_reason"] = str(reason) if reason is not None else None

            with ENRICH_LOCK:
                job = ENRICH_JOBS.get(job_id)
                if job:
                    job["status"] = "done"
                    job["finished_at"] = datetime.utcnow().isoformat()
        except Exception as exc:
            with ENRICH_LOCK:
                job = ENRICH_JOBS.get(job_id)
                if job:
                    job["status"] = "error"
                    job["error"] = str(exc)
                    job["finished_at"] = datetime.utcnow().isoformat()

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "job_id": job_id, "total": len(ids)}


@app.get("/content/enrich/jobs/{job_id}")
def content_enrich_job_status(job_id: str) -> dict:
    with ENRICH_LOCK:
        job = ENRICH_JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job_not_found")
        return dict(job)


@app.post("/prune/start")
def prune_start(body: PruneIn) -> dict:
    job_id = uuid.uuid4().hex
    with PRUNE_LOCK:
        PRUNE_JOBS[job_id] = {
            "job_id": job_id,
            "status": "running",
            "processed": 0,
            "total": 0,
            "archived": 0,
            "kept": 0,
            "summary_only": 0,
            "non_ai": 0,
            "low_relevance": 0,
            "skipped_published_or_selected": 0,
            "last_article_id": None,
            "last_reason": None,
            "started_at": datetime.utcnow().isoformat(),
            "finished_at": None,
            "error": None,
        }

    min_dt = datetime.utcnow() - timedelta(days=body.days_back)
    with session_scope() as session:
        ids = [
            int(x)
            for x in session.scalars(
                select(Article.id)
                .where(
                    Article.created_at >= min_dt,
                    Article.status != ArticleStatus.ARCHIVED,
                    Article.status != ArticleStatus.DOUBLE,
                )
                .order_by(Article.created_at.desc())
                .limit(body.limit)
            ).all()
        ]

    with PRUNE_LOCK:
        job = PRUNE_JOBS.get(job_id)
        if job:
            job["total"] = int(len(ids))

    def _run() -> None:
        try:
            archived = 0
            kept = 0
            c_summary = 0
            c_non_ai = 0
            c_low_rel = 0
            c_skipped = 0
            total = len(ids)

            for i, article_id in enumerate(ids, start=1):
                reason = None
                did_archive = False

                with session_scope() as session:
                    article = session.get(Article, article_id)
                    if not article:
                        continue

                    # Never auto-hide already published or hourly-selected content.
                    if article.status in {ArticleStatus.PUBLISHED, ArticleStatus.SELECTED_HOURLY}:
                        c_skipped += 1
                    else:
                        if body.archive_summary_only and (article.content_mode or "summary_only") == "summary_only":
                            article.status = ArticleStatus.ARCHIVED
                            article.updated_at = datetime.utcnow()
                            did_archive = True
                            reason = "summary_only"
                            c_summary += 1
                        else:
                            if body.archive_non_ai and not passes_ai_topic_filter(
                                title=article.title or "",
                                subtitle=article.subtitle or "",
                                text=article.text or "",
                                tags=article.tags or [],
                            ):
                                article.status = ArticleStatus.ARCHIVED
                                article.updated_at = datetime.utcnow()
                                did_archive = True
                                reason = "non_ai"
                                c_non_ai += 1
                            elif body.archive_low_relevance:
                                score = session.get(Score, article_id)
                                if score is not None and float(score.relevance or 0.0) < get_runtime_float("min_relevance_for_content", default=7.0):
                                    article.status = ArticleStatus.ARCHIVED
                                    article.updated_at = datetime.utcnow()
                                    did_archive = True
                                    reason = "low_relevance"
                                    c_low_rel += 1

                if did_archive:
                    archived += 1
                else:
                    kept += 1

                with PRUNE_LOCK:
                    job = PRUNE_JOBS.get(job_id)
                    if not job:
                        continue
                    job["processed"] = int(i)
                    job["total"] = int(total)
                    job["archived"] = int(archived)
                    job["kept"] = int(kept)
                    job["summary_only"] = int(c_summary)
                    job["non_ai"] = int(c_non_ai)
                    job["low_relevance"] = int(c_low_rel)
                    job["skipped_published_or_selected"] = int(c_skipped)
                    job["last_article_id"] = int(article_id)
                    job["last_reason"] = reason

            with PRUNE_LOCK:
                job = PRUNE_JOBS.get(job_id)
                if job:
                    job["status"] = "done"
                    job["finished_at"] = datetime.utcnow().isoformat()
        except Exception as exc:
            with PRUNE_LOCK:
                job = PRUNE_JOBS.get(job_id)
                if job:
                    job["status"] = "error"
                    job["error"] = str(exc)
                    job["finished_at"] = datetime.utcnow().isoformat()

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "job_id": job_id, "total": len(ids)}


@app.get("/prune/jobs/{job_id}")
def prune_job_status(job_id: str) -> dict:
    with PRUNE_LOCK:
        job = PRUNE_JOBS.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="job_not_found")
        return dict(job)


def _latest_image_prompt(session, article_id: int) -> str:
    row = session.scalars(
        select(ContentVersion)
        .where(
            ContentVersion.article_id == article_id,
            ContentVersion.image_prompt.is_not(None),
            ContentVersion.image_prompt != "",
        )
        .order_by(ContentVersion.version_no.desc())
        .limit(1)
    ).first()
    return (row.image_prompt or "") if row else ""


def _save_image_prompt_version(session, article: Article, prompt: str) -> None:
    version_no = int(
        session.scalar(select(func.coalesce(func.max(ContentVersion.version_no), 0)).where(ContentVersion.article_id == article.id))
        or 0
    ) + 1
    session.add(
        ContentVersion(
            article_id=article.id,
            version_no=version_no,
            ru_title=article.ru_title or article.title,
            ru_summary=article.ru_summary or article.subtitle or "",
            short_hook=article.short_hook or (article.ru_title or article.title)[:100],
            extraction_json=None,
            quality_report={"manual_image_prompt": True},
            image_path=article.generated_image_path,
            image_prompt=prompt,
            selected_by_editor=False,
        )
    )


def _ensure_content_allowed(article_id: int) -> None:
    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            raise HTTPException(status_code=404, detail="Article not found")
        score = session.get(Score, article_id)
        if score is None:
            raise HTTPException(status_code=409, detail="score_required_before_content")
        min_rel = get_runtime_float("min_relevance_for_content", default=7.0)
        if float(score.relevance or 0.0) < min_rel:
            raise HTTPException(
                status_code=400,
                detail=f"article_not_ai_relevant_enough: relevance={score.relevance}, min={min_rel}",
            )


def _ensure_scored(article_id: int) -> None:
    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            raise HTTPException(status_code=404, detail="Article not found")
        score = session.get(Score, article_id)
        if score is None:
            raise HTTPException(status_code=409, detail="score_required_before_content")


@app.post("/ingestion/aggregate")
def ingestion_aggregate(body: AggregateIn) -> dict:
    period = body.period.lower()
    if period == "hour":
        ingestion = run_ingestion(hours_back=1)
    elif period == "day":
        ingestion = run_ingestion(days_back=1)
    elif period == "week":
        ingestion = run_ingestion(days_back=7)
    else:
        ingestion = run_ingestion(days_back=30)
    dedup = process_embeddings_and_dedup(limit=1000)
    enrich = enrich_summary_only_articles(limit=400, days_back=30)
    inserted_total = int(sum(ingestion.values()))
    score_limit = max(100, inserted_total * 2)
    scored = run_scoring(limit=score_limit)
    return {
        "ok": True,
        "period": period,
        "inserted_total": inserted_total,
        "by_source": ingestion,
        "dedup_processed": dedup,
        "enrich_summary_only": enrich,
        "scored": scored,
    }


@app.post("/ingestion/aggregate-fast")
def ingestion_aggregate_fast(body: AggregateIn) -> dict:
    period = body.period.lower()
    if period == "hour":
        ingestion = run_ingestion_fast(hours_back=1, max_entries=120)
    elif period == "day":
        ingestion = run_ingestion_fast(days_back=1, max_entries=200)
    elif period == "week":
        ingestion = run_ingestion_fast(days_back=7, max_entries=250)
    else:
        ingestion = run_ingestion_fast(days_back=30, max_entries=300)

    dedup = process_embeddings_and_dedup(limit=2500)
    inserted_total = int(sum(ingestion.values()))
    score_limit = max(200, inserted_total * 3)
    scored = run_scoring(limit=score_limit)
    pruned = prune_bad_articles(limit=20000)
    return {
        "ok": True,
        "period": period,
        "mode": "fast",
        "inserted_total": inserted_total,
        "by_source": ingestion,
        "dedup_processed": dedup,
        "scored": scored,
        "archived_pruned": pruned,
    }


@app.post("/articles/{article_id}/image-prompt/generate")
def image_prompt_generate(article_id: int) -> dict:
    _ensure_content_allowed(article_id)
    prompt = generate_image_prompt(article_id)
    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            raise HTTPException(status_code=404, detail="Article not found")
        _save_image_prompt_version(session, article, prompt)
    return {"ok": True, "image_prompt": prompt}


@app.post("/articles/{article_id}/image-prompt/save")
def image_prompt_save(article_id: int, body: ImagePromptIn) -> dict:
    _ensure_content_allowed(article_id)
    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            raise HTTPException(status_code=404, detail="Article not found")
        _save_image_prompt_version(session, article, body.prompt.strip())
    return {"ok": True}


@app.post("/articles/{article_id}/picture/generate")
def picture_generate(article_id: int) -> dict:
    _ensure_content_allowed(article_id)
    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            raise HTTPException(status_code=404, detail="Article not found")
        prompt = _latest_image_prompt(session, article_id)
    image_path = generate_image_card(article_id, image_prompt=prompt or None)
    return {"ok": True, "image_path": image_path, "image_prompt": prompt}


@app.post("/articles/{article_id}/picture/upload")
async def picture_upload(article_id: int, image: UploadFile = File(...)) -> dict:
    _ensure_content_allowed(article_id)
    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            raise HTTPException(status_code=404, detail="Article not found")

    content_type = (image.content_type or "").lower()
    if not content_type.startswith("image/"):
        raise HTTPException(status_code=400, detail="file_must_be_image")

    ext = Path(image.filename or "upload.png").suffix.lower() or ".png"
    if ext not in {".png", ".jpg", ".jpeg", ".webp"}:
        ext = ".png"

    out_dir = Path("app/static/generated/uploads")
    out_dir.mkdir(parents=True, exist_ok=True)
    fname = f"article_{article_id}_manual_{uuid.uuid4().hex[:10]}{ext}"
    out_path = out_dir / fname
    data = await image.read()
    out_path.write_bytes(data)

    object_name = f"articles/{article_id}/manual/{fname}"
    uploaded_url = upload_generated_image(str(out_path), object_name=object_name)
    final_path = uploaded_url or str(out_path)

    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            raise HTTPException(status_code=404, detail="Article not found")
        article.generated_image_path = final_path
        _save_image_prompt_version(session, article, _latest_image_prompt(session, article_id) or "manual_uploaded_image")

    return {"ok": True, "image_path": final_path}


@app.get("/articles")
def list_articles(
    status: str | None = None,
    limit: int = 100,
    sort_by: str = "created_at",
    sort_dir: str = "desc",
) -> list[dict]:
    with session_scope() as session:
        q = select(Article).where(Article.status != ArticleStatus.ARCHIVED)
        if status:
            q = q.where(Article.status == status)
        articles = session.scalars(q).all()

        result = []
        for a in articles:
            score = session.get(Score, a.id)
            source = session.get(Source, a.source_id)
            result.append(_serialize_article(a, score, source))

    reverse = sort_dir.lower() != "asc"
    if sort_by == "score":
        result.sort(key=lambda x: float(x["final_score"] or -1), reverse=reverse)
    elif sort_by == "source":
        result.sort(key=lambda x: (x.get("source_name") or "").lower(), reverse=reverse)
    elif sort_by in {"published_at", "published", "date"}:
        result.sort(key=lambda x: x.get("published_at") or x.get("created_at") or datetime.min, reverse=reverse)
    else:
        result.sort(key=lambda x: x.get("created_at") or "", reverse=reverse)
    return result[: min(max(limit, 1), 500)]


@app.get("/articles/{article_id}")
def article_details(article_id: int) -> dict:
    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            raise HTTPException(status_code=404, detail="Article not found")
        score = session.get(Score, article.id)
        source = session.get(Source, article.source_id)
        data = _serialize_article(article, score, source)
        data["text"] = article.text
        data["ru_summary"] = article.ru_summary
        data["short_hook"] = article.short_hook
        data["generated_image_path"] = article.generated_image_path
        data["post_preview"] = _build_post_preview_text(article)
        data["image_prompt"] = _latest_image_prompt(session, article_id)
        latest_feedback = session.scalars(
            select(EditorFeedback.explanation_text)
            .where(EditorFeedback.article_id == article_id)
            .order_by(EditorFeedback.created_at.desc())
            .limit(1)
        ).first()
        data["feedback"] = latest_feedback or ""
    return data


@app.get("/admin-data/articles")
def admin_data_articles(
    request: Request,
    view: str = "all",
    page: int = 1,
    page_size: int = 25,
    sort_by: str = "created_at",
    sort_dir: str = "desc",
    hide_double: bool = False,
    q: str = "",
) -> dict:
    _require_session_user(request)
    user = _get_session_user(request)
    page = max(page, 1)
    page_size = min(max(page_size, 1), 100)
    with session_scope() as session:
        tz_name = "Europe/Moscow"
        try:
            if user is not None:
                ws = session.scalars(select(UserWorkspace).where(UserWorkspace.user_id == user.id)).first()
                tz_name = (getattr(ws, "timezone_name", "") or "").strip() or tz_name
        except Exception:
            tz_name = "Europe/Moscow"
        result: list[dict] = []
        today = date.today()
        selected_day_map: dict[int, date] = {}
        base_query = select(Article)
        if view == "deleted":
            articles = session.scalars(
                base_query.where(Article.status.in_([ArticleStatus.ARCHIVED, ArticleStatus.REJECTED]))
            ).all()
        elif view == "published":
            base_query = base_query.where(Article.status != ArticleStatus.ARCHIVED)
            articles = session.scalars(base_query.where(Article.status == ArticleStatus.PUBLISHED)).all()
        elif view == "selected_hour":
            base_query = base_query.where(Article.status != ArticleStatus.ARCHIVED)
            articles = session.scalars(base_query.where(Article.status == ArticleStatus.SELECTED_HOURLY)).all()
        elif view == "selected_day":
            rows = session.execute(
                select(DailySelection.article_id, DailySelection.selected_date).where(
                    DailySelection.active.is_(True),
                )
            ).all()
            for aid, sdate in rows:
                prev = selected_day_map.get(int(aid))
                if prev is None or sdate > prev:
                    selected_day_map[int(aid)] = sdate
            ids = list(selected_day_map.keys())
            base_query = base_query.where(Article.status != ArticleStatus.ARCHIVED)
            articles = session.scalars(base_query.where(Article.id.in_(ids))).all() if ids else []
        elif view == "unsorted":
            # Unsorted = editor inbox excluding anything that was already "sent somewhere":
            # - published
            # - selected_hourly
            # - selected_day (any active date)
            # - archived/rejected
            selected_any_day_ids = session.scalars(
                select(DailySelection.article_id).where(DailySelection.active.is_(True))
            ).all()
            base_query = base_query.where(
                Article.status != ArticleStatus.ARCHIVED,
                Article.status != ArticleStatus.PUBLISHED,
                Article.status != ArticleStatus.SELECTED_HOURLY,
                Article.status != ArticleStatus.REJECTED,
            )
            if selected_any_day_ids:
                base_query = base_query.where(Article.id.not_in(list(set(int(x) for x in selected_any_day_ids))))
            articles = session.scalars(base_query).all()
        elif view == "no_double":
            base_query = base_query.where(Article.status != ArticleStatus.ARCHIVED)
            articles = session.scalars(base_query.where(Article.status != ArticleStatus.DOUBLE)).all()
        else:
            # "All" = broad working list for manual review/history passes.
            # Keep archived/rejected here too so editor can revisit old deletions and refine reasons.
            # We still exclude already-published and explicit selections to reduce noise.
            selected_day_ids = session.scalars(
                select(DailySelection.article_id).where(
                    DailySelection.selected_date == today,
                    DailySelection.active.is_(True),
                )
            ).all()
            base_query = base_query.where(
                Article.status != ArticleStatus.PUBLISHED,
                Article.status != ArticleStatus.SELECTED_HOURLY,
            )
            if selected_day_ids:
                base_query = base_query.where(Article.id.not_in(selected_day_ids))
            articles = session.scalars(base_query).all()

        for a in articles:
            score = session.get(Score, a.id)
            source = session.get(Source, a.source_id)
            item = _serialize_article(a, score, source)
            if a.id in selected_day_map:
                item["selected_date"] = selected_day_map[a.id].isoformat()
            item["is_selected_day"] = bool(
                session.scalar(
                    select(DailySelection.id).where(
                        DailySelection.article_id == a.id,
                        DailySelection.selected_date == today,
                        DailySelection.active.is_(True),
                    )
                )
            )
            result.append(item)

    if hide_double:
        result = [x for x in result if str(x.get("status") or "").upper() != "DOUBLE"]

    query_text = str(q or "").strip()
    if query_text:
        q_norm = query_text.casefold()
        words = [w for w in re.split(r"\s+", q_norm) if w]

        def _search_blob(x: dict) -> str:
            parts = [
                str(x.get("title") or ""),
                str(x.get("ru_title") or ""),
                str(x.get("subtitle") or ""),
                str(x.get("short_hook") or ""),
                str(x.get("text") or ""),
                str(x.get("ru_summary") or ""),
            ]
            return "\n".join(parts).casefold()

        exact_matches = [x for x in result if q_norm in _search_blob(x)]
        if exact_matches:
            result = exact_matches
        elif words:
            result = [x for x in result if all(w in _search_blob(x) for w in words)]

    reverse = sort_dir.lower() != "asc"
    # Default sort for ALL: newest day first, and within the day show the highest-scored items on top.
    # This matches the workflow: "сначала последние новости; в рамках дня самые сильные сверху".
    if view == "all" and sort_by == "created_at" and reverse:
        def _dt(x: dict) -> datetime:
            return x.get("published_at") or x.get("created_at") or datetime.min

        def _local_day(x: dict) -> date:
            dt = _dt(x)
            if not isinstance(dt, datetime):
                return date.min
            try:
                tz = ZoneInfo(tz_name)
            except Exception:
                tz = ZoneInfo("Europe/Moscow")
            return dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz).date()

        result.sort(
            key=lambda x: (
                _local_day(x),
                float(x.get("final_score") or -1),
                _dt(x),
            ),
            reverse=True,
        )
    if sort_by == "score":
        result.sort(key=lambda x: float(x["final_score"] or -1), reverse=reverse)
    elif sort_by == "source":
        result.sort(key=lambda x: (x.get("source_name") or "").lower(), reverse=reverse)
    elif sort_by in {"published_at", "published", "date"}:
        result.sort(key=lambda x: x.get("published_at") or x.get("created_at") or datetime.min, reverse=reverse)
    else:
        result.sort(key=lambda x: x.get("created_at") or "", reverse=reverse)

    total = len(result)
    start = (page - 1) * page_size
    items = result[start : start + page_size]
    total_pages = max(1, (total + page_size - 1) // page_size)
    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size,
        "total_pages": total_pages,
        "view": view,
        "q": query_text,
    }


@app.get("/articles/top-hour")
def top_hour() -> list[dict]:
    hour_ago = datetime.utcnow() - timedelta(hours=1)
    with session_scope() as session:
        rows = session.execute(
            select(Article, Score)
            .join(Score, Score.article_id == Article.id)
            .where(Article.created_at >= hour_ago, Article.status != ArticleStatus.DOUBLE)
            .order_by(Score.final_score.desc())
            .limit(20)
        ).all()
    return [{"article_id": a.id, "title": a.title, "score": s.final_score, "status": a.status} for a, s in rows]


@app.get("/stats/source-coverage")
def source_coverage() -> list[dict]:
    with session_scope() as session:
        rows = session.execute(
            select(
                Source.id,
                Source.name,
                Source.rss_url,
                func.count(Article.id).label("articles_count"),
                func.max(Article.published_at).label("latest_published_at"),
            )
            .select_from(Source)
            .join(Article, Article.source_id == Source.id, isouter=True)
            .group_by(Source.id, Source.name, Source.rss_url)
            .order_by(Source.priority_rank.asc())
        ).all()
    return [
        {
            "source_id": sid,
            "source_name": name,
            "rss_url": rss_url,
            "articles_count": int(cnt or 0),
            "latest_published_at": latest,
        }
        for sid, name, rss_url, cnt, latest in rows
    ]


@app.get("/admin-data/sources")
def admin_data_sources(request: Request) -> list[dict]:
    _require_session_user(request)
    with session_scope() as session:
        rows = session.execute(
            select(
                Source.id,
                Source.name,
                Source.rss_url,
                Source.kind,
                Source.priority_rank,
                Source.is_active,
                func.count(Article.id).label("articles_count"),
                func.max(Article.published_at).label("latest_published_at"),
            )
            .select_from(Source)
            .where(Source.is_deleted.is_(False))
            .join(Article, Article.source_id == Source.id, isouter=True)
            .group_by(Source.id, Source.name, Source.rss_url, Source.kind, Source.priority_rank, Source.is_active)
            .order_by(Source.priority_rank.asc())
        ).all()
    return [
        {
            "id": sid,
            "name": name,
            "rss_url": rss_url,
            "kind": kind,
            "priority_rank": int(rank or 0),
            "is_active": bool(active),
            "articles_count": int(cnt or 0),
            "latest_published_at": latest,
        }
        for sid, name, rss_url, kind, rank, active, cnt, latest in rows
    ]


@app.get("/admin-data/costs")
def admin_data_costs(request: Request) -> dict:
    _require_session_user(request)
    now = datetime.utcnow()
    day_ago = now - timedelta(days=1)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)
    with session_scope() as session:
        total_cost = float(session.scalar(select(func.coalesce(func.sum(LLMUsageLog.estimated_cost_usd), 0.0))) or 0.0)
        day_cost = float(
            session.scalar(
                select(func.coalesce(func.sum(LLMUsageLog.estimated_cost_usd), 0.0)).where(LLMUsageLog.created_at >= day_ago)
            )
            or 0.0
        )
        week_cost = float(
            session.scalar(
                select(func.coalesce(func.sum(LLMUsageLog.estimated_cost_usd), 0.0)).where(LLMUsageLog.created_at >= week_ago)
            )
            or 0.0
        )
        month_cost = float(
            session.scalar(
                select(func.coalesce(func.sum(LLMUsageLog.estimated_cost_usd), 0.0)).where(LLMUsageLog.created_at >= month_ago)
            )
            or 0.0
        )
        total_tokens = int(session.scalar(select(func.coalesce(func.sum(LLMUsageLog.total_tokens), 0))) or 0)
    return {
        "ok": True,
        "estimated_cost_usd_total": round(total_cost, 6),
        "estimated_cost_usd_24h": round(day_cost, 6),
        "estimated_cost_usd_7d": round(week_cost, 6),
        "estimated_cost_usd_30d": round(month_cost, 6),
        # Hide token counters in UI by default; keep only $ estimates.
        "note": "Estimated by token counters and configured per-million rates.",
    }


@app.get("/admin-data/worker-status")
def admin_data_worker_status(request: Request) -> dict:
    """
    UI helper: show whether the hourly worker is alive and when the next cycle is planned.
    The worker runs in the `pipeline` container (app/tasks/worker.py).
    """
    _require_session_user(request)
    user = _require_session_user(request)
    keys = [
        "worker_last_cycle_start_utc",
        "worker_last_cycle_finish_utc",
        "worker_next_cycle_utc",
        "worker_cycle_state",
        "worker_last_cycle_error",
    ]
    with session_scope() as session:
        out: dict[str, str] = {}
        for k in keys:
            row = session.get(TelegramBotKV, k)
            out[k] = (row.value if row else "") or ""
        ws = session.scalars(select(UserWorkspace).where(UserWorkspace.user_id == user.id)).first()
        out["tz"] = (getattr(ws, "timezone_name", "") or "").strip() or "Europe/Moscow"
    out["now_utc"] = datetime.utcnow().isoformat()
    out["ok"] = True
    return out


@app.post("/sources/{source_id}/active")
def set_source_active(source_id: int, body: SourceActiveIn, request: Request) -> dict:
    _require_session_user(request)
    with session_scope() as session:
        src = session.get(Source, source_id)
        if not src:
            raise HTTPException(status_code=404, detail="source_not_found")
        src.is_active = bool(body.is_active)
    return {"ok": True, "source_id": source_id, "is_active": bool(body.is_active)}


@app.post("/sources/add")
def add_source(body: SourceAddIn, request: Request) -> dict:
    _require_session_user(request)
    with session_scope() as session:
        exists = session.scalar(select(Source.id).where((Source.name == body.name) | (Source.rss_url == body.rss_url)))
        if exists:
            raise HTTPException(status_code=409, detail="source_already_exists")
        src = Source(
            name=body.name.strip(),
            rss_url=body.rss_url.strip(),
            kind=(body.kind or "rss").strip().lower(),
            priority_rank=int(body.priority_rank),
            trust_score=0.0,
            is_active=True,
            is_deleted=False,
        )
        session.add(src)
        session.flush()
        return {"ok": True, "source_id": int(src.id)}


@app.post("/sources/{source_id}/update")
def update_source(source_id: int, body: SourceUpdateIn, request: Request) -> dict:
    _require_session_user(request)
    with session_scope() as session:
        src = session.get(Source, source_id)
        if not src:
            raise HTTPException(status_code=404, detail="source_not_found")
        exists = session.scalar(
            select(Source.id).where(
                Source.id != source_id,
                ((Source.name == body.name.strip()) | (Source.rss_url == body.rss_url.strip())),
            )
        )
        if exists:
            raise HTTPException(status_code=409, detail="source_name_or_url_already_exists")
        src.name = body.name.strip()
        src.rss_url = body.rss_url.strip()
        src.kind = (body.kind or "rss").strip().lower()
        src.priority_rank = int(body.priority_rank)
        src.is_deleted = False
        if body.is_active is not None:
            src.is_active = bool(body.is_active)
    return {"ok": True, "source_id": source_id}


@app.post("/sources/{source_id}/check")
def check_source(source_id: int, request: Request) -> dict:
    _require_session_user(request)
    # lightweight: try load feed (rss) or scrape section page (html) and return counts
    from app.services.ingestion import check_source_health  # local import to avoid circulars

    return check_source_health(source_id)


@app.delete("/sources/{source_id}")
def delete_source(source_id: int, request: Request) -> dict:
    _require_session_user(request)
    with session_scope() as session:
        src = session.get(Source, source_id)
        if not src:
            raise HTTPException(status_code=404, detail="source_not_found")
        cnt = int(session.scalar(select(func.count(Article.id)).where(Article.source_id == source_id)) or 0)
        if cnt > 0:
            src.is_active = False
            src.is_deleted = True
            return {"ok": True, "deleted_source_id": source_id, "soft_deleted": True}
        session.delete(src)
    return {"ok": True, "deleted_source_id": source_id, "soft_deleted": False}


@app.get("/admin-data/score-params")
def admin_data_score_params(request: Request) -> list[dict]:
    _require_session_user(request)
    with session_scope() as session:
        rows = session.scalars(select(ScoreParameter).order_by(ScoreParameter.id.asc())).all()
        return [
            {
                "id": r.id,
                "key": r.key,
                "title": r.title,
                "description": r.description,
                "weight": float(r.weight or 0.0),
                "influence_rule": r.influence_rule,
                "is_active": bool(r.is_active),
            }
            for r in rows
        ]


@app.post("/score-params/upsert")
def score_params_upsert(body: ScoreParamUpsertIn, request: Request) -> dict:
    _require_session_user(request)
    key = (body.key or "").strip().lower()
    if not key:
        raise HTTPException(status_code=400, detail="key_required")
    with session_scope() as session:
        row = session.scalars(select(ScoreParameter).where(ScoreParameter.key == key)).first()
        if row is None:
            row = ScoreParameter(key=key)
            session.add(row)
        row.title = body.title.strip()
        row.description = (body.description or "").strip()
        row.weight = float(body.weight)
        row.influence_rule = (body.influence_rule or "").strip()
        row.is_active = bool(body.is_active)
        row.updated_at = datetime.utcnow()
        session.flush()
        return {"ok": True, "id": int(row.id), "key": row.key}


@app.delete("/score-params/{param_id}")
def score_params_delete(param_id: int, request: Request) -> dict:
    _require_session_user(request)
    with session_scope() as session:
        row = session.get(ScoreParameter, param_id)
        if not row:
            raise HTTPException(status_code=404, detail="score_param_not_found")
        session.delete(row)
    return {"ok": True, "deleted_id": param_id}


@app.get("/admin-data/runtime-settings")
def admin_data_runtime_settings(request: Request, scope: str | None = None, topic_key: str | None = None) -> dict:
    _require_session_user(request)
    rows = list_runtime_settings(scope=scope if scope in {"global", "topic"} else None, topic_key=topic_key)
    return {"ok": True, "items": rows, "defaults": dict(RUNTIME_DEFAULTS)}


@app.post("/runtime-settings/upsert")
def runtime_settings_upsert(body: RuntimeSettingUpsertIn, request: Request) -> dict:
    _require_session_user(request)
    try:
        row = upsert_runtime_setting(
            key=body.key,
            value=body.value,
            scope=("topic" if body.scope == "topic" else "global"),
            topic_key=body.topic_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "item": row}


@app.delete("/runtime-settings/{setting_id}")
def runtime_settings_delete(setting_id: int, request: Request) -> dict:
    _require_session_user(request)
    if not delete_runtime_setting(setting_id):
        raise HTTPException(status_code=404, detail="runtime_setting_not_found")
    return {"ok": True, "deleted_id": setting_id}


@app.get("/setup/state")
def setup_state(request: Request) -> dict:
    user = _require_session_user(request)
    with session_scope() as session:
        ws = session.scalars(select(UserWorkspace).where(UserWorkspace.user_id == user.id)).first()
        if ws is None:
            ws = UserWorkspace(user_id=user.id, onboarding_step=1, onboarding_completed=False)
            session.add(ws)
            session.flush()
        # If DB already has data (legacy single-user mode), don't force onboarding.
        if not ws.onboarding_completed:
            articles_cnt = int(session.scalar(select(func.count(Article.id))) or 0)
            sources_cnt = int(session.scalar(select(func.count(Source.id))) or 0)
            if articles_cnt > 0 and sources_cnt > 0:
                ws.onboarding_step = 4
                ws.onboarding_completed = True
                ws.updated_at = datetime.utcnow()
        return {
            "user_id": user.id,
            "email": user.email,
            "channel_name": ws.channel_name or "",
            "channel_theme": ws.channel_theme or "",
            "sources_text": ws.sources_text or "",
            "audience_description": ws.audience_description or "",
            "scoring_notes": ws.scoring_notes or "",
            "openrouter_api_key_set": bool((ws.openrouter_api_key_enc or "").strip()),
            "telegram_bot_token_set": bool((ws.telegram_bot_token_enc or "").strip()),
            "telegram_review_chat_id": ws.telegram_review_chat_id or "",
            "telegram_channel_id": ws.telegram_channel_id or "",
            "telegram_signature": ws.telegram_signature or "",
            "timezone_name": ws.timezone_name or "Europe/Moscow",
            "onboarding_step": int(ws.onboarding_step or 1),
            "onboarding_completed": bool(ws.onboarding_completed),
        }


@app.post("/setup/step1")
def setup_step1(body: SetupStep1In, request: Request) -> dict:
    user = _require_session_user(request)
    with session_scope() as session:
        ws = session.scalars(select(UserWorkspace).where(UserWorkspace.user_id == user.id)).first()
        if ws is None:
            ws = UserWorkspace(user_id=user.id)
            session.add(ws)
        ws.channel_name = body.channel_name.strip()
        ws.channel_theme = body.channel_theme.strip()
        ws.sources_text = body.sources_text.strip()
        # Optional: store per-user OpenRouter key (encrypted).
        # IMPORTANT: empty string MUST NOT clear an existing key; the UI keeps the field empty
        # when the secret is already set. Provide a dedicated "clear" action later if needed.
        if body.openrouter_api_key is not None:
            raw_key = (body.openrouter_api_key or "").strip()
            if raw_key:
                ws.openrouter_api_key_enc = encrypt_secret(raw_key)
        ws.onboarding_step = max(2, int(ws.onboarding_step or 1))
        ws.updated_at = datetime.utcnow()
        # Optional convenience: auto-add provided urls as sources if they are not present yet.
        lines = [x.strip() for x in ws.sources_text.splitlines() if x.strip()]
        if lines:
            existing_urls = {u for (u,) in session.execute(select(Source.rss_url)).all()}
            max_rank = int(session.scalar(select(func.max(Source.priority_rank))) or 100)
            added = 0
            for line in lines:
                if not line.startswith(("http://", "https://")):
                    continue
                if line in existing_urls:
                    continue
                added += 1
                session.add(
                    Source(
                        name=f"User Source {max_rank + added}",
                        rss_url=line,
                        kind="rss" if ("/feed" in line or "/rss" in line or line.endswith(".xml")) else "html",
                        priority_rank=max_rank + added,
                        trust_score=0.0,
                        is_active=True,
                    )
                )
    return {"ok": True, "step": 1}


@app.post("/setup/telegram")
def setup_telegram(body: SetupTelegramIn, request: Request) -> dict:
    user = _require_session_user(request)
    with session_scope() as session:
        ws = session.scalars(select(UserWorkspace).where(UserWorkspace.user_id == user.id)).first()
        if ws is None:
            ws = UserWorkspace(user_id=user.id)
            session.add(ws)
        # IMPORTANT: empty string MUST NOT clear an existing token; the UI keeps the field empty
        # when the secret is already set. Provide a dedicated "clear" action later if needed.
        if body.telegram_bot_token is not None:
            raw = (body.telegram_bot_token or "").strip()
            if raw:
                ws.telegram_bot_token_enc = encrypt_secret(raw)
        ws.telegram_review_chat_id = (body.telegram_review_chat_id or "").strip()
        ws.telegram_channel_id = (body.telegram_channel_id or "").strip()
        ws.telegram_signature = (body.telegram_signature or "").strip()
        ws.timezone_name = (body.timezone_name or "Europe/Moscow").strip() or "Europe/Moscow"
        ws.updated_at = datetime.utcnow()
    return {"ok": True}

@app.post("/setup/step2/save")
def setup_step2_save(body: SetupStep2In, request: Request) -> dict:
    """Save audience description without running LLM analyze (cheap)."""
    user = _require_session_user(request)
    with session_scope() as session:
        ws = session.scalars(select(UserWorkspace).where(UserWorkspace.user_id == user.id)).first()
        if ws is None:
            ws = UserWorkspace(user_id=user.id)
            session.add(ws)
        ws.audience_description = body.audience_description.strip()
        ws.onboarding_step = max(3, int(ws.onboarding_step or 1))
        ws.updated_at = datetime.utcnow()
    return {"ok": True}


@app.post("/setup/step2/analyze")
def setup_step2_analyze(body: SetupStep2In, request: Request) -> dict:
    user = _require_session_user(request)
    with session_scope() as session:
        ws = session.scalars(select(UserWorkspace).where(UserWorkspace.user_id == user.id)).first()
        if ws is None:
            ws = UserWorkspace(user_id=user.id)
            session.add(ws)
        ws.audience_description = body.audience_description.strip()
        ws.onboarding_step = max(3, int(ws.onboarding_step or 1))
        ws.updated_at = datetime.utcnow()

    fallback_params = [
        {
            "key": "relevance",
            "title": "Relevance",
            "weight": 0.17,
            "description": "Насколько новость относится к теме AI/LLM для канала.",
            "influence_rule": "Повышать оценку за core AI темы, понижать за оффтоп.",
        },
        {
            "key": "significance",
            "title": "Significance",
            "weight": 0.16,
            "description": "Влияние на рынок, пользователей, индустрию.",
            "influence_rule": "Выше при масштабных изменениях, релизах, регуляторике.",
        },
        {
            "key": "business_it",
            "title": "Business IT Impact",
            "weight": 0.06,
            "description": "Понятность и практическая ценность для массовой аудитории/предпринимателей.",
            "influence_rule": "Повышать для прикладных, понятных, полезных сценариев.",
        },
    ]

    params = fallback_params
    # Use per-user key (loaded by middleware) first; fall back to server env key if present.
    if (get_workspace_api_key(user.id) or settings.openrouter_api_key):
        try:
            client = get_client()
            prompt = (
                "Ты архитектор скоринга новостного AI-канала. "
                "На основе описания аудитории верни JSON c массивом params (4-10 элементов): "
                "[{key,title,weight,description,influence_rule}]. "
                "Вес 0..1, сумма примерно 1. "
                "Ключи только латиницей и snake_case.\n\n"
                f"Audience:\n{body.audience_description.strip()}"
            )
            resp = client.chat.completions.create(
                model=settings.llm_text_model,
                messages=[
                    {"role": "system", "content": "Отвечай только валидным JSON без пояснений."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.2,
                response_format={"type": "json_object"},
            )
            track_usage_from_response(resp, operation="setup.step2_analyze", model=settings.llm_text_model, kind="chat")
            raw = resp.choices[0].message.content or "{}"
            data = json.loads(raw)
            items = data.get("params")
            if isinstance(items, list) and items:
                parsed = []
                for it in items:
                    key = str((it or {}).get("key") or "").strip().lower()
                    title = str((it or {}).get("title") or key).strip()
                    if not key or not title:
                        continue
                    try:
                        weight = float((it or {}).get("weight", 0.0))
                    except Exception:
                        weight = 0.0
                    parsed.append(
                        {
                            "key": key[:64],
                            "title": title[:128],
                            "weight": max(0.0, min(1.0, weight)),
                            "description": str((it or {}).get("description") or "")[:4000],
                            "influence_rule": str((it or {}).get("influence_rule") or "")[:4000],
                        }
                    )
                if parsed:
                    params = parsed
        except Exception:
            params = fallback_params

    with session_scope() as session:
        for p in params:
            row = session.scalars(select(ScoreParameter).where(ScoreParameter.key == p["key"])).first()
            if row is None:
                row = ScoreParameter(key=p["key"])
                session.add(row)
            row.title = p["title"]
            row.description = p["description"]
            row.weight = float(p["weight"])
            row.influence_rule = p["influence_rule"]
            row.is_active = True
            row.updated_at = datetime.utcnow()
    return {"ok": True, "params": params}


@app.post("/setup/complete")
def setup_complete(request: Request) -> dict:
    user = _require_session_user(request)
    with session_scope() as session:
        ws = session.scalars(select(UserWorkspace).where(UserWorkspace.user_id == user.id)).first()
        if ws is None:
            ws = UserWorkspace(user_id=user.id)
            session.add(ws)
        ws.onboarding_step = 4
        ws.onboarding_completed = True
        ws.updated_at = datetime.utcnow()

    # Initial bootstrap: month sync + dedup + scoring + top pick.
    ingestion = run_ingestion(days_back=30, max_entries=300)
    dedup = process_embeddings_and_dedup(limit=3000)
    scored = run_scoring(limit=3000)
    top_hour = pick_hourly_top()
    return {
        "ok": True,
        "ingested_total": int(sum(ingestion.values())),
        "dedup_processed": int(dedup),
        "scored": int(scored),
        "top_hour_article_id": top_hour,
    }


@app.post("/articles/{article_id}/prepare")
def prepare_article(article_id: int) -> dict:
    _ensure_content_allowed(article_id)
    try:
        ok = generate_ru_summary(article_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"prepare_summary_failed: {exc}") from exc
    if not ok:
        raise HTTPException(status_code=404, detail="Article not found")
    try:
        image_path = generate_image_card(article_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"prepare_image_failed: {exc}") from exc

    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            raise HTTPException(status_code=404, detail="Article not found")
        return {
            "ok": True,
            "image_path": image_path,
            "ru_title": article.ru_title,
            "ru_summary": article.ru_summary,
            "post_preview": _build_post_preview_text(article),
            "canonical_url": article.canonical_url,
        }


@app.post("/articles/{article_id}/post/generate")
def generate_post_only(article_id: int) -> dict:
    """Generate RU post text only (title + 2-paragraph summary) from English article data.

    Does NOT require Translate Full step and does NOT generate image.
    """
    _ensure_content_allowed(article_id)
    try:
        ok = generate_ru_summary(article_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"generate_post_failed: {exc}") from exc
    if not ok:
        raise HTTPException(status_code=404, detail="Article not found")

    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            raise HTTPException(status_code=404, detail="Article not found")
        return {
            "ok": True,
            "ru_title": article.ru_title,
            "ru_summary": article.ru_summary,
            "post_preview": _build_post_preview_text(article),
            "canonical_url": article.canonical_url,
        }


@app.post("/articles/{article_id}/translate")
def translate_article(article_id: int) -> dict:
    # Manual preview translation is allowed even for low-relevance articles:
    # editor may still want RU preview text for training/review.
    _ensure_scored(article_id)
    out = translate_article_text(article_id)
    if not out.get("ok"):
        if out.get("error") == "article_not_found":
            raise HTTPException(status_code=404, detail="Article not found")
        raise HTTPException(status_code=400, detail=str(out.get("error") or "translate_failed"))
    return out


@app.post("/articles/{article_id}/translate-full")
def translate_article_full(article_id: int) -> dict:
    _ensure_content_allowed(article_id)
    out = translate_article_full_style(article_id)
    if not out.get("ok"):
        if out.get("error") == "article_not_found":
            raise HTTPException(status_code=404, detail="Article not found")
        raise HTTPException(status_code=400, detail=str(out.get("error") or "translate_failed"))
    return out


@app.post("/articles/{article_id}/text/override")
def override_article_text(article_id: int, body: TextOverrideIn) -> dict:
    text = (body.text or "").strip()
    if len(text) < 50:
        raise HTTPException(status_code=400, detail="text_too_short")
    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            raise HTTPException(status_code=404, detail="Article not found")
        article.text = text
        article.content_mode = "full" if len(text) >= 800 else "summary_only"
        article.updated_at = datetime.utcnow()
        return {"ok": True, "article_id": article_id, "content_mode": article.content_mode, "text_len": len(text)}


@app.post("/articles/{article_id}/content/pull")
def pull_article_content(article_id: int) -> dict:
    out = enrich_article_from_source(article_id)
    if not out.get("ok"):
        if out.get("error") == "article_not_found":
            raise HTTPException(status_code=404, detail="Article not found")
        raise HTTPException(status_code=400, detail=str(out.get("error") or "pull_failed"))
    return out


@app.post("/articles/{article_id}/ru/save")
def save_ru_text(article_id: int, body: RuEditIn) -> dict:
    ru_title = (body.ru_title or "").strip()
    ru_summary = (body.ru_summary or "").strip()
    if not ru_title:
        raise HTTPException(status_code=400, detail="ru_title_required")
    if len(ru_summary) < 10:
        raise HTTPException(status_code=400, detail="ru_summary_too_short")
    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            raise HTTPException(status_code=404, detail="Article not found")
        article.ru_title = ru_title
        article.ru_summary = ru_summary
        if not article.short_hook:
            article.short_hook = ru_title[:100]
        article.updated_at = datetime.utcnow()
        return {
            "ok": True,
            "article_id": article_id,
            "ru_title": article.ru_title,
            "ru_summary": article.ru_summary,
            "post_preview": _build_post_preview_text(article),
        }


@app.post("/articles/{article_id}/select-day")
def select_day(article_id: int) -> dict:
    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            raise HTTPException(status_code=404, detail="Article not found")
        today = date.today()
        row = session.scalars(
            select(DailySelection).where(
                DailySelection.article_id == article_id,
                DailySelection.selected_date == today,
            )
        ).first()
        if row:
            row.active = True
        else:
            session.add(DailySelection(article_id=article_id, selected_date=today, active=True))
    return {"ok": True}


@app.post("/articles/{article_id}/unselect-day")
def unselect_day(article_id: int) -> dict:
    with session_scope() as session:
        today = date.today()
        row = session.scalars(
            select(DailySelection).where(
                DailySelection.article_id == article_id,
                DailySelection.selected_date == today,
                DailySelection.active.is_(True),
            )
        ).first()
        if row:
            row.active = False
    return {"ok": True}


@app.post("/articles/{article_id}/unselect-hour")
def unselect_hour(article_id: int) -> dict:
    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            raise HTTPException(status_code=404, detail="Article not found")
        if article.status == ArticleStatus.SELECTED_HOURLY:
            article.status = ArticleStatus.SCORED
            article.updated_at = datetime.utcnow()
    return {"ok": True}


@app.post("/articles/{article_id}/feedback")
def save_feedback(article_id: int, body: FeedbackIn) -> dict:
    text = (body.explanation_text or "").strip()
    if len(text) < 5:
        raise HTTPException(status_code=400, detail="feedback_too_short")
    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            raise HTTPException(status_code=404, detail="Article not found")
        session.add(EditorFeedback(article_id=article_id, explanation_text=text))
    return {"ok": True, "feedback": text}


@app.post("/articles/{article_id}/status")
def set_article_status(article_id: int, body: StatusIn) -> dict:
    try:
        status = ArticleStatus(body.status)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid status") from exc

    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            raise HTTPException(status_code=404, detail="Article not found")
        article.status = status
        article.updated_at = datetime.utcnow()
    return {"ok": True, "status": status}


@app.delete("/articles/{article_id}")
def delete_article(article_id: int, body: DeleteIn) -> dict:
    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            raise HTTPException(status_code=404, detail="Article not found")
        score = session.get(Score, article_id)

        delete_reason = (body.reason or "").strip()
        if len(delete_reason) < 5:
            raise HTTPException(status_code=400, detail="delete_reason_required")

        audit(
            action="article_delete_feedback",
            entity_type="article",
            entity_id=str(article_id),
            payload={
                "reason": delete_reason,
                "title": article.title,
                "canonical_url": article.canonical_url,
                "status": str(article.status),
                "content_mode": article.content_mode,
                "score_10": _score_to_10(score),
            },
        )

        article.status = ArticleStatus.ARCHIVED
        article.archived_kind = "delete"
        article.archived_reason = delete_reason
        article.archived_at = datetime.utcnow()
        article.updated_at = datetime.utcnow()
        session.query(DailySelection).filter(
            DailySelection.article_id == article_id,
            DailySelection.active.is_(True),
        ).update({"active": False}, synchronize_session=False)
    return {"ok": True, "deleted_article_id": article_id, "mode": "soft"}


@app.post("/articles/{article_id}/restore")
def restore_article(article_id: int) -> dict:
    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            raise HTTPException(status_code=404, detail="Article not found")
        article.status = ArticleStatus.INBOX
        article.updated_at = datetime.utcnow()
    return {"ok": True, "restored_article_id": article_id}


@app.post("/feedback/rebuild-profile")
def rebuild_profile() -> dict:
    return rebuild_preference_profile(min_feedback=20)


@app.post("/selection/auto")
def auto_selection() -> dict:
    return auto_select_by_profile(top_n=5)


@app.post("/articles/{article_id}/publish")
def publish(article_id: int) -> dict:
    return publish_article(article_id)


@app.post("/articles/{article_id}/schedule-publish")
def schedule_publish(article_id: int, body: SchedulePublishIn, request: Request) -> dict:
    # Interpret publish time in user's configured timezone (default: Europe/Moscow),
    # then store in DB as naive UTC for stable scheduling.
    # This fixes the common confusion: selecting "10:00" should mean 10:00 local time,
    # not 10:00 UTC.
    user = _require_session_user(request)
    raw = (body.publish_at or "").strip()
    if not raw:
        raise HTTPException(status_code=400, detail="publish_at_required")
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="invalid_publish_at") from exc
    with session_scope() as session:
        ws = session.scalars(select(UserWorkspace).where(UserWorkspace.user_id == user.id)).first()
        tz_name = (ws.timezone_name if ws else "") or "Europe/Moscow"
    try:
        user_tz = ZoneInfo(tz_name)
    except Exception:
        user_tz = ZoneInfo("Europe/Moscow")

    if dt.tzinfo is None:
        dt_utc = dt.replace(tzinfo=user_tz).astimezone(timezone.utc).replace(tzinfo=None)
    else:
        dt_utc = dt.astimezone(timezone.utc).replace(tzinfo=None)

    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            raise HTTPException(status_code=404, detail="Article not found")
        article.scheduled_publish_at = dt_utc
        article.updated_at = datetime.utcnow()
    return {
        "ok": True,
        "article_id": article_id,
        "scheduled_publish_at": _dt_to_utc_z(dt_utc),
        "timezone_name": tz_name,
    }


@app.post("/articles/{article_id}/unschedule-publish")
def unschedule_publish(article_id: int, request: Request) -> dict:
    _require_session_user(request)
    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            raise HTTPException(status_code=404, detail="Article not found")
        article.scheduled_publish_at = None
        article.updated_at = datetime.utcnow()
    return {"ok": True, "article_id": article_id, "scheduled_publish_at": None}


@app.post("/telegram/test")
def telegram_test(request: Request) -> dict:
    # Use the logged-in user's Telegram secret (multi-tenant).
    user = _require_session_user(request)
    load_workspace_telegram_context(user.id)
    return send_test_message()


@app.post("/telegram/review/send-latest")
def telegram_review_send_latest(request: Request, force: bool = False) -> dict:
    user = _require_session_user(request)
    load_workspace_telegram_context(user.id)
    return send_hourly_top_for_review(article_id=None, force=bool(force))


@app.post("/telegram/review/send-backlog")
def telegram_review_send_backlog(request: Request, limit: int = 10) -> dict:
    user = _require_session_user(request)
    load_workspace_telegram_context(user.id)
    return send_selected_backlog_for_review(limit=limit)

@app.post("/telegram/review/send-hourly-backfill")
def telegram_review_send_hourly_backfill(request: Request, hours: int = 24, limit: int = 24, force: bool = False) -> dict:
    user = _require_session_user(request)
    load_workspace_telegram_context(user.id)
    return send_hourly_backfill_for_review(hours_back=hours, limit=limit, force=bool(force))

@app.get("/telegram/review/jobs")
def telegram_review_jobs(request: Request, limit: int = 20) -> dict:
    _require_session_user(request)
    n = max(1, min(int(limit), 200))
    with session_scope() as session:
        rows = session.execute(
            select(TelegramReviewJob).order_by(TelegramReviewJob.id.desc()).limit(n)
        ).scalars().all()
    return {
        "ok": True,
        "items": [
            {
                "id": r.id,
                "article_id": r.article_id,
                "chat_id": r.chat_id,
                "review_message_id": r.review_message_id,
                "status": r.status,
                "created_at": r.created_at,
                "updated_at": r.updated_at,
            }
            for r in rows
        ],
    }


@app.post("/telegram/review/poll")
def telegram_review_poll(request: Request) -> dict:
    user = _require_session_user(request)
    load_workspace_telegram_context(user.id)
    return poll_review_updates(limit=100)


@app.get("/admin/score", response_class=HTMLResponse)
def admin_score_page(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse(url="/login", status_code=303)
    return """
<!doctype html>
<html lang='ru'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>Score Parameters</title>
  <link rel="stylesheet" href="/static/app.css?v=3">
</head>
<body>
  <header>
    <a href="/"><button>← Back</button></a>
    <a href="/setup"><button>Setup</button></a>
    <a href="/sources"><button>Sources</button></a>
    <span class="muted">Параметры влияют на формулу скоринга. Весы нормализуются автоматически.</span>
    <span id="result" class="muted"></span>
  </header>
  <main>
    <h3>Add / Update Parameter</h3>
    <div style="display:grid;gap:8px;grid-template-columns: 160px 220px 120px 1fr;">
      <input id="p_key" placeholder="key (snake_case)" />
      <input id="p_title" placeholder="title" />
      <input id="p_weight" type="number" step="0.01" min="0" max="1" value="0.05" />
      <input id="p_desc" placeholder="description" />
    </div>
    <div style="margin-top:8px;">
      <textarea id="p_rule" placeholder="Как параметр влияет на оценку"></textarea>
      <label style="display:inline-flex;gap:6px;align-items:center;margin-top:8px;"><input id="p_active" type="checkbox" checked /> active</label>
      <button onclick="saveParam()">Save Param</button>
    </div>
    <h3>Current Parameters</h3>
    <table>
      <thead><tr><th>ID</th><th>Key</th><th>Title</th><th>Weight</th><th>Active</th><th>Description</th><th>Rule</th><th>Action</th></tr></thead>
      <tbody id="rows"></tbody>
    </table>

    <h3 style="margin-top:24px;">Runtime Filters / Style Settings</h3>
    <p class="muted">Эти значения больше не нужно держать в .env. Поддерживается global или topic scope.</p>
    <p class="muted">Telegram настройки теперь настраиваются в <code>/setup</code> (как secret в workspace пользователя). Здесь остаются только non-secret runtime параметры.</p>
    <div style="display:grid;gap:8px;grid-template-columns: 180px 120px 180px 1fr;">
      <input id="rs_key" placeholder="key" />
      <select id="rs_scope"><option value="global">global</option><option value="topic">topic</option></select>
      <input id="rs_topic" placeholder="topic_key (for topic scope)" />
      <input id="rs_value" placeholder="value (string/number/csv/bool)" />
    </div>
    <div style="margin-top:8px;">
      <button onclick="saveRuntime()">Save Runtime Setting</button>
      <button onclick="loadRuntimeSettings()">Reload</button>
    </div>
    <table style="margin-top:12px;">
      <thead><tr><th>ID</th><th>Scope</th><th>Topic</th><th>Key</th><th>Value</th><th>Action</th></tr></thead>
      <tbody id="runtime_rows"></tbody>
    </table>
  </main>
  <script>
    function setResult(v){ document.getElementById('result').textContent = typeof v === 'string' ? v : JSON.stringify(v); }
    function esc(s){ return String(s||'').replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;'); }
    let paramsById = {};
    async function loadParams(){
      const resp = await fetch('/admin-data/score-params');
      if (!resp.ok) { if (resp.status === 401) location.href='/login'; return; }
      const rows = await resp.json();
      paramsById = {};
      for (const r of (rows || [])) { paramsById[String(r.id)] = r; }
      document.getElementById('rows').innerHTML = (rows || []).map(r => `
        <tr>
          <td>${r.id}</td><td>${esc(r.key)}</td><td>${esc(r.title)}</td><td>${Number(r.weight||0).toFixed(4)}</td>
          <td>${r.is_active ? 'yes' : 'no'}</td><td>${esc(r.description)}</td><td>${esc(r.influence_rule)}</td>
          <td>
            <button onclick='fillParam(${r.id})'>Edit</button>
            <button onclick='deleteParam(${r.id})'>Delete</button>
          </td>
        </tr>`).join('');
    }
    function fillParam(id){
      const r = paramsById[String(id)] || {};
      document.getElementById('p_key').value = r.key || '';
      document.getElementById('p_title').value = r.title || '';
      document.getElementById('p_weight').value = String(r.weight ?? 0.05);
      document.getElementById('p_desc').value = r.description || '';
      document.getElementById('p_rule').value = r.influence_rule || '';
      document.getElementById('p_active').checked = !!r.is_active;
    }
    async function saveParam(){
      const body = {
        key: (document.getElementById('p_key').value || '').trim(),
        title: (document.getElementById('p_title').value || '').trim(),
        weight: Number(document.getElementById('p_weight').value || 0),
        description: (document.getElementById('p_desc').value || '').trim(),
        influence_rule: (document.getElementById('p_rule').value || '').trim(),
        is_active: !!document.getElementById('p_active').checked
      };
      const resp = await fetch('/score-params/upsert', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
      const out = await resp.json();
      setResult(out);
      if (!resp.ok) return alert(out.detail || 'save failed');
      loadParams();
    }
    async function deleteParam(id){
      if (!confirm('Delete param '+id+'?')) return;
      const resp = await fetch('/score-params/' + id, {method:'DELETE'});
      const out = await resp.json();
      setResult(out);
      if (!resp.ok) return alert(out.detail || 'delete failed');
      loadParams();
    }
    async function loadRuntimeSettings(){
      const resp = await fetch('/admin-data/runtime-settings');
      if (!resp.ok) { if (resp.status === 401) location.href='/login'; return; }
      const out = await resp.json();
      const items = out.items || [];
      document.getElementById('runtime_rows').innerHTML = items.map(r => `
        <tr>
          <td>${r.id}</td><td>${esc(r.scope)}</td><td>${esc(r.topic_key || '')}</td><td>${esc(r.key)}</td>
          <td style="max-width:660px;word-break:break-word;">${esc(r.value)}</td>
          <td><button onclick='fillRuntime(${r.id})'>Edit</button><button onclick='deleteRuntime(${r.id})'>Delete</button></td>
        </tr>
      `).join('');
      window.runtimeById = {};
      for (const i of items) window.runtimeById[String(i.id)] = i;
    }
    function fillRuntime(id){
      const r = (window.runtimeById || {})[String(id)] || {};
      document.getElementById('rs_key').value = r.key || '';
      document.getElementById('rs_scope').value = r.scope || 'global';
      document.getElementById('rs_topic').value = r.topic_key || '';
      document.getElementById('rs_value').value = r.value || '';
    }
    async function saveRuntime(){
      const body = {
        key: (document.getElementById('rs_key').value || '').trim(),
        scope: (document.getElementById('rs_scope').value || 'global').trim(),
        topic_key: (document.getElementById('rs_topic').value || '').trim() || null,
        value: (document.getElementById('rs_value').value || '').trim()
      };
      const resp = await fetch('/runtime-settings/upsert', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
      const out = await resp.json();
      setResult(out);
      if (!resp.ok) return alert(out.detail || 'save runtime failed');
      loadRuntimeSettings();
    }
    async function deleteRuntime(id){
      if (!confirm('Delete runtime setting '+id+'?')) return;
      const resp = await fetch('/runtime-settings/' + id, {method:'DELETE'});
      const out = await resp.json();
      setResult(out);
      if (!resp.ok) return alert(out.detail || 'delete failed');
      loadRuntimeSettings();
    }
    loadParams();
    loadRuntimeSettings();
  </script>
</body>
</html>
"""


@app.get("/score", response_class=HTMLResponse)
def score_page(request: Request):
    return admin_score_page(request)


@app.get("/admin/setup", response_class=HTMLResponse)
def admin_setup_page(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse(url="/login", status_code=303)
    return """
<!doctype html>
<html lang='ru'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>Setup Wizard</title>
  <link rel="stylesheet" href="/static/app.css?v=3">
</head>
<body>
  <header>
    <a href="/"><button>← Back</button></a>
    <a href="/score"><button>Score</button></a>
    <a href="/sources"><button>Sources</button></a>
    <span id="status" class="muted"></span>
  </header>
  <main class="nv-container-sm">
    <div class="card">
      <h3>Step 1: Channel</h3>
      <p class="muted">Название канала, тематика, OpenRouter API key (твой). Источники добавляются отдельно в разделе <code>Sources</code>.</p>
      <div id="step1SavedView" class="card" style="display:none;padding:12px;margin:10px 0;background:#0f1a33;">
        <div style="display:flex;gap:10px;align-items:center;justify-content:space-between;flex-wrap:wrap;">
          <b>Сохранено</b>
          <button type="button" onclick="toggleStep1Edit(true)">✎ Edit</button>
        </div>
        <div style="margin-top:8px;">
          <div class="muted">Название канала</div>
          <div id="channel_name_saved">-</div>
        </div>
        <div style="margin-top:8px;">
          <div class="muted">Тематика</div>
          <div id="channel_theme_saved" style="white-space:pre-wrap;">-</div>
        </div>
      </div>
      <div id="step1EditView" style="display:block;">
        <p><input id="channel_name" placeholder="Название канала" /></p>
        <p><textarea id="channel_theme" placeholder="Тематика канала"></textarea></p>
      </div>
      <div class="card" style="padding:12px;margin:10px 0;background:#0f1a33;">
        <div class="muted" id="openrouter_hint"></div>
        <div id="openrouterSavedRow" style="display:none;gap:10px;align-items:center;flex-wrap:wrap;">
          <span>OpenRouter key: <b id="openrouterMask">********</b></span>
          <button type="button" onclick="toggleOpenrouterEdit(true)">✎ Edit</button>
        </div>
        <div id="openrouterEditRow" style="display:block;margin-top:10px;">
          <input id="openrouter_api_key" type="password" placeholder="OpenRouter API key (sk-or-v1-...)" style="width:100%;" />
          <div class="muted" style="margin-top:6px;">Ключ хранится в базе зашифрованно и не отображается обратно. Чтобы заменить, вставь новый ключ и нажми Save.</div>
        </div>
      </div>
      <p style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
        <button onclick="saveStep1()">Save Step 1</button>
        <span id="step1_save_status" class="muted"></span>
      </p>
    </div>
    <div class="card">
      <h3>Step 2: Audience + Scoring</h3>
      <p class="muted">Опиши аудиторию канала. ИИ предложит параметры скоринга и заполнит раздел Score.</p>
      <div id="step2SavedView" class="card" style="display:none;padding:12px;margin:10px 0;background:#0f1a33;">
        <div style="display:flex;gap:10px;align-items:center;justify-content:space-between;flex-wrap:wrap;">
          <b>Аудитория сохранена</b>
          <button type="button" onclick="toggleStep2Edit(true)">✎ Edit</button>
        </div>
        <div style="margin-top:8px;">
          <div class="muted">Описание аудитории</div>
          <div id="audience_description_saved" style="white-space:pre-wrap;">-</div>
        </div>
      </div>
      <div id="step2EditView" style="display:block;">
        <p><textarea id="audience_description" placeholder="Для кого канал, что им важно, какие темы нежелательны"></textarea></p>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;">
        <button onclick="saveStep2()">Save Audience</button>
        <button onclick="analyzeStep2()">Analyze Scoring</button>
        <span id="step2_save_status" class="muted" style="align-self:center;"></span>
      </div>
      <pre id="analysis_out"></pre>
    </div>
    <div class="card">
      <h3>Step 3: Telegram</h3>
      <p class="muted">Куда слать превью на review и куда публиковать. Bot token хранится как secret у пользователя (в базе, зашифрованно) и не отображается обратно.</p>
      <p class="muted" id="telegram_token_hint"></p>
      <div id="telegramSavedRow" style="display:none;gap:10px;align-items:center;flex-wrap:wrap;">
        <span>Bot token: <b id="telegramTokenMask">********</b></span>
        <button type="button" onclick="toggleTelegramTokenEdit(true)">✎ Edit</button>
      </div>
      <div id="telegramTokenEditRow" style="display:block;margin-top:10px;">
        <input id="telegram_bot_token" type="password" placeholder="Bot token (123:AA...)" style="width:100%;" />
      </div>
      <p class="muted" style="margin-top:10px;">Review chat: куда бот шлет превью для ревью (например <code>@Yudin_Finance</code> или chat_id).</p>
      <p><input id="telegram_review_chat_id" placeholder="Review chat id (например @Yudin_Finance)" /></p>
      <p class="muted">Channel id: куда бот публикует посты (обычно <code>-100…</code>).</p>
      <p><input id="telegram_channel_id" placeholder="Channel id (например -1002340845297)" /></p>
      <p><input id="telegram_signature" placeholder="Signature (например @neuro_vibes_future)" /></p>
      <p><input id="timezone_name" placeholder="Timezone (например Europe/Moscow)" /></p>
      <p><button onclick="saveTelegram()">Save Telegram</button></p>
      <p class="muted" id="telegram_hint"></p>
    </div>
    <div class="card">
      <h3>Step 4: Initial Bootstrap</h3>
      <p class="muted">Сбор за месяц + дедуп + скоринг + автоподбор top hour.</p>
      <p><button onclick="completeSetup()">Run Initial Import</button></p>
      <pre id="bootstrap_out"></pre>
    </div>
  </main>
  <script>
    function setStatus(v){ document.getElementById('status').textContent = v; }
    function flashSaveStatus(id, text){
      const el = document.getElementById(id);
      if (!el) return;
      el.textContent = text || '';
      if (!text) return;
      setTimeout(() => { if (el.textContent === text) el.textContent = ''; }, 2500);
    }
    function toggleStep1Edit(on){
      const edit = document.getElementById('step1EditView');
      const saved = document.getElementById('step1SavedView');
      if (edit) edit.style.display = on ? 'block' : 'none';
      if (saved) saved.style.display = on ? 'none' : 'block';
      if (on) { const el = document.getElementById('channel_name'); if (el) el.focus(); }
    }
    function toggleStep2Edit(on){
      const edit = document.getElementById('step2EditView');
      const saved = document.getElementById('step2SavedView');
      if (edit) edit.style.display = on ? 'block' : 'none';
      if (saved) saved.style.display = on ? 'none' : 'block';
      if (on) { const el = document.getElementById('audience_description'); if (el) el.focus(); }
    }
    function toggleTelegramTokenEdit(on){
      const edit = document.getElementById('telegramTokenEditRow');
      const saved = document.getElementById('telegramSavedRow');
      if (edit) edit.style.display = on ? 'block' : 'none';
      if (saved) saved.style.display = on ? 'none' : 'flex';
      if (on) { const el = document.getElementById('telegram_bot_token'); if (el) el.focus(); }
    }
    function toggleOpenrouterEdit(on){
      const edit = document.getElementById('openrouterEditRow');
      const saved = document.getElementById('openrouterSavedRow');
      if (edit) edit.style.display = on ? 'block' : 'none';
      if (saved) saved.style.display = on ? 'none' : 'flex';
      if (on) { const el = document.getElementById('openrouter_api_key'); if (el) el.focus(); }
    }
    async function loadState(){
      const resp = await fetch('/setup/state');
      if (!resp.ok) { if (resp.status === 401) location.href='/login'; return; }
      const s = await resp.json();
      document.getElementById('channel_name').value = s.channel_name || '';
      document.getElementById('channel_theme').value = s.channel_theme || '';
      document.getElementById('audience_description').value = s.audience_description || '';
      const cnSaved = document.getElementById('channel_name_saved');
      if (cnSaved) cnSaved.textContent = s.channel_name || '—';
      const ctSaved = document.getElementById('channel_theme_saved');
      if (ctSaved) ctSaved.textContent = s.channel_theme || '—';
      const adSaved = document.getElementById('audience_description_saved');
      if (adSaved) adSaved.textContent = s.audience_description || '—';
      document.getElementById('telegram_review_chat_id').value = s.telegram_review_chat_id || '';
      document.getElementById('telegram_channel_id').value = s.telegram_channel_id || '';
      document.getElementById('telegram_signature').value = s.telegram_signature || '';
      document.getElementById('timezone_name').value = s.timezone_name || 'Europe/Moscow';
      document.getElementById('openrouter_hint').textContent = s.openrouter_api_key_set ? 'OpenRouter key: set (hidden).' : 'OpenRouter key: not set. You must set it to use LLM features for your account.';
      document.getElementById('telegram_token_hint').textContent = s.telegram_bot_token_set ? 'Bot token: set (hidden).' : 'Bot token: not set. Review/publish will not work until you set it.';
      // Show masked saved rows for secrets (token/key) and hide edit fields until user clicks pencil.
      const orMask = document.getElementById('openrouterMask');
      if (orMask) orMask.textContent = s.openrouter_api_key_set ? '******** (saved)' : 'not set';
      toggleOpenrouterEdit(!s.openrouter_api_key_set);
      const tgMask = document.getElementById('telegramTokenMask');
      if (tgMask) tgMask.textContent = s.telegram_bot_token_set ? '******** (saved)' : 'not set';
      toggleTelegramTokenEdit(!s.telegram_bot_token_set);
      toggleStep1Edit(!(s.channel_name || s.channel_theme));
      toggleStep2Edit(!s.audience_description);

      const missing = [];
      if (!s.telegram_bot_token_set) missing.push('bot token');
      if (!s.telegram_review_chat_id) missing.push('review chat');
      if (!s.telegram_channel_id) missing.push('channel id');
      const hint = missing.length ? ('Telegram: missing ' + missing.join(', ') + '.') : 'Telegram: chat + channel configured.';
      const warn = (String(s.telegram_review_chat_id||'').startsWith('-100') && !String(s.telegram_channel_id||'').startsWith('-100')) ? ' (Похоже, ты вставил channel id в review chat.)' : '';
      document.getElementById('telegram_hint').textContent = hint + warn;
      setStatus(`User: ${s.email} | step: ${s.onboarding_step} | completed: ${s.onboarding_completed ? 'yes':'no'}`);
    }
    async function saveStep1(){
      const body = {
        channel_name: (document.getElementById('channel_name').value || '').trim(),
        channel_theme: (document.getElementById('channel_theme').value || '').trim(),
        openrouter_api_key: (document.getElementById('openrouter_api_key').value || '').trim(),
      };
      body.sources_text = ''; // sources are managed in /sources
      const resp = await fetch('/setup/step1', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
      const out = await resp.json();
      if (!resp.ok) return alert(out.detail || 'save step1 failed');
      setStatus('Step 1 saved');
      flashSaveStatus('step1_save_status', 'Saved');
      document.getElementById('openrouter_api_key').value = '';
      await loadState();
    }
    async function analyzeStep2(){
      const body = { audience_description: (document.getElementById('audience_description').value || '').trim() };
      const resp = await fetch('/setup/step2/analyze', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
      const out = await resp.json();
      document.getElementById('analysis_out').textContent = JSON.stringify(out, null, 2);
      if (!resp.ok) return alert(out.detail || 'analyze failed');
      setStatus('Scoring params updated');
      loadState();
    }
    async function saveStep2(){
      const body = { audience_description: (document.getElementById('audience_description').value || '').trim() };
      const resp = await fetch('/setup/step2/save', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
      const out = await resp.json();
      if (!resp.ok) return alert(out.detail || 'save audience failed');
      setStatus('Audience saved');
      flashSaveStatus('step2_save_status', 'Saved');
      await loadState();
    }
    async function saveTelegram(){
      const body = {
        telegram_bot_token: (document.getElementById('telegram_bot_token').value || '').trim(),
        telegram_review_chat_id: (document.getElementById('telegram_review_chat_id').value || '').trim(),
        telegram_channel_id: (document.getElementById('telegram_channel_id').value || '').trim(),
        telegram_signature: (document.getElementById('telegram_signature').value || '').trim(),
        timezone_name: (document.getElementById('timezone_name').value || '').trim(),
      };
      const resp = await fetch('/setup/telegram', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
      const out = await resp.json();
      if (!resp.ok) return alert(out.detail || 'save telegram failed');
      document.getElementById('telegram_bot_token').value = '';
      setStatus('Telegram settings saved');
      loadState();
    }
    async function completeSetup(){
      const resp = await fetch('/setup/complete', {method:'POST'});
      const out = await resp.json();
      document.getElementById('bootstrap_out').textContent = JSON.stringify(out, null, 2);
      if (!resp.ok) return alert(out.detail || 'bootstrap failed');
      setStatus('Setup completed');
      loadState();
    }
    loadState();
  </script>
</body>
</html>
"""


@app.get("/setup", response_class=HTMLResponse)
def setup_page(request: Request):
    return admin_setup_page(request)


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    # Keep /admin for backward compatibility, but treat root "/" as the main entrypoint.
    return RedirectResponse(url="/", status_code=303)


@app.get("/", response_class=HTMLResponse)
def home_page(request: Request):
    # Platform root.
    user = _get_session_user(request)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    with session_scope() as session:
        ws = session.scalars(select(UserWorkspace).where(UserWorkspace.user_id == user.id)).first()
        if ws is None:
            ws = UserWorkspace(user_id=user.id, onboarding_step=1, onboarding_completed=False)
            session.add(ws)
            session.flush()
        if not ws.onboarding_completed:
            articles_cnt = int(session.scalar(select(func.count(Article.id))) or 0)
            sources_cnt = int(session.scalar(select(func.count(Source.id))) or 0)
            if articles_cnt > 0 and sources_cnt > 0:
                ws.onboarding_step = 4
                ws.onboarding_completed = True
                ws.updated_at = datetime.utcnow()
            else:
                return RedirectResponse(url="/setup", status_code=303)
    return _render_admin_list_page("all")


@app.get("/admin/published", response_class=HTMLResponse)
def admin_published_page(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse(url="/login", status_code=303)
    return _render_admin_list_page("published")


@app.get("/published", response_class=HTMLResponse)
def published_page(request: Request):
    return admin_published_page(request)


@app.get("/admin/selected-day", response_class=HTMLResponse)
def admin_selected_day_page(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse(url="/login", status_code=303)
    return _render_admin_list_page("selected_day")


@app.get("/selected-day", response_class=HTMLResponse)
def selected_day_page(request: Request):
    return admin_selected_day_page(request)


@app.get("/admin/selected-hour", response_class=HTMLResponse)
def admin_selected_hour_page(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse(url="/login", status_code=303)
    return _render_admin_list_page("selected_hour")


@app.get("/selected-hour", response_class=HTMLResponse)
def selected_hour_page(request: Request):
    return admin_selected_hour_page(request)

@app.get("/admin/unsorted", response_class=HTMLResponse)
def admin_unsorted_page(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse(url="/login", status_code=303)
    return _render_admin_list_page("unsorted")


@app.get("/unsorted", response_class=HTMLResponse)
def unsorted_page(request: Request):
    return admin_unsorted_page(request)


@app.get("/admin/no-double", response_class=HTMLResponse)
def admin_no_double_page(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse(url="/login", status_code=303)
    return _render_admin_list_page("no_double")


@app.get("/no-double", response_class=HTMLResponse)
def no_double_page(request: Request):
    return admin_no_double_page(request)


@app.get("/admin/deleted", response_class=HTMLResponse)
def admin_deleted_page(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse(url="/login", status_code=303)
    return _render_admin_list_page("deleted")


@app.get("/deleted", response_class=HTMLResponse)
def deleted_page(request: Request):
    return admin_deleted_page(request)


@app.get("/admin/sources", response_class=HTMLResponse)
def admin_sources_page(request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse(url="/login", status_code=303)
    user = _get_session_user(request)
    return """
<!doctype html>
<html lang='ru'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>Sources</title>
  <link rel="stylesheet" href="/static/app.css?v=3">
</head>
<body>
  <header>
    <a href="/"><button>← Back</button></a>
    <a href="/score"><button>Score</button></a>
    <a href="/setup"><button>Setup</button></a>
    <a href="/logout"><button>Logout</button></a>
    <span class="muted">Sources: включай/выключай фиды. Выключенный источник не будет загружаться при Sync.</span>
    <span id="result" class="muted"></span>
  </header>
  <main>
    <h3>Add Source</h3>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:12px;">
      <input id="addName" placeholder="Name" style="min-width:220px" />
      <select id="addKind" style="min-width:120px">
        <option value="rss" selected>rss</option>
        <option value="html">html</option>
      </select>
      <input id="addUrl" placeholder="RSS URL or Section URL" style="min-width:420px" />
      <input id="addRank" placeholder="Rank" type="number" value="50" min="1" max="999" style="width:100px" />
      <button onclick="addSource()">Add</button>
    </div>
    <h3>All Sources</h3>
    <table>
      <thead>
        <tr>
          <th>ID</th>
          <th>Active</th>
          <th>Kind</th>
          <th>Rank</th>
          <th>Name</th>
          <th>RSS</th>
          <th>Articles</th>
          <th>Latest</th>
          <th>Action</th>
        </tr>
      </thead>
      <tbody id="rows"></tbody>
    </table>
  </main>
  <script>
    function setResult(v){ document.getElementById('result').textContent = v; }

    async function loadSources(){
      const resp = await fetch('/admin-data/sources');
      const items = await resp.json();
      const rows = document.getElementById('rows');
      rows.innerHTML = (items || []).map(s => `
        <tr>
          <td>${s.id}</td>
          <td>${s.is_active ? 'yes' : 'no'}</td>
          <td><code>${escapeHtml(s.kind || 'rss')}</code></td>
          <td>${s.priority_rank}</td>
          <td>${escapeHtml(s.name)}</td>
          <td><a href="${s.rss_url}" target="_blank">${escapeHtml(s.rss_url)}</a></td>
          <td>${s.articles_count}</td>
          <td>${s.latest_published_at ?? '-'}</td>
          <td>
            <button onclick="checkSource(${s.id})">Check</button>
            <button onclick="editSource(${s.id})">Edit</button>
            ${s.is_active ? `<button onclick="setActive(${s.id}, false)">Disable</button>` : `<button onclick="setActive(${s.id}, true)">Enable</button>`}
            <button onclick="deleteSource(${s.id})">Delete</button>
          </td>
        </tr>
      `).join('');
    }

    function escapeHtml(s) {
      return String(s || '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');
    }

    async function setActive(id, isActive){
      const resp = await fetch(`/sources/${id}/active`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({is_active: !!isActive})});
      const out = await resp.json();
      setResult(JSON.stringify(out));
      loadSources();
    }

    async function addSource(){
      const name = document.getElementById('addName').value.trim();
      const rss_url = document.getElementById('addUrl').value.trim();
      const priority_rank = Number(document.getElementById('addRank').value || 50);
      const kind = (document.getElementById('addKind').value || 'rss').trim();
      if (name.length < 2 || rss_url.length < 8) {
        alert('Name и RSS URL обязательны');
        return;
      }
      const resp = await fetch('/sources/add', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name, rss_url, priority_rank, kind})});
      const out = await resp.json();
      setResult(JSON.stringify(out));
      loadSources();
    }

    async function checkSource(id){
      const resp = await fetch(`/sources/${id}/check`, {method:'POST'});
      const out = await resp.json();
      setResult(JSON.stringify(out));
    }

    async function editSource(id){
      const resp0 = await fetch('/admin-data/sources');
      const items = await resp0.json();
      const s = (items || []).find(x => Number(x.id) === Number(id));
      if (!s) { alert('Источник не найден'); return; }

      const name = prompt('Название источника', s.name || '');
      if (name === null) return;
      const kind = prompt('Тип источника: rss или html', (s.kind || 'rss'));
      if (kind === null) return;
      const rss_url = prompt('URL (RSS или раздел новостей)', s.rss_url || '');
      if (rss_url === null) return;
      const rankRaw = prompt('Priority rank (1..999)', String(s.priority_rank || 50));
      if (rankRaw === null) return;
      const priority_rank = Number(rankRaw || 50);
      if (!name.trim() || !rss_url.trim()) { alert('Name и URL обязательны'); return; }
      if (!['rss','html'].includes(String(kind).trim().toLowerCase())) { alert('Тип должен быть rss или html'); return; }
      if (!Number.isFinite(priority_rank) || priority_rank < 1 || priority_rank > 999) { alert('Rank 1..999'); return; }

      const resp = await fetch(`/sources/${id}/update`, {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({
          name: name.trim(),
          kind: String(kind).trim().toLowerCase(),
          rss_url: rss_url.trim(),
          priority_rank: Math.round(priority_rank),
        }),
      });
      const out = await resp.json();
      setResult(JSON.stringify(out));
      if (!resp.ok) { alert(out.detail || 'update failed'); return; }
      loadSources();
    }

    async function deleteSource(id){
      if (!confirm('Удалить источник ' + id + '?')) return;
      const resp = await fetch(`/sources/${id}`, {method:'DELETE'});
      const out = await resp.json();
      setResult(JSON.stringify(out));
      if (!resp.ok) alert(out.detail || 'delete failed');
      loadSources();
    }

    loadSources();
  </script>
</body>
</html>
"""


@app.get("/sources", response_class=HTMLResponse)
def sources_page(request: Request):
    return admin_sources_page(request)


@app.get("/bot", response_class=HTMLResponse)
def bot_page(request: Request):
    _require_session_user(request)
    user = _get_session_user(request)
    with session_scope() as session:
        ws = session.scalars(select(UserWorkspace).where(UserWorkspace.user_id == user.id)).first()
        bot_set = bool(getattr(ws, "telegram_bot_token_enc", None))
        review_chat = (getattr(ws, "telegram_review_chat_id", "") or "").strip()
        channel_id = (getattr(ws, "telegram_channel_id", "") or "").strip()
        signature = (getattr(ws, "telegram_signature", "") or "").strip()
        tz = (getattr(ws, "timezone_name", "") or "").strip()
        last_start = session.get(TelegramBotKV, "worker_last_cycle_start_utc")
        last_finish = session.get(TelegramBotKV, "worker_last_cycle_finish_utc")
        next_cycle = session.get(TelegramBotKV, "worker_next_cycle_utc")
        cycle_state = session.get(TelegramBotKV, "worker_cycle_state")
        last_err = session.get(TelegramBotKV, "worker_last_cycle_error")
        last_start_s = (last_start.value if last_start else "").strip()
        last_finish_s = (last_finish.value if last_finish else "").strip()
        next_cycle_s = (next_cycle.value if next_cycle else "").strip()
        cycle_state_s = (cycle_state.value if cycle_state else "").strip()
        last_err_s = (last_err.value if last_err else "").strip()

    def _mask(v: str) -> str:
        if not v:
            return "-"
        if len(v) <= 6:
            return v
        return v[:3] + "…" + v[-2:]

    return f"""
<!doctype html>
<html lang='ru'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>Bot</title>
  <link rel="stylesheet" href="/static/app.css?v=3">
</head>
<body>
  <header>
    <a href="/"><button>← Articles</button></a>
    <a href="/setup"><button>Setup</button></a>
    <a href="/logout"><button>Logout</button></a>
  </header>
  <main class="nv-container-sm">
    <div class="card">
      <h2 style="margin:0 0 10px 0;">Telegram Bot</h2>
      <div class="muted">Настройки (токены/чаты) редактируются в <code>Setup → Telegram</code>.</div>
      <p class="muted" style="margin-top:10px;">
        Bot token: <b>{'set' if bot_set else 'not set'}</b><br>
        Review chat: <b>{review_chat or '-'}</b><br>
        Channel: <b>{channel_id or '-'}</b><br>
        Signature: <b>{signature or '-'}</b><br>
        Timezone: <b>{tz or '-'}</b><br>
        Auto-send: <b>каждый час</b> (воркер в контейнере <code>pipeline</code>)<br>
        Worker state: <b>{cycle_state_s or '-'}</b><br>
        Last error: <b>{escape(last_err_s[:180]) if last_err_s else '-'}</b><br>
        Last cycle start: <b id="ws_last_start">{last_start_s or '-'}</b><br>
        Last cycle finish: <b id="ws_last_finish">{last_finish_s or '-'}</b><br>
        Next cycle: <b id="ws_next">{next_cycle_s or '-'}</b><br>
        <span class="muted">Время отображается в таймзоне: <code id="ws_tz">{tz or 'Europe/Moscow'}</code></span>
      </p>
      <div style="display:flex;gap:8px;flex-wrap:wrap;">
        <button onclick="telegramTest()">Telegram Test</button>
        <button onclick="telegramPoll()">Poll TG Now</button>
        <button onclick="telegramHourlyBackfill24()">Send 24h Backfill (24 msgs)</button>
        <button onclick="telegramHourlyBackfillCustom()">Send Backfill (custom)</button>
      </div>
      <pre id="out" style="white-space:pre-wrap;background:#0b1428;border:1px solid #2a3b60;padding:12px;border-radius:8px;margin-top:12px;"></pre>
    </div>
  </main>
  <script>
    function fmtUtcToTz(iso, tz) {{
      const s = String(iso || '').trim();
      if (!s || s === '-') return s;
      const hasOffset = /[zZ]|[+-]\\d\\d:\\d\\d$/.test(s);
      const d = new Date(hasOffset ? s : (s + 'Z'));
      if (isNaN(d.getTime())) return s;
      return d.toLocaleString('ru-RU', {{ timeZone: tz || 'Europe/Moscow' }});
    }}
    (function(){{ 
      const tz = (document.getElementById('ws_tz')?.textContent || 'Europe/Moscow').trim();
      const s1 = document.getElementById('ws_last_start');
      const s2 = document.getElementById('ws_last_finish');
      const s3 = document.getElementById('ws_next');
      if (s1) s1.textContent = fmtUtcToTz(s1.textContent, tz) || '-';
      if (s2) s2.textContent = fmtUtcToTz(s2.textContent, tz) || '-';
      if (s3) s3.textContent = fmtUtcToTz(s3.textContent, tz) || '-';
    }})();
    function setOut(v) {{ document.getElementById('out').textContent = v; }}
    async function telegramTest() {{
      setOut('Sending test…');
      const resp = await fetch('/telegram/test', {{method:'POST'}});
      const out = await resp.json();
      setOut(JSON.stringify(out, null, 2));
      if (!resp.ok) alert(out.detail || 'telegram test failed');
    }}
    async function telegramBacklog() {{
      const n = prompt('How many messages to send? (1..100)', '10');
      if (n === null) return;
      setOut('Sending backlog…');
      const resp = await fetch(`/telegram/review/send-backlog?limit=${{Math.max(1, Math.min(100, Number(n) || 10))}}`, {{method:'POST'}});
      const out = await resp.json();
      setOut(JSON.stringify(out, null, 2));
      if (!resp.ok) alert(out.detail || 'send backlog failed');
    }}
    async function telegramHourlyBackfill24() {{
      const force = confirm('Force resend already-sent items for 24 hours? (OK = resend, Cancel = only new)');
      setOut('Selecting per-hour + sending…');
      const hours = 24;
      const limit = 24;
      const resp = await fetch(`/telegram/review/send-hourly-backfill?hours=${{hours}}&limit=${{limit}}&force=${{force ? 'true' : 'false'}}`, {{method:'POST'}});
      const out = await resp.json();
      setOut(JSON.stringify(out, null, 2));
      if (!resp.ok) alert(out.detail || 'send hourly backfill failed');
    }}
    async function telegramHourlyBackfillCustom() {{
      const h = prompt('Backfill how many hours? (1..168)', '24');
      if (h === null) return;
      const hours = Math.max(1, Math.min(168, Number(h) || 24));
      const limit = Math.max(1, Math.min(100, hours));
      const force = confirm('Force resend already-sent items? (OK = resend, Cancel = only new)');
      setOut('Selecting per-hour + sending…');
      const resp = await fetch(`/telegram/review/send-hourly-backfill?hours=${{hours}}&limit=${{limit}}&force=${{force ? 'true' : 'false'}}`, {{method:'POST'}});
      const out = await resp.json();
      setOut(JSON.stringify(out, null, 2));
      if (!resp.ok) alert(out.detail || 'send hourly backfill failed');
    }}
    async function telegramPoll() {{
      setOut('Polling Telegram updates…');
      const resp = await fetch('/telegram/review/poll', {{method:'POST'}});
      const out = await resp.json();
      setOut(JSON.stringify(out, null, 2));
      if (!resp.ok) alert(out.detail || 'poll failed');
    }}
  </script>
</body>
</html>
"""


@app.get("/publish", response_class=HTMLResponse)
def publish_settings_page(request: Request):
    _require_session_user(request)
    return """
<!doctype html>
<html lang='ru'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>Publish</title>
  <link rel="stylesheet" href="/static/app.css?v=3">
</head>
<body>
  <header>
    <a href="/"><button>← Articles</button></a>
    <a href="/setup"><button>Setup</button></a>
    <a href="/logout"><button>Logout</button></a>
  </header>
  <main class="nv-container-sm">
    <div class="card">
      <h2 style="margin:0 0 10px 0;">Publish Settings</h2>
      <div class="muted">
        Настройки публикации (channel_id, signature, timezone, bot token) находятся в <code>Setup → Telegram</code>.
        Публикация делается через Telegram-review (кнопка "Опубликовать") или из карточки статьи.
      </div>
      <p class="muted" style="margin-top:10px;">Если хочешь, добавлю сюда управление расписанием и очередь запланированных постов.</p>
    </div>
  </main>
</body>
</html>
"""


def _render_admin_list_page(view: str) -> str:
    return """
<!doctype html>
<html lang='ru'>
<head>
  <meta charset='utf-8'>
  <meta name='viewport' content='width=device-width, initial-scale=1'>
  <title>Neurovibes Admin</title>
  <link rel="stylesheet" href="/static/app.css?v=3">
</head>
<body>
  <div id="navOverlay" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9998;align-items:center;justify-content:center;">
    <div style="background:#0f1a33;border:1px solid #345;border-radius:12px;padding:14px 16px;min-width:240px;max-width:90vw;">
      <div class="muted">Загружаю…</div>
      <div style="margin-top:10px;height:10px;background:#0c1a33;border:1px solid #355;border-radius:999px;overflow:hidden;">
        <div style="height:100%;width:40%;background:#3f8cff;animation:nvload 1.1s ease-in-out infinite;"></div>
      </div>
    </div>
  </div>
  <header>
    <div class="menu" tabindex="0">
      <div class="menu-trigger">Account ▾</div>
      <div class="menu-panel">
        <a class="menu-item" href="/setup">Setup</a>
        <a class="menu-item" href="/sources">Sources</a>
        <a class="menu-item" href="/logout">Logout</a>
      </div>
    </div>

    <div class="menu" tabindex="0">
      <div class="menu-trigger">Articles ▾</div>
      <div class="menu-panel">
        <a class="menu-item" href="/">All</a>
        <a class="menu-item" href="/unsorted">Unsorted</a>
        <a class="menu-item" href="/published">Published</a>
        <a class="menu-item" href="/selected-day">Selected Day</a>
        <a class="menu-item" href="/selected-hour">Selected Hour</a>
        <a class="menu-item" href="/deleted">Deleted</a>
        <button onclick="autoSelect()" title="Pick best candidate using preference profile (no publish)">Auto Select</button>
      </div>
    </div>

    <div class="menu" tabindex="0">
      <div class="menu-trigger">Actions ▾</div>
      <div class="menu-panel">
        <label>Period:
          <select id="aggregatePeriod">
            <option value="hour">1h</option>
            <option value="day">1d</option>
            <option value="week">1w</option>
            <option value="month" selected>1m</option>
          </select>
        </label>
        <button onclick="aggregateNews()" title="Load only new items for selected period">Sync</button>
        <button onclick="runPipeline()" title="Sync + Enrich full text + Dedup + Score + Pick hourly top + Prepare RU+Image">Run Pipeline</button>
        <button onclick="runScoring()" title="Score unscored items (new) and update Selected Hour">Score New</button>
        <button onclick="enrichFullText()" title="Try to fetch full text from site for summary_only articles">Get Full Text</button>
        <button onclick="pruneBad()" title="Archive items that don't match filters (non-AI, too technical, low relevance, etc.)">Prune</button>
        <button onclick="rebuildProfile()" title="Rebuild preference profile from feedback (LLM-costly)">Rebuild Profile</button>
      </div>
    </div>

    <div class="menu" tabindex="0">
      <div class="menu-trigger">Tools ▾</div>
      <div class="menu-panel">
        <a class="menu-item" href="/bot">Bot</a>
        <a class="menu-item" href="/publish">Publish</a>
        <a class="menu-item" href="/score">Score</a>
      </div>
    </div>

    <div class="spacer"></div>

    <div class="statusbar">
      <label><input id="hideDoubleToggle" type="checkbox" onchange="onHideDoubleChange()"> No Double</label>
      <label>Page size: <input id="pageSizeInput" type="number" value="25" min="5" max="100" onchange="onPageSizeChange()"></label>
      <label style="display:flex;align-items:center;gap:6px;">
        Search:
        <input id="searchInput" type="text" placeholder="Заголовок или текст..." style="min-width:260px;">
        <button type="button" onclick="applySearch()">Search</button>
        <button type="button" onclick="clearSearch()">Clear</button>
      </label>
      <span class="muted">Generate Post: RU заголовок + 2 абзаца (без Translate Full)</span>
      <span id="costBadge" class="muted">Cost: ...</span>
      <span id="workerBadge" class="muted">Worker: ...</span>
      <span id="action_state" class="muted">Ready</span>
      <span id="result" class="muted"></span>
    </div>

    <div id="scoreProgressWrap" style="display:none;min-width:260px;">
      <div class="muted" id="scoreProgressText">Scoring: 0/0</div>
      <div style="height:10px;background:#0c1a33;border:1px solid #355;border-radius:999px;overflow:hidden;">
        <div id="scoreProgressBar" style="height:100%;width:0%;background:#3f8cff;"></div>
      </div>
    </div>
    <div id="enrichProgressWrap" style="display:none;min-width:340px;">
      <div class="muted" id="enrichProgressText">Enrich: 0/0</div>
      <div style="height:10px;background:#0c1a33;border:1px solid #355;border-radius:999px;overflow:hidden;">
        <div id="enrichProgressBar" style="height:100%;width:0%;background:#37c078;"></div>
      </div>
    </div>
  <div id="pruneProgressWrap" style="display:none;min-width:420px;">
      <div class="muted" id="pruneProgressText">Prune: 0/0</div>
      <div style="height:10px;background:#0c1a33;border:1px solid #355;border-radius:999px;overflow:hidden;">
        <div id="pruneProgressBar" style="height:100%;width:0%;background:#ffb020;"></div>
      </div>
    </div>
    <div id="pipelineProgressWrap" style="display:none;min-width:420px;">
      <div class="muted" id="pipelineProgressText">Pipeline: 0/0</div>
      <div style="height:10px;background:#0c1a33;border:1px solid #355;border-radius:999px;overflow:hidden;">
        <div id="pipelineProgressBar" style="height:100%;width:0%;background:#c084fc;"></div>
      </div>
    </div>
  </header>
  <main>
    <table>
      <thead>
        <tr>
          <th>ID</th>
          <th>Status</th>
          <th>Content</th>
          <th id="sortHeaderScore" class="sortable" onclick="toggleSort('score')">Score<span id="sortIndicatorScore" class="sort-indicator"></span></th>
          <th>Title</th>
          <th id="sortHeaderSource" class="sortable" onclick="toggleSort('source')">Source<span id="sortIndicatorSource" class="sort-indicator"></span></th>
          <th id="sortHeaderDate" class="sortable" onclick="toggleSort('published_at')">Published<span id="sortIndicatorDate" class="sort-indicator"></span></th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody id="rows"></tbody>
    </table>
    <div style="display:flex;gap:8px;align-items:center;margin-top:12px;">
      <button onclick="prevPage()">Prev</button>
      <span id="pager" class="muted"></span>
      <button onclick="nextPage()">Next</button>
    </div>
  </main>
  <div id="previewModal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9999;">
    <div style="max-width:900px;margin:4vh auto;background:#0f1a33;border:1px solid #345;border-radius:12px;padding:16px;max-height:90vh;overflow:auto;">
      <div style="display:flex;justify-content:space-between;align-items:center;">
        <h3 style="margin:0;">Preview Post</h3>
        <button onclick="closePreview()">Close</button>
      </div>
      <p class="muted" id="previewTitle"></p>
      <pre id="previewText" style="white-space:pre-wrap;background:#0b1428;border:1px solid #2a3b60;padding:12px;border-radius:8px;"></pre>
    </div>
  </div>
  <script>
    // Loader overlay on menu navigation: immediate feedback even if the server is slow.
    document.addEventListener('click', (e) => {
      const a = e.target && e.target.closest ? e.target.closest('a.menu-item') : null;
      if (!a) return;
      const href = a.getAttribute('href') || '';
      if (!href || !href.startsWith('/')) return;
      const ov = document.getElementById('navOverlay');
      if (ov) ov.style.display = 'flex';
    });

    const CURRENT_VIEW = "__VIEW__";
    let currentPage = 1;
    let totalPages = 1;
    let scorePollTimer = null;
    let sortBy = null;
    let sortDir = null;
    let searchQuery = '';
    window.NV_TZ = 'Europe/Moscow';

    async function loadArticles() {
      const pageSize = document.getElementById('pageSizeInput').value || '25';
      const qs = new URLSearchParams();
      qs.set('view', CURRENT_VIEW);
      qs.set('page', String(currentPage));
      qs.set('page_size', String(pageSize));
      const hideDouble = document.getElementById('hideDoubleToggle')?.checked;
      if (hideDouble) qs.set('hide_double', '1');
      if (searchQuery && searchQuery.trim()) qs.set('q', searchQuery.trim());
      if (sortBy && sortDir) {
        qs.set('sort_by', sortBy);
        qs.set('sort_dir', sortDir);
      }
      const resp = await fetch('/admin-data/articles?' + qs.toString());
      const data = await resp.json();
      const rows = document.getElementById('rows');
      const items = data.items || [];
      totalPages = data.total_pages || 1;
      document.getElementById('pager').textContent = `Page ${data.page || 1}/${totalPages} · total ${data.total || 0}`;
      rows.innerHTML = items.map(a => `
        <tr id="row-${a.id}">
          <td>${a.id}</td>
          <td><span class='tag'>${a.status}</span></td>
          <td><span class='tag'>${a.content_mode || 'summary_only'}</span></td>
          <td>${a.score_10 ?? 'not scored'}</td>
          <td>
            <a href='/article/${a.id}'>${escapeHtml(a.ru_title || a.title)}</a>
            <div class='muted' style='margin-top:6px;'>${escapeHtml((a.short_hook || a.subtitle || '').slice(0, 220))}</div>
            <div class='muted' style='margin-top:6px;'><a href='${a.canonical_url}' target='_blank'>source</a></div>
          </td>
          <td>${escapeHtml(a.source_name || ('#' + a.source_id))}</td>
          <td>${fmtUtcToTz(a.published_at, window.NV_TZ) || '-'}</td>
          <td>
            ${a.is_selected_day ? `<button onclick='unselectDay(${a.id})'>Remove Day</button>` : `<button onclick='selectDay(${a.id})'>Select Day</button>`}
            ${String(a.status || '').toUpperCase() === 'SELECTED_HOURLY' ? `<button onclick='unselectHour(${a.id})'>Remove Hour</button>` : `<button onclick='selectHour(${a.id})'>Select Hour</button>`}
            ${CURRENT_VIEW === 'deleted' ? `<button onclick='restoreArticle(${a.id})'>Restore</button>` : `<button onclick='deleteArticle(${a.id})'>Delete</button>`}
            <button onclick='archiveArticle(${a.id})'>Archive</button>
            <button onclick='publish(${a.id})'>Publish</button>
          </td>
        </tr>
      `).join('');
      renderSortIndicators();
    }
    function applySearch() {
      searchQuery = (document.getElementById('searchInput')?.value || '').trim();
      currentPage = 1;
      loadArticles();
    }
    function clearSearch() {
      searchQuery = '';
      const el = document.getElementById('searchInput');
      if (el) el.value = '';
      currentPage = 1;
      loadArticles();
    }
    async function refreshCosts() {
      try {
        const resp = await fetch('/admin-data/costs');
        if (!resp.ok) return;
        const c = await resp.json();
        const el = document.getElementById('costBadge');
        if (!el) return;
        el.textContent = `Cost est: $${Number(c.estimated_cost_usd_total || 0).toFixed(3)} | 24h: $${Number(c.estimated_cost_usd_24h || 0).toFixed(3)}`;
      } catch (_) {}
    }
    function fmtUtcToTz(iso, tz) {
      const s = String(iso || '').trim();
      if (!s) return '';
      // Stored values may be naive ("2026-02-18T13:44:17.29") or tz-aware ("...+00:00").
      // Force UTC if no offset is present.
      const hasOffset = /[zZ]|[+-]\\d\\d:\\d\\d$/.test(s);
      const d = new Date(hasOffset ? s : (s + 'Z'));
      if (isNaN(d.getTime())) return s;
      return d.toLocaleString('ru-RU', { timeZone: tz || 'Europe/Moscow' });
    }

    async function refreshWorker() {
      try {
        const resp = await fetch('/admin-data/worker-status');
        if (!resp.ok) return;
        const s = await resp.json();
        const el = document.getElementById('workerBadge');
        if (!el) return;
        const tz = (s.tz || 'Europe/Moscow').trim();
        window.NV_TZ = tz || 'Europe/Moscow';
        const next = (s.worker_next_cycle_utc || '').trim();
        const last = (s.worker_last_cycle_finish_utc || '').trim();
        const start = (s.worker_last_cycle_start_utc || '').trim();
        const state = (s.worker_cycle_state || '').trim() || 'unknown';
        const err = (s.worker_last_cycle_error || '').trim();
        if (!next && !last) {
          el.textContent = 'Worker: no heartbeat yet (check pipeline container logs)';
          return;
        }
        const lastLocal = fmtUtcToTz(last, tz);
        const nextLocal = fmtUtcToTz(next, tz);
        const startLocal = fmtUtcToTz(start, tz);
        el.textContent = `Worker(${tz}): state=${state} · last=${lastLocal || '-'} · next=${nextLocal || '-'} · start=${startLocal || '-'}` + (err ? ` · err=${err.slice(0,120)}` : '');
      } catch (_) {}
    }

    function toggleSort(key) {
      if (sortBy !== key) {
        sortBy = key;
        sortDir = 'desc';
      } else if (sortDir === 'desc') {
        sortDir = 'asc';
      } else if (sortDir === 'asc') {
        sortBy = null;
        sortDir = null;
      } else {
        sortBy = key;
        sortDir = 'desc';
      }
      currentPage = 1;
      renderSortIndicators();
      loadArticles();
    }

    function renderSortIndicators() {
      const map = {
        published_at: 'sortIndicatorDate',
        score: 'sortIndicatorScore',
        source: 'sortIndicatorSource',
      };
      for (const id of Object.values(map)) {
        const el = document.getElementById(id);
        if (el) el.textContent = '';
      }
      if (sortBy && map[sortBy]) {
        const el = document.getElementById(map[sortBy]);
        if (el) el.textContent = sortDir === 'asc' ? '▲' : '▼';
      }
    }

    function onPageSizeChange() {
      currentPage = 1;
      loadArticles();
    }

    function onHideDoubleChange() {
      currentPage = 1;
      loadArticles();
    }

    function prevPage() {
      if (currentPage > 1) {
        currentPage -= 1;
        loadArticles();
      }
    }

    function nextPage() {
      if (currentPage < totalPages) {
        currentPage += 1;
        loadArticles();
      }
    }

    function escapeHtml(s) {
      return String(s || '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');
    }

    function setBusy(flag, label) {
      const state = document.getElementById('action_state');
      if (state) state.textContent = flag ? `Running: ${label}...` : 'Ready';
      for (const btn of document.querySelectorAll('header button')) {
        btn.disabled = !!flag;
      }
    }

    let pipelinePollTimer = null;
    function startPipelinePolling(jobId) {
      if (pipelinePollTimer) clearInterval(pipelinePollTimer);
      const wrap = document.getElementById('pipelineProgressWrap');
      const bar = document.getElementById('pipelineProgressBar');
      const text = document.getElementById('pipelineProgressText');
      if (wrap) wrap.style.display = 'block';
      pipelinePollTimer = setInterval(async () => {
        try {
          const resp = await fetch(`/pipeline/jobs/${jobId}`);
          const out = await resp.json();
          document.getElementById('result').textContent = JSON.stringify(out, null, 2);
          const stage = out.stage || out.status || '';
          const detail = out.stage_detail ? ` (${out.stage_detail})` : '';
          const state = document.getElementById('action_state');
          if (state) state.textContent = `Running: pipeline (${stage})${detail}...`;

          const processed = Number(out.processed || 0);
          const total = Number(out.total || 0);
          if (text) {
            if (total > 0) text.textContent = `Pipeline: ${stage} ${processed}/${total}${detail}`;
            else text.textContent = `Pipeline: ${stage}${detail}`;
          }
          if (bar) {
            const pct = total > 0 ? Math.min(100, Math.max(0, (processed / total) * 100)) : 0;
            bar.style.width = `${pct.toFixed(1)}%`;
          }

          if (out.status === 'done' || out.status === 'error') {
            clearInterval(pipelinePollTimer);
            pipelinePollTimer = null;
            setBusy(false, 'pipeline');
            if (wrap) wrap.style.display = 'none';
            if (out.status === 'done') loadArticles();
          }
        } catch (_) {}
      }, 1500);
    }

    async function runPipeline() {
      setBusy(true, 'pipeline');
      setResult('starting pipeline...');
      const resp = await fetch('/pipeline/start', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({backfill_days:1})});
      const out = await resp.json();
      setResult(JSON.stringify(out, null, 2));
      if (!resp.ok || !out.job_id) {
        setBusy(false, 'pipeline');
        return;
      }
      startPipelinePolling(out.job_id);
    }

    async function runScoring() {
      setBusy(true, 'score inbox');
      setResult('starting scoring...');
      const resp = await fetch('/scoring/start', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({limit: 1000})
      });
      const out = await resp.json();
      if (!resp.ok || !out.job_id) {
        setResult(JSON.stringify(out));
        setBusy(false, 'score inbox');
        return;
      }
      setResult(`scoring started: ${out.job_id}`);
      startScorePolling(out.job_id);
    }

    let enrichPollTimer = null;
    async function enrichFullText() {
      setBusy(true, 'enrich full text');
      setResult('starting enrich...');
      const resp = await fetch('/content/enrich/start', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({days_back: 30, limit: 20000})
      });
      const out = await resp.json();
      if (!resp.ok || !out.job_id) {
        setResult(JSON.stringify(out));
        setBusy(false, 'enrich full text');
        return;
      }
      setResult(`enrich started: ${out.job_id} (total=${out.total || 0})`);
      startEnrichPolling(out.job_id);
    }

    function startEnrichPolling(jobId) {
      const wrap = document.getElementById('enrichProgressWrap');
      const bar = document.getElementById('enrichProgressBar');
      const text = document.getElementById('enrichProgressText');
      wrap.style.display = 'block';
      if (enrichPollTimer) clearInterval(enrichPollTimer);
      enrichPollTimer = setInterval(async () => {
        const resp = await fetch(`/content/enrich/jobs/${jobId}`);
        const data = await resp.json();
        if (!resp.ok) {
          text.textContent = 'Enrich error';
          setBusy(false, 'enrich full text');
          clearInterval(enrichPollTimer);
          enrichPollTimer = null;
          return;
        }
        const total = Number(data.total || 0);
        const processed = Number(data.processed || 0);
        const pct = total > 0 ? Math.min(100, Math.round((processed / total) * 100)) : 0;
        bar.style.width = `${pct}%`;
        const upgraded = Number(data.upgraded_to_full || 0);
        const blocked = Number(data.blocked || 0);
        const paywalled = Number(data.paywalled_or_thin || 0);
        const tooShort = Number(data.too_short || 0);
        text.textContent = `Enrich: ${processed}/${total} (${pct}%) | full: ${upgraded} | blocked: ${blocked} | paywall/thin: ${paywalled} | short: ${tooShort}`;
        if (data.status === 'done') {
          setResult(JSON.stringify(data));
          setBusy(false, 'enrich full text');
          clearInterval(enrichPollTimer);
          enrichPollTimer = null;
          loadArticles();
        }
        if (data.status === 'error') {
          setResult(JSON.stringify(data));
          setBusy(false, 'enrich full text');
          clearInterval(enrichPollTimer);
          enrichPollTimer = null;
        }
      }, 1200);
    }

    let prunePollTimer = null;
    async function pruneBad() {
      setBusy(true, 'prune');
      setResult('starting prune...');
      const resp = await fetch('/prune/start', {
        method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({days_back: 365, limit: 500000, archive_summary_only: true, archive_non_ai: true, archive_low_relevance: true})
      });
      const out = await resp.json();
      if (!resp.ok || !out.job_id) {
        setResult(JSON.stringify(out));
        setBusy(false, 'prune');
        return;
      }
      setResult(`prune started: ${out.job_id} (total=${out.total || 0})`);
      startPrunePolling(out.job_id);
    }

    function startPrunePolling(jobId) {
      const wrap = document.getElementById('pruneProgressWrap');
      const bar = document.getElementById('pruneProgressBar');
      const text = document.getElementById('pruneProgressText');
      wrap.style.display = 'block';
      if (prunePollTimer) clearInterval(prunePollTimer);
      prunePollTimer = setInterval(async () => {
        const resp = await fetch(`/prune/jobs/${jobId}`);
        const data = await resp.json();
        if (!resp.ok) {
          text.textContent = 'Prune error';
          setBusy(false, 'prune');
          clearInterval(prunePollTimer);
          prunePollTimer = null;
          return;
        }
        const total = Number(data.total || 0);
        const processed = Number(data.processed || 0);
        const pct = total > 0 ? Math.min(100, Math.round((processed / total) * 100)) : 0;
        bar.style.width = `${pct}%`;
        const archived = Number(data.archived || 0);
        const kept = Number(data.kept || 0);
        const sumOnly = Number(data.summary_only || 0);
        const nonAi = Number(data.non_ai || 0);
        const lowRel = Number(data.low_relevance || 0);
        text.textContent = `Prune: ${processed}/${total} (${pct}%) | archived: ${archived} | kept: ${kept} | summary_only: ${sumOnly} | non_ai: ${nonAi} | low_rel: ${lowRel}`;
        if (data.status === 'done') {
          setResult(JSON.stringify(data));
          setBusy(false, 'prune');
          clearInterval(prunePollTimer);
          prunePollTimer = null;
          loadArticles();
        }
        if (data.status === 'error') {
          setResult(JSON.stringify(data));
          setBusy(false, 'prune');
          clearInterval(prunePollTimer);
          prunePollTimer = null;
        }
      }, 1200);
    }

    function startScorePolling(jobId) {
      const wrap = document.getElementById('scoreProgressWrap');
      const bar = document.getElementById('scoreProgressBar');
      const text = document.getElementById('scoreProgressText');
      wrap.style.display = 'block';
      if (scorePollTimer) clearInterval(scorePollTimer);
      scorePollTimer = setInterval(async () => {
        const resp = await fetch(`/scoring/jobs/${jobId}`);
        const data = await resp.json();
        if (!resp.ok) {
          text.textContent = 'Scoring error';
          setBusy(false, 'score inbox');
          clearInterval(scorePollTimer);
          scorePollTimer = null;
          return;
        }
        const total = Number(data.total || 0);
        const processed = Number(data.processed || 0);
        const pct = total > 0 ? Math.min(100, Math.round((processed / total) * 100)) : 0;
        bar.style.width = `${pct}%`;
        text.textContent = `Scoring: ${processed}/${total} (${pct}%)`;
        if (data.status === 'done') {
          text.textContent = `Scoring done: ${data.scored ?? processed}/${total}`;
          setResult(JSON.stringify(data));
          setBusy(false, 'score inbox');
          clearInterval(scorePollTimer);
          scorePollTimer = null;
          loadArticles();
        }
        if (data.status === 'error') {
          text.textContent = `Scoring failed: ${data.error || 'unknown'}`;
          setResult(JSON.stringify(data));
          setBusy(false, 'score inbox');
          clearInterval(scorePollTimer);
          scorePollTimer = null;
        }
      }, 1200);
    }

    async function aggregateNews() {
      const period = document.getElementById('aggregatePeriod').value || 'month';
      setBusy(true, `sync ${period}`);
      setResult(`sync started: ${period}`);
      try {
        const resp = await fetch('/ingestion/aggregate', {
          method:'POST',
          headers:{'Content-Type':'application/json'},
          body: JSON.stringify({period})
        });
        setResult(JSON.stringify(await resp.json()));
        loadArticles();
      } finally {
        setBusy(false, `sync ${period}`);
      }
    }

    async function rebuildProfile() {
      setBusy(true, 'rebuild profile');
      try {
        const resp = await fetch('/feedback/rebuild-profile', {method:'POST'});
        setResult(JSON.stringify(await resp.json()));
      } finally {
        setBusy(false, 'rebuild profile');
      }
    }

    async function autoSelect() {
      setBusy(true, 'auto select');
      try {
        const resp = await fetch('/selection/auto', {method:'POST'});
        setResult(JSON.stringify(await resp.json()));
      } finally {
        setBusy(false, 'auto select');
      }
    }

    async function telegramTest() {
      setBusy(true, 'telegram test');
      try {
        const resp = await fetch('/telegram/test', {method:'POST'});
        setResult(JSON.stringify(await resp.json()));
      } finally {
        setBusy(false, 'telegram test');
      }
    }

    async function telegramReviewSend() {
      setBusy(true, 'telegram review send');
      try {
        const resp = await fetch('/telegram/review/send-latest', {method:'POST'});
        setResult(JSON.stringify(await resp.json()));
      } finally {
        setBusy(false, 'telegram review send');
      }
    }

    async function telegramReviewPoll() {
      setBusy(true, 'telegram review poll');
      try {
        const resp = await fetch('/telegram/review/poll', {method:'POST'});
        setResult(JSON.stringify(await resp.json()));
      } finally {
        setBusy(false, 'telegram review poll');
      }
    }
    async function telegramBacklog() {
      const n = Number(prompt('Сколько отправить старых выбранных статей подряд?', '10') || '10');
      setBusy(true, 'telegram backlog');
      try {
        const resp = await fetch(`/telegram/review/send-backlog?limit=${Math.max(1, Math.min(100, n || 10))}`, {method:'POST'});
        setResult(JSON.stringify(await resp.json()));
      } finally {
        setBusy(false, 'telegram backlog');
      }
    }

    async function prepare(id) {
      const resp = await fetch(`/articles/${id}/post/generate`, {method:'POST'});
      const data = await resp.json();
      if (!resp.ok) {
        setResult(JSON.stringify(data));
        alert('Generate Post failed: ' + (data.detail || 'unknown error'));
        return;
      }
      setResult(JSON.stringify(data));
      openPreview(id, data);
      loadArticles();
    }

    async function translateArticle(id) {
      const resp = await fetch(`/articles/${id}/translate`, {method:'POST'});
      const data = await resp.json();
      if (!resp.ok) {
        setResult(JSON.stringify(data));
        alert('Translate failed: ' + (data.detail || 'unknown error'));
        return;
      }
      document.getElementById('previewTitle').textContent = (data.ru_title || data.title || '') + ` [id=${id}]`;
      document.getElementById('previewText').textContent = data.ru_translation || '';
      document.getElementById('previewModal').style.display = 'block';
      setResult(JSON.stringify(data));
    }

    async function translateFullArticle(id) {
      const resp = await fetch(`/articles/${id}/translate-full`, {method:'POST'});
      const data = await resp.json();
      if (!resp.ok) {
        setResult(JSON.stringify(data));
        alert('Translate Full failed: ' + (data.detail || 'unknown error'));
        return;
      }
      document.getElementById('previewTitle').textContent = (data.ru_title || data.title || '') + ` [id=${id}]`;
      document.getElementById('previewText').textContent = data.ru_translation || '';
      document.getElementById('previewModal').style.display = 'block';
      setResult(JSON.stringify(data));
    }

    async function openPreview(id, preparedData=null) {
      let details = preparedData;
      if (!details || !details.post_preview) {
        const resp = await fetch(`/articles/${id}`);
        details = await resp.json();
      }
      document.getElementById('previewTitle').textContent = (details.ru_title || details.title || '') + ` [id=${id}]`;
      document.getElementById('previewText').textContent = details.post_preview || '';
      document.getElementById('previewModal').style.display = 'block';
    }

    function closePreview() {
      document.getElementById('previewModal').style.display = 'none';
    }

    async function publish(id) {
      const feedback = prompt('Почему выбрал эту новость?');
      if (feedback && feedback.trim().length >= 5) {
        await fetch(`/articles/${id}/feedback`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({explanation_text: feedback.trim()})});
      }
      const resp = await fetch(`/articles/${id}/publish`, {method:'POST'});
      setResult(JSON.stringify(await resp.json()));
      loadArticles();
    }

    async function archiveArticle(id) {
      const resp = await fetch(`/articles/${id}/status`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({status:'rejected'})});
      setResult(JSON.stringify(await resp.json()));
      loadArticles();
    }

    async function selectHour(id) {
      const resp = await fetch(`/articles/${id}/status`, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({status:'selected_hourly'})});
      setResult(JSON.stringify(await resp.json()));
      loadArticles();
    }

    async function selectDay(id) {
      const resp = await fetch(`/articles/${id}/select-day`, {method:'POST'});
      setResult(JSON.stringify(await resp.json()));
      loadArticles();
    }

    async function unselectDay(id) {
      const resp = await fetch(`/articles/${id}/unselect-day`, {method:'POST'});
      setResult(JSON.stringify(await resp.json()));
      loadArticles();
    }

    async function unselectHour(id) {
      const resp = await fetch(`/articles/${id}/unselect-hour`, {method:'POST'});
      setResult(JSON.stringify(await resp.json()));
      loadArticles();
    }

    async function deleteArticle(id) {
      const reason = prompt(`Почему вы хотите удалить статью ${id}?`);
      if (!reason || reason.trim().length < 5) {
        alert('Нужна причина удаления (минимум 5 символов).');
        return;
      }
      const row = document.getElementById(`row-${id}`);
      if (row) row.remove();
      setResult(`Удалено из списка (UI): ${id}. Удаляю в БД...`);
      const resp = await fetch(`/articles/${id}`, {
        method:'DELETE',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({reason: reason.trim()})
      });
      const out = await resp.json();
      setResult(JSON.stringify(out));
      if (!resp.ok) {
        alert('Удаление в БД не выполнено: ' + (out.detail || 'unknown error'));
      }
    }

    async function restoreArticle(id) {
      const row = document.getElementById(`row-${id}`);
      if (row) row.remove();
      setResult(`Восстанавливаю: ${id}...`);
      const resp = await fetch(`/articles/${id}/restore`, { method:'POST' });
      const out = await resp.json();
      setResult(JSON.stringify(out));
      if (!resp.ok) {
        alert('Восстановление не выполнено: ' + (out.detail || 'unknown error'));
        loadArticles();
      }
    }

    function setResult(v) {
      document.getElementById('result').textContent = v;
    }

    document.addEventListener('DOMContentLoaded', () => {
      const s = document.getElementById('searchInput');
      if (s) {
        s.addEventListener('keydown', (e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            applySearch();
          }
        });
      }
    });
    loadArticles();
    refreshCosts();
    refreshWorker();
    setInterval(refreshWorker, 10000);
  </script>
</body>
</html>
""".replace("__VIEW__", view)


@app.get("/article/{article_id}", response_class=HTMLResponse)
@app.get("/admin/article/{article_id}", response_class=HTMLResponse)
def admin_article_page(article_id: int, request: Request):
    if _get_session_user(request) is None:
        return RedirectResponse(url="/login", status_code=303)
    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            raise HTTPException(status_code=404, detail="Article not found")
        score = session.get(Score, article.id)
        today = date.today()
        is_selected_day = bool(
            session.scalar(
                select(DailySelection.id).where(
                    DailySelection.article_id == article_id,
                    DailySelection.selected_date == today,
                    DailySelection.active.is_(True),
                )
            )
        )
        image_prompt = _latest_image_prompt(session, article_id)
        latest_feedback = session.scalars(
            select(EditorFeedback.explanation_text)
            .where(EditorFeedback.article_id == article_id)
            .order_by(EditorFeedback.created_at.desc())
            .limit(1)
        ).first()

    image_web = ""
    image_raw = article.generated_image_path or ""
    if image_raw:
        image_web = image_raw
        if image_web.startswith(("http://", "https://")):
            pass
        elif image_web.startswith("app/static/"):
            image_web = "/static/" + image_web.removeprefix("app/static/")
        elif not image_web.startswith("/"):
            image_web = "/" + image_web

    details = {
        "id": article.id,
        "status": article.status,
        "content_mode": article.content_mode,
        "title": article.title,
        "subtitle": article.subtitle,
        "ru_title": article.ru_title or "",
        "ru_summary": article.ru_summary or "",
        "short_hook": article.short_hook or "",
        "text": article.text,
        "source_id": article.source_id,
        "published_at": article.published_at,
        "canonical_url": article.canonical_url,
        "scheduled_publish_at": _dt_to_utc_z(article.scheduled_publish_at),
        "image": image_raw,
        "image_web": image_web,
        "score": score.final_score if score else None,
        "score_10": _score_to_10(score),
        "score_reasoning": score.reasoning if score else "",
        "post_preview": _build_post_preview_text(article),
        "image_prompt": image_prompt,
        "feedback": latest_feedback or "",
        "is_selected_day": is_selected_day,
    }
    payload = json.dumps(details, default=str).replace("</", "<\\/")
    return f"""
<!doctype html>
<html lang='ru'>
<head>
<meta charset='utf-8'>
<meta name='viewport' content='width=device-width, initial-scale=1'>
<title>Article {article_id}</title>
<link rel="stylesheet" href="/static/app.css?v=3">
</head>
<body>
  <main class="nv-container">
	  <p><a href='/'>← back</a></p>
	  <div class='card'>
	    <div id='meta'></div>
    <p><a id='sourceLink' target='_blank'>original source</a></p>
    <p id='score'></p>
  </div>
  <div class='card'>
    <h3>1) English Full</h3>
    <h2 id='title'></h2>
    <textarea id='en_full_text' style='min-height:320px' placeholder='Full English text'></textarea>
    <p>
      <button id='btn_save_en_full' onclick='saveManualText()'>Save English Full</button>
      <button onclick='pullFromSource()'>Read From Site</button>
    </p>
  </div>
  <div class='card'>
    <h3>2) English Short</h3>
    <p class='muted'>RSS subtitle/summary</p>
    <textarea id='en_short' style='min-height:120px' readonly></textarea>
  </div>
  <div class='card'>
    <h3>3) Russian Full</h3>
    <p class='muted'>Полный перевод статьи для чтения (опционально). Для поста не обязателен.</p>
    <textarea id='ru_full_text' style='min-height:240px' placeholder='Нажми Translate Full'></textarea>
    <p>
      <button onclick='translateFull()'>Translate Full</button>
    </p>
  </div>
  <div class='card'>
    <h3>4) Russian Short</h3>
    <p class='muted'>Короткая версия для поста: заголовок + 2 абзаца. Нажми Generate Post — создаст из английских title/subtitle/text.</p>
    <p class='muted'>RU Title</p>
    <textarea id='ru_title_edit' style='min-height:70px'></textarea>
    <p class='muted'>RU Summary</p>
    <textarea id='ru_summary_edit' style='min-height:220px'></textarea>
    <p>
      <button onclick='prepare()'>Generate Post</button>
      <button onclick='translateArticle()'>Translate Preview</button>
      <button id='btn_save_ru' onclick='saveRuText()'>Save RU Text</button>
    </p>
  </div>
  <div class='card'>
    <h3>5) Image Prompt</h3>
    <p class='muted'>1) Generate Image Prompt 2) Edit prompt 3) Generate Picture (landscape)</p>
    <textarea id='image_prompt' style='min-height:110px' placeholder='Image prompt'></textarea>
    <p>
      <button onclick='generateImagePrompt()'>Generate Image Prompt</button>
      <button id='btn_save_prompt' onclick='saveImagePrompt()'>Save Prompt</button>
      <button onclick='generatePicture()'>Generate Picture</button>
    </p>
    <p class='muted'>Или загрузи свою картинку для публикации:</p>
    <p>
      <input id='upload_image_file' type='file' accept='image/*' />
      <button onclick='uploadPicture()'>Upload Image</button>
    </p>
  </div>
  <div class='card'>
    <h3>6) Post + Image</h3>
    <p class='muted'>Формат Telegram: заголовок + текст + Подробнее + @neuro_vibes_future</p>
    <pre id='post_preview'></pre>
    <p id='image'></p>
    <p class='muted'>Загрузить свою картинку:</p>
    <p>
      <input id='upload_image_file_ru' type='file' accept='image/*' />
      <button onclick='uploadPicture()'>Upload Image</button>
    </p>
  </div>
  <div class='card'>
    <h3>Actions</h3>
    <p id='action_state' style='color:#9ec5ff;margin-top:0;'>Ready</p>
    <p class='muted'>Отложенная публикация (локальное время из Setup → Telegram timezone):</p>
    <p>
      <input id='schedule_at' type='datetime-local' />
      <button onclick='schedulePublish()'>Schedule</button>
      <button onclick='unschedulePublish()'>Clear Schedule</button>
    </p>
    <div style='display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;'>
      <button onclick='pullFromSource()'>Read From Site</button>
      <button onclick='scoreNow()'>Score</button>
      <button onclick='prepareWithImage()'>Prepare + Image</button>
      <button onclick='translateFull()'>Translate Full</button>
      <button onclick='translateArticle()'>Translate Preview</button>
      <button onclick='prepare()'>Generate Post</button>
    </div>
    <div style='display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;'>
      <button id='btn_day_toggle' onclick='toggleDaySelection()'>Select Day</button>
      <button id='btn_hour_toggle' onclick='toggleHourSelection()'>Select Hour</button>
      <button onclick="setStatus('selected_hourly')">Mark Selected</button>
    </div>
    <div style='display:flex;gap:8px;flex-wrap:wrap;'>
      <button onclick='archiveArticle()'>Archive</button>
      <button onclick='deleteArticle()'>Delete</button>
      <button onclick='publish()'>Publish</button>
    </div>
  </div>
  <div class='card'>
    <h3>Why selected</h3>
    <textarea id='feedback' placeholder='Почему выбрана именно эта новость'></textarea>
    <p><button onclick='saveFeedback()'>Save Feedback</button></p>
    <pre id='result' style='margin-top:8px;color:#9ec5ff;'></pre>
  </div>
</main>
<script>
const data = {payload};
const id = data.id;
function toLocalInputValue(v) {{
  if (!v) return '';
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return '';
  const p = (n) => String(n).padStart(2, '0');
  return `${{d.getFullYear()}}-${{p(d.getMonth()+1)}}-${{p(d.getDate())}}T${{p(d.getHours())}}:${{p(d.getMinutes())}}`;
}}
function toLocalDisplay(v) {{
  if (!v) return '';
  const d = new Date(v);
  if (Number.isNaN(d.getTime())) return String(v);
  const p = (n) => String(n).padStart(2, '0');
  return `${{p(d.getDate())}}.${{p(d.getMonth()+1)}}.${{d.getFullYear()}} ${{p(d.getHours())}}:${{p(d.getMinutes())}}`;
}}
function renderMeta(d) {{
  const sched = d.scheduled_publish_at ? ` | scheduled_at: ${{toLocalDisplay(d.scheduled_publish_at)}}` : '';
  document.getElementById('meta').innerHTML = `ID: ${{d.id}} | status: ${{d.status}} | content: ${{d.content_mode || 'summary_only'}} | source_id: ${{d.source_id}} | published_at: ${{d.published_at || '-'}}${{sched}}`;
}}
renderMeta(data);
document.getElementById('sourceLink').href = data.canonical_url;
document.getElementById('sourceLink').textContent = data.canonical_url;
document.getElementById('score').textContent = `score: ${{data.score_10 ?? 'not scored'}}/10 | reasoning: ${{data.score_reasoning || '-'}}`;
document.getElementById('title').textContent = data.title || '';
document.getElementById('en_full_text').value = data.text || '';
document.getElementById('en_short').value = data.subtitle || '';
document.getElementById('ru_full_text').value = '';
document.getElementById('ru_title_edit').value = data.ru_title || '';
document.getElementById('ru_summary_edit').value = data.ru_summary || '';
document.getElementById('feedback').value = data.feedback || '';
document.getElementById('post_preview').textContent = data.post_preview || '';
document.getElementById('image_prompt').value = data.image_prompt || '';
document.getElementById('image').innerHTML = data.image_web ? `Image: <a target='_blank' href='${{data.image_web}}'>${{data.image}}</a><br><img src='${{data.image_web}}' style='max-width:420px;border-radius:8px;margin-top:8px'>` : 'No generated image';
document.getElementById('schedule_at').value = toLocalInputValue(data.scheduled_publish_at);
const originalTexts = {{
  en_full_text: document.getElementById('en_full_text').value || '',
  ru_title_edit: document.getElementById('ru_title_edit').value || '',
  ru_summary_edit: document.getElementById('ru_summary_edit').value || '',
  image_prompt: document.getElementById('image_prompt').value || '',
}};

function setResult(v) {{ document.getElementById('result').textContent = typeof v === 'string' ? v : JSON.stringify(v, null, 2); }}
function setBusy(flag, label) {{
  const state = document.getElementById('action_state');
  if (state) state.textContent = flag ? `Running: ${{label}}...` : 'Ready';
  for (const btn of document.querySelectorAll('button')) {{
    btn.disabled = !!flag;
  }}
  if (!flag) {{
    try {{ updateSaveButtons(); }} catch (_) {{}}
  }}
}}
function updateActionToggles() {{
  const dayBtn = document.getElementById('btn_day_toggle');
  const hourBtn = document.getElementById('btn_hour_toggle');
  if (dayBtn) {{
    dayBtn.textContent = data.is_selected_day ? 'Remove Day' : 'Select Day';
  }}
  if (hourBtn) {{
    const isHour = String(data.status || '').toUpperCase() === 'SELECTED_HOURLY';
    hourBtn.textContent = isHour ? 'Remove Hour' : 'Select Hour';
  }}
}}
function updateSaveButtons() {{
  const en = (document.getElementById('en_full_text').value || '').trim();
  const ruTitle = (document.getElementById('ru_title_edit').value || '').trim();
  const ruSummary = (document.getElementById('ru_summary_edit').value || '').trim();
  const prompt = (document.getElementById('image_prompt').value || '').trim();
  const enBtn = document.getElementById('btn_save_en_full');
  const ruBtn = document.getElementById('btn_save_ru');
  const prBtn = document.getElementById('btn_save_prompt');
  if (enBtn) enBtn.disabled = en === (originalTexts.en_full_text || '').trim();
  if (ruBtn) ruBtn.disabled = ruTitle === (originalTexts.ru_title_edit || '').trim() && ruSummary === (originalTexts.ru_summary_edit || '').trim();
  if (prBtn) prBtn.disabled = prompt === (originalTexts.image_prompt || '').trim();
}}
function showError(out) {{
  const raw = (out && (out.detail || out.error || out.message)) ? (out.detail || out.error || out.message) : 'Request failed';
  if (String(raw).includes('score_required_before_content')) {{
    alert('Нужно проскорить статью перед Generate Post/переводом/генерацией. Нажми кнопку Score на этой странице (или Score Inbox в списке).');
    return;
  }}
  if (String(raw).includes('article_not_ai_relevant_enough')) {{
    alert('Статья не проходит AI-релевантность (по скорингу). Такое мы не переводим/не публикуем.');
    return;
  }}
  alert(String(raw));
}}
async function requestJson(url, options) {{
  let resp;
  try {{
    resp = await fetch(url, options || {{}});
  }} catch (err) {{
    return {{ ok: false, out: {{ detail: `network_error: ${{err?.message || err}}` }} }};
  }}
  const text = await resp.text();
  let out = {{}};
  try {{
    out = text ? JSON.parse(text) : {{}};
  }} catch (_) {{
    out = {{ detail: text || `http_${{resp.status}}` }};
  }}
  return {{ ok: resp.ok, out }};
}}

async function prepare() {{
  setBusy(true, 'generate post');
  try {{
    const {{ ok, out }} = await requestJson(`/articles/${{id}}/post/generate`, {{method:'POST'}});
    setResult(out);
    if (!ok) return showError(out);
    if (out.post_preview) document.getElementById('post_preview').textContent = out.post_preview;
    if (out.ru_title) document.getElementById('ru_title_edit').value = out.ru_title;
    if (out.ru_summary) document.getElementById('ru_summary_edit').value = out.ru_summary;
    updateSaveButtons();
  }} finally {{
    setBusy(false, 'generate post');
  }}
}}

async function prepareWithImage() {{
  setBusy(true, 'prepare + image');
  try {{
    const {{ ok, out }} = await requestJson(`/articles/${{id}}/prepare`, {{method:'POST'}});
    setResult(out);
    if (!ok) return showError(out);
    if (out.post_preview) document.getElementById('post_preview').textContent = out.post_preview;
    if (out.ru_title) document.getElementById('ru_title_edit').value = out.ru_title;
    if (out.ru_summary) document.getElementById('ru_summary_edit').value = out.ru_summary;
    updateSaveButtons();
    if (out.image_path) {{
      const path = out.image_path.startsWith('app/static/') ? ('/static/' + out.image_path.replace('app/static/','')) : out.image_path;
      document.getElementById('image').innerHTML = `Image: <a target='_blank' href='${{path}}'>${{out.image_path}}</a><br><img src='${{path}}' style='max-width:640px;border-radius:8px;margin-top:8px'>`;
    }}
  }} finally {{
    setBusy(false, 'prepare + image');
  }}
}}

async function saveRuText() {{
  const ru_title = document.getElementById('ru_title_edit').value.trim();
  const ru_summary = document.getElementById('ru_summary_edit').value.trim();
  if (ru_title.length < 1) {{
    setResult('RU title is required');
    return;
  }}
  if (ru_summary.length < 10) {{
    setResult('RU summary too short');
    return;
  }}
  setBusy(true, 'save ru text');
  try {{
    const {{ ok, out }} = await requestJson(`/articles/${{id}}/ru/save`, {{
      method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{ru_title, ru_summary}})
    }});
    setResult(out);
    if (!ok) return showError(out);
    if (out.post_preview) document.getElementById('post_preview').textContent = out.post_preview;
    originalTexts.ru_title_edit = ru_title;
    originalTexts.ru_summary_edit = ru_summary;
    updateSaveButtons();
  }} finally {{
    setBusy(false, 'save ru text');
  }}
}}

async function translateArticle() {{
  setBusy(true, 'translate');
  try {{
    const {{ ok, out }} = await requestJson(`/articles/${{id}}/translate`, {{method:'POST'}});
    setResult(out);
    if (!ok) return showError(out);
    if (out.ru_title) {{
      document.getElementById('ru_title_edit').value = out.ru_title;
    }}
    if (out.ru_translation) {{
      document.getElementById('ru_summary_edit').value = out.ru_translation;
    }}
    updateSaveButtons();
  }} finally {{
    setBusy(false, 'translate');
  }}
}}

async function generateImagePrompt() {{
  setBusy(true, 'generate image prompt');
  try {{
    const {{ ok, out }} = await requestJson(`/articles/${{id}}/image-prompt/generate`, {{method:'POST'}});
    setResult(out);
    if (!ok) return showError(out);
    document.getElementById('image_prompt').value = out.image_prompt || '';
    updateSaveButtons();
  }} finally {{
    setBusy(false, 'generate image prompt');
  }}
}}

async function saveImagePrompt() {{
  const prompt = document.getElementById('image_prompt').value.trim();
  if (prompt.length < 10) {{ setResult('Prompt too short'); return; }}
  setBusy(true, 'save image prompt');
  try {{
    const {{ ok, out }} = await requestJson(`/articles/${{id}}/image-prompt/save`, {{
      method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{prompt}})
    }});
    setResult(out);
    if (!ok) showError(out);
    originalTexts.image_prompt = prompt;
    updateSaveButtons();
  }} finally {{
    setBusy(false, 'save image prompt');
  }}
}}

async function generatePicture() {{
  setBusy(true, 'generate picture');
  try {{
    const prompt = document.getElementById('image_prompt').value.trim();
    if (prompt.length >= 10) {{
      await requestJson(`/articles/${{id}}/image-prompt/save`, {{
        method:'POST',
        headers:{{'Content-Type':'application/json'}},
        body: JSON.stringify({{prompt}})
      }});
    }}
    const {{ ok, out }} = await requestJson(`/articles/${{id}}/picture/generate`, {{method:'POST'}});
    setResult(out);
    if (!ok) return showError(out);
    if (out.image_path) {{
      const path = out.image_path.startsWith('app/static/') ? ('/static/' + out.image_path.replace('app/static/','')) : out.image_path;
      document.getElementById('image').innerHTML = `Image: <a target='_blank' href='${{path}}'>${{out.image_path}}</a><br><img src='${{path}}' style='max-width:640px;border-radius:8px;margin-top:8px'>`;
    }}
  }} finally {{
    setBusy(false, 'generate picture');
  }}
}}

async function uploadPicture() {{
  const inputRu = document.getElementById('upload_image_file_ru');
  const inputLegacy = document.getElementById('upload_image_file');
  const input = (inputRu && inputRu.files && inputRu.files.length) ? inputRu : inputLegacy;
  if (!input || !input.files || !input.files.length) {{
    setResult('Choose image file first');
    return;
  }}
  setBusy(true, 'upload picture');
  try {{
    const fd = new FormData();
    fd.append('image', input.files[0]);
    const {{ ok, out }} = await requestJson(`/articles/${{id}}/picture/upload`, {{
      method:'POST',
      body: fd
    }});
    setResult(out);
    if (!ok) return showError(out);
    if (out.image_path) {{
      const path = out.image_path.startsWith('app/static/') ? ('/static/' + out.image_path.replace('app/static/','')) : out.image_path;
      document.getElementById('image').innerHTML = `Image: <a target='_blank' href='${{path}}'>${{out.image_path}}</a><br><img src='${{path}}' style='max-width:640px;border-radius:8px;margin-top:8px'>`;
    }}
  }} finally {{
    setBusy(false, 'upload picture');
  }}
}}

async function translateFull() {{
  setBusy(true, 'translate full');
  try {{
    const {{ ok, out }} = await requestJson(`/articles/${{id}}/translate-full`, {{method:'POST'}});
    setResult(out);
    if (!ok) return showError(out);
    document.getElementById('ru_full_text').value = out.ru_translation || '';
    if (out.ru_title) {{
      document.getElementById('ru_title_edit').value = out.ru_title;
    }}
    updateSaveButtons();
  }} finally {{
    setBusy(false, 'translate full');
  }}
}}

async function saveManualText() {{
  const text = document.getElementById('en_full_text').value.trim();
  if (text.length < 50) {{
    setResult('Text too short');
    return;
  }}
  setBusy(true, 'save full text');
  try {{
    const {{ ok, out }} = await requestJson(`/articles/${{id}}/text/override`, {{
      method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{text}})
    }});
    setResult(out);
    if (!ok) return showError(out);
    document.getElementById('en_full_text').value = text;
    originalTexts.en_full_text = text;
    updateSaveButtons();
  }} finally {{
    setBusy(false, 'save full text');
  }}
}}

async function pullFromSource() {{
  setBusy(true, 'pull content');
  try {{
    const {{ ok, out }} = await requestJson(`/articles/${{id}}/content/pull`, {{method:'POST'}});
    setResult(out);
    if (!ok) return showError(out);
    if (out.updated) {{
      const resp = await requestJson(`/articles/${{id}}`, {{method:'GET'}});
      if (resp.ok && resp.out) {{
        const d = resp.out;
        document.getElementById('en_full_text').value = d.text || '';
        renderMeta(d);
      }}
    }}
  }} finally {{
    setBusy(false, 'pull content');
  }}
}}

async function scoreNow() {{
  setBusy(true, 'score article');
  try {{
    const {{ ok, out }} = await requestJson(`/articles/${{id}}/score`, {{method:'POST'}});
    setResult(out);
    if (!ok) return showError(out);
    const resp = await requestJson(`/articles/${{id}}`, {{method:'GET'}});
    if (resp.ok && resp.out) {{
      const d = resp.out;
      renderMeta(d);
      document.getElementById('score').textContent = `score: ${{d.score_10 ?? 'not scored'}}/10 | reasoning: ${{d.score_reasoning || '-'}}`;
      if (d.status) data.status = d.status;
      updateActionToggles();
    }}
  }} finally {{
    setBusy(false, 'score article');
  }}
}}

async function selectDay() {{
  setBusy(true, 'select day');
  try {{
    const {{ ok, out }} = await requestJson(`/articles/${{id}}/select-day`, {{method:'POST'}});
    setResult(out);
    if (!ok) showError(out);
  }} finally {{
    setBusy(false, 'select day');
  }}
}}

async function unselectDay() {{
  setBusy(true, 'remove day');
  try {{
    const {{ ok, out }} = await requestJson(`/articles/${{id}}/unselect-day`, {{method:'POST'}});
    setResult(out);
    if (!ok) showError(out);
  }} finally {{
    setBusy(false, 'remove day');
  }}
}}

async function unselectHour() {{
  setBusy(true, 'remove hour');
  try {{
    const {{ ok, out }} = await requestJson(`/articles/${{id}}/unselect-hour`, {{method:'POST'}});
    setResult(out);
    if (!ok) showError(out);
  }} finally {{
    setBusy(false, 'remove hour');
  }}
}}

async function toggleDaySelection() {{
  if (data.is_selected_day) {{
    await unselectDay();
    data.is_selected_day = false;
  }} else {{
    await selectDay();
    data.is_selected_day = true;
  }}
  updateActionToggles();
}}

async function toggleHourSelection() {{
  const isHour = String(data.status || '').toUpperCase() === 'SELECTED_HOURLY';
  if (isHour) {{
    await unselectHour();
    data.status = 'scored';
  }} else {{
    await setStatus('selected_hourly');
    data.status = 'selected_hourly';
  }}
  renderMeta(data);
  updateActionToggles();
}}

async function schedulePublish() {{
  const v = (document.getElementById('schedule_at').value || '').trim();
  if (!v) {{
    setResult('Укажи дату и время публикации');
    return;
  }}
  setBusy(true, 'schedule publish');
  try {{
    const {{ ok, out }} = await requestJson(`/articles/${{id}}/schedule-publish`, {{
      method:'POST',
      headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{publish_at: v}})
    }});
    setResult(out);
    if (!ok) return showError(out);
    const resp = await requestJson(`/articles/${{id}}`, {{method:'GET'}});
    if (resp.ok && resp.out) {{
      renderMeta(resp.out);
      document.getElementById('schedule_at').value = toLocalInputValue(resp.out.scheduled_publish_at);
    }}
  }} finally {{
    setBusy(false, 'schedule publish');
  }}
}}

async function unschedulePublish() {{
  setBusy(true, 'clear schedule');
  try {{
    const {{ ok, out }} = await requestJson(`/articles/${{id}}/unschedule-publish`, {{method:'POST'}});
    setResult(out);
    if (!ok) return showError(out);
    const resp = await requestJson(`/articles/${{id}}`, {{method:'GET'}});
    if (resp.ok && resp.out) {{
      renderMeta(resp.out);
      document.getElementById('schedule_at').value = '';
    }}
  }} finally {{
    setBusy(false, 'clear schedule');
  }}
}}

async function publish() {{
  setBusy(true, 'publish');
  try {{
    const {{ ok, out }} = await requestJson(`/articles/${{id}}/publish`, {{method:'POST'}});
    setResult(out);
    if (!ok) showError(out);
  }} finally {{
    setBusy(false, 'publish');
  }}
}}

async function archiveArticle() {{
  setBusy(true, 'archive');
  try {{
    const {{ ok, out }} = await requestJson(`/articles/${{id}}/status`, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{status:'rejected'}})}});
    setResult(out);
    if (!ok) return showError(out);
    data.status = 'rejected';
    renderMeta(data);
    updateActionToggles();
  }} finally {{
    setBusy(false, 'archive');
  }}
}}

async function setStatus(status) {{
  setBusy(true, `set status ${{status}}`);
  try {{
    const {{ ok, out }} = await requestJson(`/articles/${{id}}/status`, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{status}})}});
    setResult(out);
    if (!ok) showError(out);
  }} finally {{
    setBusy(false, `set status ${{status}}`);
  }}
}}

async function saveFeedback() {{
  const text = document.getElementById('feedback').value.trim();
  if (text.length < 5) {{ setResult('Feedback too short'); return; }}
  setBusy(true, 'save feedback');
  try {{
    const {{ ok, out }} = await requestJson(`/articles/${{id}}/feedback`, {{method:'POST', headers:{{'Content-Type':'application/json'}}, body: JSON.stringify({{explanation_text:text}})}});
    setResult(out);
    if (!ok) showError(out);
  }} finally {{
    setBusy(false, 'save feedback');
  }}
}}

async function deleteArticle() {{
  const reason = prompt(`Почему вы хотите удалить статью ${{id}}?`);
  if (!reason || reason.trim().length < 5) {{
    alert('Нужна причина удаления (минимум 5 символов).');
    return;
  }}
  setBusy(true, 'delete article');
  try {{
    const {{ ok, out }} = await requestJson(`/articles/${{id}}`, {{
      method:'DELETE',
      headers:{{'Content-Type':'application/json'}},
      body: JSON.stringify({{reason: reason.trim()}})
    }});
    setResult(out);
    if (!ok) return showError(out);
    data.status = 'archived';
    renderMeta(data);
    updateActionToggles();
  }} finally {{
    setBusy(false, 'delete article');
  }}
}}
updateActionToggles();
document.getElementById('en_full_text').addEventListener('input', updateSaveButtons);
document.getElementById('ru_title_edit').addEventListener('input', updateSaveButtons);
document.getElementById('ru_summary_edit').addEventListener('input', updateSaveButtons);
document.getElementById('image_prompt').addEventListener('input', updateSaveButtons);
updateSaveButtons();
</script>
</body>
</html>
"""


def _serialize_article(article: Article, score: Score | None, source: Source | None) -> dict:
    final_score = score.final_score if score else None
    return {
        "id": article.id,
        "status": article.status,
        "content_mode": article.content_mode,
        "double_of_article_id": article.double_of_article_id,
        "title": article.title,
        "subtitle": article.subtitle,
        "ru_title": article.ru_title,
        "short_hook": article.short_hook,
        "source_id": article.source_id,
        "source_name": source.name if source else None,
        "published_at": article.published_at,
        "created_at": article.created_at,
        "final_score": final_score,
        "score_10": _score_to_10(score),
        "canonical_url": article.canonical_url,
        "generated_image_path": article.generated_image_path,
        "scheduled_publish_at": _dt_to_utc_z(article.scheduled_publish_at),
    }


def _build_post_preview_text(article: Article) -> str:
    title = (article.ru_title or "").strip()
    summary = (article.ru_summary or "").strip()
    url = (article.canonical_url or "").strip()
    if not title or not summary:
        return "RU текст не готов. Нажми Generate Post, при необходимости отредактируй и затем Save RU Text."
    parts = [
        f"<b>{title}</b>" if title else "",
        summary,
        f'<a href="{url}">Подробнее</a>',
        settings.telegram_signature or "@neuro_vibes_future",
    ]
    return "\n\n".join([p for p in parts if p])


def _dt_to_utc_z(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        aware = dt.replace(tzinfo=timezone.utc)
    else:
        aware = dt.astimezone(timezone.utc)
    # `Z` makes JS Date parsing deterministic (UTC).
    return aware.isoformat(timespec="seconds").replace("+00:00", "Z")


def _score_to_10(score: Score | None) -> int | None:
    if score is None or score.final_score is None:
        return None
    raw = max(0.0, min(10.0, float(score.final_score) * 10.0))

    # Editorial calibration:
    # - lift mid-range (fillers) to human-friendly 5-7 band
    # - stretch top stories into 8-10 when relevance/virality/business_IT are strong
    features = score.features if isinstance(score.features, dict) else {}
    relevance = max(0.0, min(1.0, float(score.relevance or 0.0) / 10.0))
    virality = max(0.0, min(1.0, float(score.virality or 0.0) / 10.0))
    business_it = max(0.0, min(1.0, float(features.get("business_it", 0.5))))
    domain = str(features.get("domain") or "").strip().lower()
    geek_penalty = max(0.6, min(1.0, float(features.get("geek_penalty", 1.0))))

    audience_fit = (0.45 * relevance) + (0.25 * virality) + (0.30 * business_it)
    base = raw + 1.4
    fit_adjust = (audience_fit - 0.5) * 1.0  # approx -0.5..+0.5
    hot_bonus = 0.7 if (relevance >= 0.85 and virality >= 0.70) else 0.0

    low_rel_penalty = 0.0
    if relevance < 0.50:
        low_rel_penalty = -1.2
    elif relevance < 0.60:
        low_rel_penalty = -0.6

    value = base + fit_adjust + hot_bonus + low_rel_penalty
    if domain == "research":
        value -= 1.6
    if business_it < 0.65:
        value -= 0.7
    value *= geek_penalty
    value = max(0.0, min(10.0, value))
    return int(round(value))
