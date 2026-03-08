from __future__ import annotations

import os
import time
from datetime import datetime
from html import escape

import httpx
from sqlalchemy import select

from app.core.config import settings
from app.db import session_scope
from app.models import Article, ArticleStatus, PublishJob, PublishStatus, TelegramReviewJob
from app.services.content_generation import generate_ru_summary
from app.services.telegram_context import telegram_bot_token, telegram_channel_id, telegram_signature

_MIN_FULL_TEXT_LEN = 500
_TG_TIMEOUT_SECONDS = 30
_TG_MAX_RETRIES = 3
_TG_BASE_DELAY_SECONDS = 1.0
_TG_PER_CHAT_MIN_DELAY_SECONDS = 1.05
_TG_GLOBAL_MIN_DELAY_SECONDS = 0.05
_LAST_CHAT_SEND_TS: dict[str, float] = {}
_LAST_GLOBAL_SEND_TS: float = 0.0


def _mask_sensitive(text: str, token: str | None = None) -> str:
    out = str(text or "")
    if token:
        out = out.replace(token, "***")
    out = out.replace("api.telegram.org/bot", "api.telegram.org/bot***")
    return out[:1200]


def _tg_wait_send_slot(chat_id: str) -> None:
    global _LAST_GLOBAL_SEND_TS
    now = time.monotonic()
    last_chat = _LAST_CHAT_SEND_TS.get(chat_id, 0.0)
    wait_chat = max(0.0, _TG_PER_CHAT_MIN_DELAY_SECONDS - (now - last_chat))
    wait_global = max(0.0, _TG_GLOBAL_MIN_DELAY_SECONDS - (now - _LAST_GLOBAL_SEND_TS))
    wait_s = max(wait_chat, wait_global)
    if wait_s > 0:
        time.sleep(wait_s)
    _LAST_CHAT_SEND_TS[chat_id] = time.monotonic()
    _LAST_GLOBAL_SEND_TS = _LAST_CHAT_SEND_TS[chat_id]


def _tg_request(
    url: str,
    *,
    token: str,
    chat_id: str,
    data: dict | None = None,
    files: dict | None = None,
    timeout: int = _TG_TIMEOUT_SECONDS,
) -> dict:
    last_error = ""
    for attempt in range(1, _TG_MAX_RETRIES + 1):
        _tg_wait_send_slot(chat_id)
        try:
            resp = httpx.post(url, data=data, files=files, timeout=timeout)
            payload = resp.json()
            if payload.get("ok"):
                return payload

            error_code = int(payload.get("error_code") or 0)
            retry_after = 0
            if isinstance(payload.get("parameters"), dict):
                retry_after = int(payload["parameters"].get("retry_after") or 0)
            if error_code == 429 and attempt < _TG_MAX_RETRIES:
                time.sleep(max(_TG_BASE_DELAY_SECONDS, float(retry_after or 1)))
                continue
            last_error = str(payload)
            break
        except Exception as exc:
            last_error = _mask_sensitive(str(exc), token=token)
            if attempt < _TG_MAX_RETRIES:
                time.sleep(_TG_BASE_DELAY_SECONDS * attempt)
                continue
            break
    return {"ok": False, "error": last_error[:1200]}


def send_test_message(text: str = "Neurovibes bot test message") -> dict:
    channel_id = (telegram_channel_id() or settings.telegram_channel_id or "").strip()
    token = (telegram_bot_token() or settings.telegram_bot_token or "").strip()
    if not token or not channel_id:
        return {"ok": False, "error": "telegram_not_configured"}

    url_base = f"https://api.telegram.org/bot{token}"
    out = _tg_request(
        f"{url_base}/sendMessage",
        token=token,
        chat_id=channel_id,
        data={"chat_id": channel_id, "text": text[:4096]},
    )
    if not out.get("ok"):
        return {"ok": False, "error": _mask_sensitive(str(out.get("error") or out), token=token)}
    return {"ok": True, "message_id": str(out["result"]["message_id"])}


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


def _has_sufficient_publish_content(article: Article) -> bool:
    text_len = len((article.text or "").strip())
    mode = str(article.content_mode or "summary_only").strip().lower()
    if mode != "summary_only":
        return True
    return text_len >= _MIN_FULL_TEXT_LEN


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
        if not _has_sufficient_publish_content(article):
            return {
                "ok": False,
                "error": "publish_blocked_insufficient_content",
                "hint": "Сайт не дал полный текст. Нужен полноценный материал, а не короткий RSS summary.",
            }
        last_success = session.scalars(
            select(PublishJob)
            .where(PublishJob.article_id == article.id, PublishJob.status == PublishStatus.SUCCESS)
            .order_by(PublishJob.id.desc())
            .limit(1)
        ).first()
        if last_success and (last_success.telegram_message_id or "").strip():
            return {"ok": True, "message_id": str(last_success.telegram_message_id), "idempotent": True}
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
        if not _has_sufficient_publish_content(article):
            return {
                "ok": False,
                "error": "publish_blocked_insufficient_content",
                "hint": "Сайт не дал полный текст. Нужен полноценный материал, а не короткий RSS summary.",
            }
        last_success = session.scalars(
            select(PublishJob)
            .where(PublishJob.article_id == article.id, PublishJob.status == PublishStatus.SUCCESS)
            .order_by(PublishJob.id.desc())
            .limit(1)
        ).first()
        if last_success and (last_success.telegram_message_id or "").strip():
            article.status = ArticleStatus.PUBLISHED
            article.scheduled_publish_at = None
            return {"ok": True, "message_id": str(last_success.telegram_message_id), "idempotent": True}
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
            photo_data = _tg_request(
                f"{url_base}/sendPhoto",
                token=token,
                chat_id=channel_id,
                data={
                    "chat_id": channel_id,
                    "caption": caption[:1024],
                    "parse_mode": "HTML",
                    "photo": image_path,
                },
            )
            if photo_data.get("ok"):
                resp = photo_data
        elif image_path and os.path.exists(image_path):
            with open(image_path, "rb") as f:
                photo_data = _tg_request(
                    f"{url_base}/sendPhoto",
                    token=token,
                    chat_id=channel_id,
                    data={
                        "chat_id": channel_id,
                        "caption": caption[:1024],
                        "parse_mode": "HTML",
                    },
                    files={"photo": f},
                )
            if photo_data.get("ok"):
                resp = photo_data

        if resp is None:
            resp = _tg_request(
                f"{url_base}/sendMessage",
                token=token,
                chat_id=channel_id,
                data={"chat_id": channel_id, "text": caption[:4096], "parse_mode": "HTML"},
            )

        data = resp
        if not data.get("ok"):
            raise RuntimeError(_mask_sensitive(str(data), token=token))

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
                job.error_text = _mask_sensitive(str(exc), token=token)
        return {"ok": False, "error": _mask_sensitive(str(exc), token=token)}


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
