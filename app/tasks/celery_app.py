from __future__ import annotations

from celery import Celery

from app.core.config import settings


celery_app = Celery(
    "neurovibes",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_default_queue="news_ingestion",
    beat_schedule={
        "hourly-ingestion": {
            "task": "app.tasks.celery_tasks.ingestion_tasks.enqueue_hourly_fetch",
            "schedule": 3600.0,
        }
    },
)
