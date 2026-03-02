from __future__ import annotations

import os
from datetime import datetime
from html import escape

import httpx
from sqlalchemy import select

from app.core.config import settings
from app.db import session_scope
from app.models import Article, ArticleStatus, PublishJob, PublishStatus, TelegramReviewJob
from app.services.content_generation import generate_ru_summary
from app.services.telegram_context import telegram_bot_token, telegram_channel_id, telegram_signature


def send_test_message(text: str = "Neurovibes bot test message") -> dict:
    channel_id = (telegram_channel_id() or settings.telegram_channel_id or "").strip()
    token = (telegram_bot_token() or settings.telegram_bot_token or "").strip()
    if not token or not channel_id:
        return {"ok": False, "error": "telegram_not_configured"}

    url_base = f"https://api.telegram.org/bot{token}"
    try:
        resp = httpx.post(
            f"{url_base}/sendMessage",
            data={"chat_id": channel_id, "text": text[:4096]},
            timeout=30,
        )
        data = resp.json()
        if not data.get("ok"):
            return {"ok": False, "error": str(data)}
        return {"ok": True, "message_id": str(data["result"]["message_id"])}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _has_pending_review(session, article_id: int) -> bool:
    job = session.scalars(
        select(TelegramReviewJob)
        .where(TelegramReviewJob.article_id == int(article_id))
        .order_by(TelegramReviewJob.id.desc())
        .limit(1)
    ).first()
    if not job:
        return False
    status = str(job.status or "").strip().lower()
    return status in {"sent", "resent"}


def publish_article(article_id: int, *, manual: bool = True) -> dict:
    # Hard rule: publish only RU posts. If RU content is missing, try to prepare it automatically.
    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            return {"ok": False, "error": "article_not_found"}
        if article.status in {ArticleStatus.ARCHIVED, ArticleStatus.REJECTED, ArticleStatus.DOUBLE}:
            return {"ok": False, "error": f"publish_blocked_status:{str(article.status)}"}
        if (not manual) and _has_pending_review(session, article_id):
            return {"ok": False, "error": "publish_blocked_pending_review"}
        has_ru = bool((article.ru_title or "").strip()) and bool((article.ru_summary or "").strip())
    if not has_ru:
        try:
            generate_ru_summary(article_id)
        except Exception:
            pass

    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            return {"ok": False, "error": "article_not_found"}
        if article.status in {ArticleStatus.ARCHIVED, ArticleStatus.REJECTED, ArticleStatus.DOUBLE}:
            return {"ok": False, "error": f"publish_blocked_status:{str(article.status)}"}
        if (not manual) and _has_pending_review(session, article_id):
            return {"ok": False, "error": "publish_blocked_pending_review"}
        if not (article.ru_title or "").strip() or not (article.ru_summary or "").strip():
            return {"ok": False, "error": "ru_content_required", "hint": "Нажми Generate Post и/или Translate Full, затем сохрани RU текст"}

        title = escape(article.ru_title or "")
        summary = escape(article.ru_summary or "")
        url = escape(article.canonical_url or "")
        signature = escape(telegram_signature() or settings.telegram_signature or "@neuro_vibes_future")
        caption = (
            f"<b>{title}</b>\n\n"
            f"{summary}\n"
            f"<a href=\"{url}\">Подробнее</a>\n\n"
            f"{signature}"
        )
        job = PublishJob(article_id=article.id, status=PublishStatus.PENDING)
        session.add(job)

    channel_id = (telegram_channel_id() or settings.telegram_channel_id or "").strip()
    token = (telegram_bot_token() or settings.telegram_bot_token or "").strip()
    if not token or not channel_id:
        with session_scope() as session:
            pending = session.scalars(select(PublishJob).where(PublishJob.article_id == article_id).order_by(PublishJob.id.desc()).limit(1)).first()
            if pending:
                pending.status = PublishStatus.FAILED
                pending.error_text = "telegram_not_configured"
        return {"ok": False, "error": "telegram_not_configured"}

    url_base = f"https://api.telegram.org/bot{token}"

    try:
        image_path = None
        with session_scope() as session:
            article = session.get(Article, article_id)
            if article:
                image_path = article.generated_image_path

        resp = None
        if image_path and image_path.startswith(("http://", "https://")):
            photo_resp = httpx.post(
                f"{url_base}/sendPhoto",
                data={
                    "chat_id": channel_id,
                    "caption": caption[:1024],
                    "parse_mode": "HTML",
                    "photo": image_path,
                },
                timeout=30,
            )
            photo_data = photo_resp.json()
            if photo_data.get("ok"):
                resp = photo_resp
        elif image_path and os.path.exists(image_path):
            with open(image_path, "rb") as f:
                photo_resp = httpx.post(
                    f"{url_base}/sendPhoto",
                    data={
                        "chat_id": channel_id,
                        "caption": caption[:1024],
                        "parse_mode": "HTML",
                    },
                    files={"photo": f},
                    timeout=30,
                )
            photo_data = photo_resp.json()
            if photo_data.get("ok"):
                resp = photo_resp

        if resp is None:
            resp = httpx.post(
                f"{url_base}/sendMessage",
                data={"chat_id": channel_id, "text": caption[:4096], "parse_mode": "HTML"},
                timeout=30,
            )

        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(str(data))

        msg_id = str(data["result"]["message_id"])

        with session_scope() as session:
            article = session.get(Article, article_id)
            if article:
                article.status = ArticleStatus.PUBLISHED
                article.scheduled_publish_at = None
            job = session.scalars(select(PublishJob).where(PublishJob.article_id == article_id).order_by(PublishJob.id.desc()).limit(1)).first()
            if job:
                job.status = PublishStatus.SUCCESS
                job.telegram_message_id = msg_id
            review_job = session.scalars(
                select(TelegramReviewJob).where(TelegramReviewJob.article_id == article_id).order_by(TelegramReviewJob.id.desc()).limit(1)
            ).first()
            if review_job:
                review_job.status = "published"
                review_job.updated_at = datetime.utcnow()

        return {"ok": True, "message_id": msg_id}
    except Exception as exc:
        with session_scope() as session:
            job = session.scalars(select(PublishJob).where(PublishJob.article_id == article_id).order_by(PublishJob.id.desc()).limit(1)).first()
            if job:
                job.status = PublishStatus.FAILED
                job.error_text = str(exc)
        return {"ok": False, "error": str(exc)}


def publish_scheduled_due(limit: int = 20) -> dict:
    now = datetime.utcnow()
    with session_scope() as session:
        rows = session.scalars(
            select(Article.id)
            .where(
                Article.scheduled_publish_at.is_not(None),
                Article.scheduled_publish_at <= now,
                Article.status != ArticleStatus.PUBLISHED,
                Article.status != ArticleStatus.ARCHIVED,
                Article.status != ArticleStatus.DOUBLE,
                Article.status != ArticleStatus.REJECTED,
            )
            .order_by(Article.scheduled_publish_at.asc())
            .limit(limit)
        ).all()

    processed = 0
    published = 0
    failed = 0
    for article_id in rows:
        processed += 1
        out = publish_article(int(article_id), manual=False)
        if out.get("ok"):
            published += 1
        else:
            failed += 1

    return {"ok": True, "processed": processed, "published": published, "failed": failed}
