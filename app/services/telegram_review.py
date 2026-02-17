from __future__ import annotations

from datetime import datetime, timedelta
from html import escape

import httpx
from sqlalchemy import select

from app.core.config import settings
from app.db import session_scope
from app.models import (
    Article,
    ArticleStatus,
    AuditLog,
    DailySelection,
    EditorFeedback,
    Score,
    TelegramBotKV,
    TelegramPendingReason,
    TelegramReviewJob,
)
from app.services.content_generation import generate_ru_summary
from app.services.telegram_publisher import publish_article


def _bot_base_url() -> str | None:
    token = (settings.telegram_bot_token or "").strip()
    if not token:
        return None
    return f"https://api.telegram.org/bot{token}"


def _review_chat_id() -> str:
    return (settings.telegram_review_chat_id or "").strip()


def _build_review_text(article: Article) -> str:
    title = (article.ru_title or "").strip()
    summary = (article.ru_summary or "").strip()
    if not title or not summary:
        generate_ru_summary(article.id)
        with session_scope() as session:
            fresh = session.get(Article, article.id)
            if fresh:
                title = (fresh.ru_title or "").strip()
                summary = (fresh.ru_summary or "").strip()
    if not title:
        title = (article.title or "").strip()
    if not summary:
        summary = ((article.subtitle or "")[:900]).strip() or "Короткий текст пока не готов."
    url = escape((article.canonical_url or "").strip())
    signature = escape(settings.telegram_signature or "@neuro_vibes_future")
    return (
        "Топ-1 за последний час. Публиковать?\n\n"
        f"<b>{escape(title)}</b>\n\n"
        f"{escape(summary)}\n\n"
        f"<a href=\"{url}\">Подробнее</a>\n\n"
        f"{signature}"
    )


def _send_message(chat_id: str, text: str, reply_markup: dict | None = None, force_reply: bool = False) -> dict:
    base = _bot_base_url()
    if not base:
        return {"ok": False, "error": "telegram_not_configured"}
    payload: dict = {"chat_id": chat_id, "text": text[:4096], "parse_mode": "HTML"}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    if force_reply:
        payload["reply_markup"] = {"force_reply": True, "input_field_placeholder": "Напиши причину и отправь ответом"}
    try:
        resp = httpx.post(f"{base}/sendMessage", json=payload, timeout=30)
        data = resp.json()
        if not data.get("ok"):
            return {"ok": False, "error": str(data)}
        return {"ok": True, "result": data.get("result") or {}}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def _answer_callback(callback_query_id: str, text: str = "") -> None:
    base = _bot_base_url()
    if not base:
        return
    try:
        httpx.post(
            f"{base}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text[:180]},
            timeout=20,
        )
    except Exception:
        pass


def _edit_message_reply_markup(chat_id: str, message_id: str) -> None:
    base = _bot_base_url()
    if not base or not chat_id or not message_id:
        return
    try:
        httpx.post(
            f"{base}/editMessageReplyMarkup",
            json={"chat_id": chat_id, "message_id": int(message_id), "reply_markup": {"inline_keyboard": []}},
            timeout=20,
        )
    except Exception:
        pass


def _edit_message_caption_action(chat_id: str, message_id: str, action_label: str) -> None:
    base = _bot_base_url()
    if not base or not chat_id or not message_id:
        return
    try:
        # Try to append action hint to existing message text when possible.
        httpx.post(
            f"{base}/editMessageReplyMarkup",
            json={"chat_id": chat_id, "message_id": int(message_id), "reply_markup": {"inline_keyboard": []}},
            timeout=20,
        )
    except Exception:
        pass


def _get_kv(session, key: str, default: str = "0") -> str:
    row = session.get(TelegramBotKV, key)
    if not row:
        return default
    return row.value or default


def _set_kv(session, key: str, value: str) -> None:
    row = session.get(TelegramBotKV, key)
    if row:
        row.value = value
        row.updated_at = datetime.utcnow()
    else:
        session.add(TelegramBotKV(key=key, value=value, updated_at=datetime.utcnow()))


