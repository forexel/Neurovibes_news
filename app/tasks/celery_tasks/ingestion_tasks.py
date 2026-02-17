from __future__ import annotations

from sqlalchemy import select

from app.db import init_db, session_scope
from app.models import Source
from app.services.ingestion import fetch_source_articles, run_backfill_batched
from app.tasks.celery_app import celery_app


@celery_app.task(bind=True, autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={"max_retries": 3})
def fetch_source_task(self, source_id: int, days_back: int = 1) -> dict:
    init_db()
    with session_scope() as session:
        source = session.get(Source, source_id)
    if not source:
        return {"ok": False, "error": "source_not_found"}
    inserted = fetch_source_articles(source, days_back=days_back)
    return {"ok": True, "source_id": source_id, "inserted": inserted}


@celery_app.task(bind=True, autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={"max_retries": 2})
def enqueue_hourly_fetch(self) -> dict:
    init_db()
    with session_scope() as session:
        sources = session.scalars(select(Source).where(Source.is_active.is_(True)).order_by(Source.priority_rank.asc())).all()

    jobs = [fetch_source_task.delay(s.id, 1).id for s in sources]
    return {"ok": True, "jobs": jobs, "count": len(jobs)}


@celery_app.task(bind=True, autoretry_for=(Exception,), retry_backoff=True, retry_kwargs={"max_retries": 2})
def run_backfill_task(self, total_days: int = 30, batch_days: int = 3) -> dict:
    init_db()
    batches = run_backfill_batched(total_days=total_days, batch_days=batch_days)
    return {"ok": True, "batches": batches}