def _archive_article_with_reason(article_id: int, reason: str) -> dict:
    with session_scope() as session:
        article = session.get(Article, article_id)
        if not article:
            return {"ok": False, "error": "article_not_found"}
        score = session.get(Score, article_id)
        session.add(
            AuditLog(
                action="article_delete_feedback",
                entity_type="article",
                entity_id=str(article_id),
                payload={
                    "reason": reason,
                    "title": article.title,
                    "canonical_url": article.canonical_url,
                    "status": str(article.status),
                    "content_mode": article.content_mode,
                    "score_10": int(round(float((score.final_score or 0.0) * 10))) if score and score.final_score is not None else None,
                    "origin": "telegram_review",
                },
            )
        )
        article.status = ArticleStatus.ARCHIVED
        article.updated_at = datetime.utcnow()
        session.query(DailySelection).filter(
            DailySelection.article_id == article_id,
            DailySelection.active.is_(True),
        ).update({"active": False}, synchronize_session=False)
    return {"ok": True}


def send_hourly_top_for_review(article_id: int | None = None) -> dict:
    configured_chat = _review_chat_id()
    if not configured_chat:
        return {"ok": False, "error": "telegram_review_chat_not_configured"}

    with session_scope() as session:
        runtime_chat = _get_kv(session, "telegram_review_runtime_chat_id", "").strip()
    chat_id = runtime_chat or configured_chat

    with session_scope() as session:
        if article_id is None:
            article = session.scalars(
                select(Article)
                .where(Article.status == ArticleStatus.SELECTED_HOURLY)
                .order_by(Article.updated_at.desc())
                .limit(1)
            ).first()
        else:
            article = session.get(Article, article_id)
        if not article:
            return {"ok": False, "error": "no_selected_article"}

        existing = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == article.id)).first()
        if existing:
            return {"ok": True, "skipped": "already_sent", "article_id": article.id}

        target_article_id = article.id

    with session_scope() as session:
        article = session.get(Article, target_article_id)
        if not article:
            return {"ok": False, "error": "article_not_found"}
        text = _build_review_text(article)

    markup = {
        "inline_keyboard": [
            [
                {"text": "Опубликовать", "callback_data": f"rv:pub:{target_article_id}"},
                {"text": "Удалить", "callback_data": f"rv:del:{target_article_id}"},
                {"text": "Отправить позже", "callback_data": f"rv:later:{target_article_id}"},
            ]
        ]
    }
    sent = _send_message(chat_id=chat_id, text=text, reply_markup=markup)
    if not sent.get("ok"):
        return sent

    message_id = str((sent.get("result") or {}).get("message_id") or "")
    with session_scope() as session:
        session.add(
            TelegramReviewJob(
                article_id=target_article_id,
                chat_id=chat_id,
                review_message_id=message_id or None,
                status="sent",
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
        )
    return {"ok": True, "article_id": target_article_id, "message_id": message_id}


def send_selected_backlog_for_review(limit: int = 20) -> dict:
    configured_chat = _review_chat_id()
    if not configured_chat:
        return {"ok": False, "error": "telegram_review_chat_not_configured"}
    with session_scope() as session:
        runtime_chat = _get_kv(session, "telegram_review_runtime_chat_id", "").strip()
    chat_id = runtime_chat or configured_chat

    sent = 0
    last_article_id = None
    for _ in range(max(1, min(int(limit), 100))):
        with session_scope() as session:
            rows = session.scalars(
                select(Article)
                .join(DailySelection, DailySelection.article_id == Article.id)
                .where(
                    DailySelection.active.is_(True),
                    Article.status != ArticleStatus.PUBLISHED,
                    Article.status != ArticleStatus.ARCHIVED,
                )
                .order_by(DailySelection.selected_date.asc(), Article.updated_at.asc())
                .limit(200)
            ).all()
            article = None
            for a in rows:
                exists = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == a.id)).first()
                if exists is None:
                    article = a
                    break
            if article is None:
                break
            target_article_id = int(article.id)
            text = _build_review_text(article)

        markup = {
            "inline_keyboard": [
                [
                    {"text": "Опубликовать", "callback_data": f"rv:pub:{target_article_id}"},
                    {"text": "Удалить", "callback_data": f"rv:del:{target_article_id}"},
                    {"text": "Отправить позже", "callback_data": f"rv:later:{target_article_id}"},
                ]
            ]
        }
        out = _send_message(chat_id=chat_id, text=text, reply_markup=markup)
        if not out.get("ok"):
            return {"ok": False, "error": out.get("error"), "sent": sent}
        message_id = str((out.get("result") or {}).get("message_id") or "")
        with session_scope() as session:
            session.add(
                TelegramReviewJob(
                    article_id=target_article_id,
                    chat_id=chat_id,
                    review_message_id=message_id or None,
                    status="sent",
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )
            )
        sent += 1
        last_article_id = target_article_id
    return {"ok": True, "sent": sent, "last_article_id": last_article_id}


def _handle_callback(update: dict) -> dict:
    cb = update.get("callback_query") or {}
    data = str(cb.get("data") or "")
    callback_id = str(cb.get("id") or "")
    from_user = cb.get("from") or {}
    user_id = str(from_user.get("id") or "")
    msg = cb.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    message_id = str(msg.get("message_id") or "")

    if callback_id:
        _answer_callback(callback_id, "Принято")

    if not data.startswith("rv:") or not chat_id:
        return {"ok": True, "skipped": "not_review_callback"}

    parts = data.split(":")
    if len(parts) != 3 or not parts[2].isdigit():
        return {"ok": True, "skipped": "bad_callback"}
    action = parts[1]
    article_id = int(parts[2])

    if action == "pub":
        _edit_message_reply_markup(chat_id, message_id)
        out = publish_article(article_id)
        with session_scope() as session:
            job = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == article_id)).first()
            if job and out.get("ok"):
                job.status = "published"
                job.updated_at = datetime.utcnow()
        prompt = _send_message(chat_id, f"Опубликовано. Почему выбрал эту новость? Ответь реплаем на это сообщение.", force_reply=True)
        if prompt.get("ok"):
            prompt_id = str((prompt.get("result") or {}).get("message_id") or "")
            if prompt_id:
                with session_scope() as session:
                    session.add(
                        TelegramPendingReason(
                            chat_id=chat_id,
                            user_id=user_id or "0",
                            article_id=article_id,
                            action="publish",
                            prompt_message_id=prompt_id,
                            created_at=datetime.utcnow(),
                        )
                    )
        return {"ok": True, "action": "publish", "article_id": article_id, "publish": out}

    if action == "del":
        _edit_message_reply_markup(chat_id, message_id)
        prompt = _send_message(chat_id, "Почему удаляем эту новость? Ответь реплаем на это сообщение.", force_reply=True)
        if prompt.get("ok"):
            prompt_id = str((prompt.get("result") or {}).get("message_id") or "")
            if prompt_id:
                with session_scope() as session:
                    session.add(
                        TelegramPendingReason(
                            chat_id=chat_id,
                            user_id=user_id or "0",
                            article_id=article_id,
                            action="delete",
                            prompt_message_id=prompt_id,
                            created_at=datetime.utcnow(),
                        )
                    )
        return {"ok": True, "action": "delete", "article_id": article_id}

    if action == "later":
        _edit_message_reply_markup(chat_id, message_id)
        prompt = _send_message(
            chat_id,
            "Почему отправляем позже? Ответь реплаем. После этого поставлю отложенную публикацию (по умолчанию +3 часа).",
            force_reply=True,
        )
        if prompt.get("ok"):
            prompt_id = str((prompt.get("result") or {}).get("message_id") or "")
            if prompt_id:
                with session_scope() as session:
                    session.add(
                        TelegramPendingReason(
                            chat_id=chat_id,
                            user_id=user_id or "0",
                            article_id=article_id,
                            action="later",
                            prompt_message_id=prompt_id,
                            created_at=datetime.utcnow(),
                        )
                    )
        return {"ok": True, "action": "later", "article_id": article_id}

    return {"ok": True, "skipped": "unknown_action"}


def _handle_message(update: dict) -> dict:
    msg = update.get("message") or {}
    text = str(msg.get("text") or "").strip()
    if not text:
        return {"ok": True, "skipped": "no_text"}
    chat = msg.get("chat") or {}
    chat_id = str(chat.get("id") or "")
    from_user = msg.get("from") or {}
    user_id = str(from_user.get("id") or "")
    username = str(from_user.get("username") or "").strip().lower()

    configured = _review_chat_id().strip()
    if configured.startswith("@") and username and ("@" + username) == configured.lower():
        with session_scope() as session:
            _set_kv(session, "telegram_review_runtime_chat_id", chat_id)

    reply_to = msg.get("reply_to_message") or {}
    prompt_id = str(reply_to.get("message_id") or "")
    if not prompt_id or not chat_id:
        return {"ok": True, "skipped": "not_reply"}

    with session_scope() as session:
        pending = session.scalars(
            select(TelegramPendingReason).where(
                TelegramPendingReason.prompt_message_id == prompt_id,
                TelegramPendingReason.chat_id == chat_id,
            )
        ).first()
        if not pending:
            return {"ok": True, "skipped": "no_pending"}
        article_id = int(pending.article_id)
        action = pending.action
        session.delete(pending)

    if action == "publish":
        with session_scope() as session:
            session.add(EditorFeedback(article_id=article_id, explanation_text=text))
            job = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == article_id)).first()
            if job:
                job.decision_reason = text
                job.updated_at = datetime.utcnow()
        _send_message(chat_id, "Причину публикации сохранил.")
        return {"ok": True, "action": "publish_reason_saved", "article_id": article_id}

    if action == "delete":
        out = _archive_article_with_reason(article_id=article_id, reason=text)
        with session_scope() as session:
            job = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == article_id)).first()
            if job:
                job.status = "deleted" if out.get("ok") else "failed"
                job.decision_reason = text
                job.updated_at = datetime.utcnow()
        _send_message(chat_id, "Удалил новость и сохранил причину." if out.get("ok") else f"Не удалось удалить: {out.get('error')}")
        return {"ok": True, "action": "delete_reason_saved", "article_id": article_id, "delete": out}

    if action == "later":
        with session_scope() as session:
            article = session.get(Article, article_id)
            if not article:
                _send_message(chat_id, "Не нашел статью для отложенной отправки.")
                return {"ok": True, "action": "later_missing_article", "article_id": article_id}
            article.scheduled_publish_at = datetime.utcnow() + timedelta(hours=3)
            article.updated_at = datetime.utcnow()
            session.add(
                EditorFeedback(
                    article_id=article_id,
                    explanation_text=f"LATER: {text}",
                )
            )
            job = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == article_id)).first()
            if job:
                job.status = "sent"
                job.decision_reason = f"later: {text}"
                job.updated_at = datetime.utcnow()
        _send_message(
            chat_id,
            "Поставил отложенную публикацию на +3 часа. Время можно изменить в карточке статьи (Schedule). Причину сохранил.",
        )
        return {"ok": True, "action": "later_scheduled", "article_id": article_id}

    return {"ok": True, "skipped": "unknown_pending_action"}


def poll_review_updates(limit: int = 50) -> dict:
    base = _bot_base_url()
    if not base:
        return {"ok": False, "error": "telegram_not_configured"}

    with session_scope() as session:
        offset_raw = _get_kv(session, "telegram_review_offset", "0")
        try:
            offset = int(offset_raw)
        except ValueError:
            offset = 0

    try:
        resp = httpx.post(
            f"{base}/getUpdates",
            json={"offset": offset, "timeout": 0, "limit": limit, "allowed_updates": ["callback_query", "message"]},
            timeout=30,
        )
        data = resp.json()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    if not data.get("ok"):
        return {"ok": False, "error": str(data)}

    updates = data.get("result") or []
    processed = 0
    for upd in updates:
        update_id = int(upd.get("update_id") or 0)
        if "callback_query" in upd:
            _handle_callback(upd)
        elif "message" in upd:
            _handle_message(upd)
        processed += 1
        with session_scope() as session:
            _set_kv(session, "telegram_review_offset", str(update_id + 1))

    return {"ok": True, "processed": processed}
