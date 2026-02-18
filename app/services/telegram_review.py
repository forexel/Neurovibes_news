from __future__ import annotations

import re
from datetime import datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

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
from app.services.telegram_context import (
    telegram_bot_token,
    telegram_review_chat_id,
    telegram_signature,
    telegram_timezone_name,
)
from app.services.runtime_settings import get_runtime_str
from app.services.pipeline import pick_hourly_backfill, pick_hourly_top
from app.services.scoring import reclassify_all_articles

_SQLI_PATTERNS = [
    re.compile(r"(?i)(?:'|\"|`)\s*or\s+1\s*=\s*1"),
    re.compile(r"(?i)\bunion\s+select\b"),
    re.compile(r"(?i);\s*(?:drop|truncate|alter|delete|insert|update|create)\b"),
    re.compile(r"(--|/\*|\*/)"),
]

def _post_decision_recalc() -> None:
    # Cheap recalc only: re-apply gates and update hourly selection using existing scores.
    try:
        reclassify_all_articles(limit=20000, include_archived=True, days_back=1, exclude_deleted=True)
    except Exception:
        pass
    try:
        pick_hourly_top()
    except Exception:
        pass


def _sanitize_reason_input(text: str) -> tuple[bool, str, str | None]:
    raw = str(text or "")
    cleaned = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", raw)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) < 5:
        return False, "", "слишком коротко (минимум 5 символов)"
    if len(cleaned) > 1000:
        cleaned = cleaned[:1000].strip()
    for pattern in _SQLI_PATTERNS:
        if pattern.search(cleaned):
            return False, "", "текст похож на небезопасный ввод"
    return True, cleaned, None


def _bot_base_url() -> str | None:
    token = (telegram_bot_token() or settings.telegram_bot_token or "").strip()
    if not token:
        return None
    return f"https://api.telegram.org/bot{token}"


def _review_chat_id() -> str:
    return (telegram_review_chat_id() or get_runtime_str("telegram_review_chat_id") or settings.telegram_review_chat_id or "").strip()

def _hour_slot_key() -> str:
    """Slot key for deduping hourly notifications (in user's configured timezone)."""
    try:
        tz = ZoneInfo(telegram_timezone_name() or get_runtime_str("timezone_name") or "Europe/Moscow")
    except Exception:
        tz = ZoneInfo("Europe/Moscow")
    return datetime.now(tz=tz).strftime("%Y%m%d%H")


def send_review_status_once_per_hour(kind: str, text: str) -> dict:
    """
    Send a status message to review chat at most once per hour.
    Used when there is no new top article or everything was filtered out.
    """
    configured_chat = _review_chat_id()
    if not configured_chat:
        return {"ok": False, "error": "telegram_review_chat_not_configured"}

    with session_scope() as session:
        runtime_chat = _get_kv(session, "telegram_review_runtime_chat_id", "").strip()
    chat_id = runtime_chat or configured_chat

    slot = _hour_slot_key()
    kv_key = "telegram_review_status_slot"
    with session_scope() as session:
        prev = _get_kv(session, kv_key, "")
        if prev == slot:
            return {"ok": True, "skipped": "already_sent_this_hour", "slot": slot, "kind": kind}
        sent = _send_message(chat_id=chat_id, text=text)
        if sent.get("ok"):
            _set_kv(session, kv_key, slot)
            _set_kv(session, "telegram_review_status_kind", kind[:64])
    return {"ok": True, "slot": slot, "kind": kind}


# Backward-compatible alias (internal name used by older worker code).
_send_review_status_once_per_hour = send_review_status_once_per_hour


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
    signature = escape(telegram_signature() or get_runtime_str("telegram_signature") or settings.telegram_signature or "@neuro_vibes_future")
    return (
        "Топ-1 за последний час. Публиковать?\n\n"
        f"<b>{escape(title)}</b>\n\n"
        f"{escape(summary)}\n"
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


def _delete_message(chat_id: str, message_id: str) -> None:
    base = _bot_base_url()
    if not base or not chat_id or not message_id:
        return
    try:
        httpx.post(
            f"{base}/deleteMessage",
            json={"chat_id": chat_id, "message_id": int(message_id)},
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
        article.archived_kind = "delete"
        article.archived_reason = reason
        article.archived_at = datetime.utcnow()
        article.updated_at = datetime.utcnow()
        session.query(DailySelection).filter(
            DailySelection.article_id == article_id,
            DailySelection.active.is_(True),
        ).update({"active": False}, synchronize_session=False)
    return {"ok": True}


def send_hourly_top_for_review(article_id: int | None = None, force: bool = False) -> dict:
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
        if existing and not force:
            return {
                "ok": True,
                "skipped": "already_sent",
                "article_id": article.id,
                "chat_id": existing.chat_id,
                "message_id": existing.review_message_id,
                "hint": "Use force=1 to resend to current review chat.",
            }

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
                {"text": "Скрыть", "callback_data": f"rv:hide:{target_article_id}"},
                {"text": "Удалить", "callback_data": f"rv:del:{target_article_id}"},
            ]
        ]
    }
    sent = _send_message(chat_id=chat_id, text=text, reply_markup=markup)
    if not sent.get("ok"):
        return {**sent, "chat_id": chat_id, "article_id": target_article_id}

    message_id = str((sent.get("result") or {}).get("message_id") or "")
    with session_scope() as session:
        existing = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == target_article_id)).first()
        if existing:
            existing.chat_id = chat_id
            existing.review_message_id = message_id or None
            existing.status = "resent"
            existing.updated_at = datetime.utcnow()
        else:
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
        # Mark that we sent an article message this hour (so worker can avoid spamming).
        _set_kv(session, "telegram_review_last_article_slot", _hour_slot_key())
    return {"ok": True, "article_id": target_article_id, "chat_id": chat_id, "message_id": message_id, "forced": bool(force)}


def send_selected_backlog_for_review(limit: int = 20) -> dict:
    configured_chat = _review_chat_id()
    if not configured_chat:
        return {"ok": False, "error": "telegram_review_chat_not_configured"}
    with session_scope() as session:
        runtime_chat = _get_kv(session, "telegram_review_runtime_chat_id", "").strip()
    chat_id = runtime_chat or configured_chat

    sent = 0
    skipped_exists = 0
    considered = 0
    last_article_id = None

    # Priority backlog:
    # 1) Selected Day (DailySelection.active)
    # 2) Selected Hour (ArticleStatus.SELECTED_HOURLY)
    # This matches the expectation: "send me many messages for previous hours/days".
    with session_scope() as session:
        day_rows = session.scalars(
            select(Article)
            .join(DailySelection, DailySelection.article_id == Article.id)
            .where(
                DailySelection.active.is_(True),
                Article.status != ArticleStatus.PUBLISHED,
                Article.status != ArticleStatus.ARCHIVED,
                Article.status != ArticleStatus.DOUBLE,
                Article.status != ArticleStatus.REJECTED,
            )
            .order_by(DailySelection.selected_date.asc(), Article.updated_at.asc())
            .limit(2000)
        ).all()
        hour_rows = session.scalars(
            select(Article)
            .where(
                Article.status == ArticleStatus.SELECTED_HOURLY,
                Article.status != ArticleStatus.PUBLISHED,
                Article.status != ArticleStatus.ARCHIVED,
                Article.status != ArticleStatus.DOUBLE,
                Article.status != ArticleStatus.REJECTED,
            )
            .order_by(Article.updated_at.asc())
            .limit(2000)
        ).all()

    candidates: list[Article] = []
    seen_ids: set[int] = set()
    for a in day_rows + hour_rows:
        if int(a.id) in seen_ids:
            continue
        seen_ids.add(int(a.id))
        candidates.append(a)

    max_send = max(1, min(int(limit), 100))
    for a in candidates:
        if sent >= max_send:
            break
        considered += 1
        target_article_id = int(a.id)
        with session_scope() as session:
            exists = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == target_article_id)).first()
            if exists is not None:
                skipped_exists += 1
                continue
            article = session.get(Article, target_article_id)
            if not article:
                continue
            text = _build_review_text(article)

        markup = {
            "inline_keyboard": [
                [
                    {"text": "Опубликовать", "callback_data": f"rv:pub:{target_article_id}"},
                    {"text": "Скрыть", "callback_data": f"rv:hide:{target_article_id}"},
                    {"text": "Удалить", "callback_data": f"rv:del:{target_article_id}"},
                ]
            ]
        }
        out = _send_message(chat_id=chat_id, text=text, reply_markup=markup)
        if not out.get("ok"):
            return {
                "ok": False,
                "error": out.get("error"),
                "sent": sent,
                "considered": considered,
                "skipped_exists": skipped_exists,
                "chat_id": chat_id,
            }
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

    return {
        "ok": True,
        "sent": sent,
        "last_article_id": last_article_id,
        "considered": considered,
        "skipped_exists": skipped_exists,
        "chat_id": chat_id,
    }


def send_hourly_backfill_for_review(hours_back: int = 24, limit: int = 24) -> dict:
    """
    Ensure hourly selections exist for the last N hours (aligned to user's timezone),
    then send up to `limit` hourly-selected items that were not yet sent to the review chat.
    """
    configured_chat = _review_chat_id()
    if not configured_chat:
        return {"ok": False, "error": "telegram_review_chat_not_configured"}
    with session_scope() as session:
        runtime_chat = _get_kv(session, "telegram_review_runtime_chat_id", "").strip()
    chat_id = runtime_chat or configured_chat

    # 1) Backfill missing buckets.
    backfill = pick_hourly_backfill(hours_back=hours_back, per_hour=1)

    # 2) Send hourly backlog (only those not yet sent).
    hours = max(1, min(int(hours_back or 24), 168))
    now = datetime.utcnow()
    cutoff = now - timedelta(hours=hours)

    with session_scope() as session:
        rows = session.scalars(
            select(Article)
            .where(
                Article.status == ArticleStatus.SELECTED_HOURLY,
                Article.selected_hour_bucket_utc.is_not(None),
                Article.selected_hour_bucket_utc >= cutoff,
                Article.status != ArticleStatus.PUBLISHED,
                Article.status != ArticleStatus.ARCHIVED,
                Article.status != ArticleStatus.DOUBLE,
                Article.status != ArticleStatus.REJECTED,
            )
            .order_by(Article.selected_hour_bucket_utc.asc(), Article.updated_at.asc())
            .limit(2000)
        ).all()

    sent = 0
    skipped_exists = 0
    considered = 0
    last_article_id = None
    max_send = max(1, min(int(limit or 24), 100))

    for a in rows:
        if sent >= max_send:
            break
        considered += 1
        target_article_id = int(a.id)

        with session_scope() as session:
            exists = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == target_article_id)).first()
            if exists is not None:
                skipped_exists += 1
                continue
            article = session.get(Article, target_article_id)
            if not article:
                continue
            text = _build_review_text(article)

        markup = {
            "inline_keyboard": [
                [
                    {"text": "Опубликовать", "callback_data": f"rv:pub:{target_article_id}"},
                    {"text": "Скрыть", "callback_data": f"rv:hide:{target_article_id}"},
                    {"text": "Удалить", "callback_data": f"rv:del:{target_article_id}"},
                ]
            ]
        }
        out = _send_message(chat_id=chat_id, text=text, reply_markup=markup)
        if not out.get("ok"):
            return {
                "ok": False,
                "error": out.get("error"),
                "sent": sent,
                "considered": considered,
                "skipped_exists": skipped_exists,
                "chat_id": chat_id,
                "backfill": backfill,
            }
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

    return {
        "ok": True,
        "sent": sent,
        "last_article_id": last_article_id,
        "considered": considered,
        "skipped_exists": skipped_exists,
        "chat_id": chat_id,
        "backfill": backfill,
    }


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

    # Log callback data for diagnostics (helps catch wrong button mappings).
    try:
        print("[tg] callback", {"data": data, "chat_id": chat_id, "message_id": message_id, "user_id": user_id}, flush=True)
    except Exception:
        pass

    if callback_id:
        _answer_callback(callback_id, "Принято")

    if not data.startswith("rv:") or not chat_id:
        return {"ok": True, "skipped": "not_review_callback"}

    parts = data.split(":")
    if len(parts) < 3 or not parts[2].isdigit():
        return {"ok": True, "skipped": "bad_callback"}
    action = parts[1]
    article_id = int(parts[2])

    if action == "pub":
        _edit_message_reply_markup(chat_id, message_id)
        kb = {
            "inline_keyboard": [
                [
                    {"text": "Сейчас", "callback_data": f"rv:pubnow:{article_id}"},
                    {"text": "+1 час", "callback_data": f"rv:pub1h:{article_id}"},
                ],
                [
                    {"text": "Ввести время (МСК)", "callback_data": f"rv:pubpick:{article_id}"},
                ],
            ]
        }
        _send_message(chat_id, "Когда публиковать?", reply_markup=kb)
        return {"ok": True, "action": "publish_choose_time", "article_id": article_id}

    if action == "pubnow":
        _edit_message_reply_markup(chat_id, message_id)
        # Safety: do NOT publish immediately. Ask for reason first, then publish in message handler.
        prompt = _send_message(chat_id, "Почему публикуем сейчас? Ответь реплаем на это сообщение.", force_reply=True)
        if prompt.get("ok"):
            prompt_id = str((prompt.get("result") or {}).get("message_id") or "")
            if prompt_id:
                with session_scope() as session:
                    session.add(
                        TelegramPendingReason(
                            chat_id=chat_id,
                            user_id=user_id or "0",
                            article_id=article_id,
                            action="publish_now",
                            prompt_message_id=prompt_id,
                            created_at=datetime.utcnow(),
                        )
                    )
        return {"ok": True, "action": "publish_now_pending_reason", "article_id": article_id}

    if action == "pub1h":
        _edit_message_reply_markup(chat_id, message_id)
        with session_scope() as session:
            article = session.get(Article, article_id)
            if article:
                article.scheduled_publish_at = datetime.utcnow() + timedelta(hours=1)
                article.updated_at = datetime.utcnow()
        prompt = _send_message(chat_id, "Поставил публикацию через 1 час. Почему выбрал именно эту новость? Ответь реплаем.", force_reply=True)
        if prompt.get("ok"):
            prompt_id = str((prompt.get("result") or {}).get("message_id") or "")
            if prompt_id:
                with session_scope() as session:
                    session.add(
                        TelegramPendingReason(
                            chat_id=chat_id,
                            user_id=user_id or "0",
                            article_id=article_id,
                            action="schedule_1h",
                            prompt_message_id=prompt_id,
                            created_at=datetime.utcnow(),
                        )
                    )
        return {"ok": True, "action": "publish_plus_1h", "article_id": article_id}

    if action == "pubpick":
        _edit_message_reply_markup(chat_id, message_id)
        prompt = _send_message(
            chat_id,
            "Напиши время публикации по Москве: `HH:MM` или `YYYY-MM-DD HH:MM`. Ответь реплаем на это сообщение.",
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
                            action="pick_time",
                            prompt_message_id=prompt_id,
                            created_at=datetime.utcnow(),
                        )
                    )
        return {"ok": True, "action": "publish_pick_time", "article_id": article_id}

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

    if action == "hide":
        _edit_message_reply_markup(chat_id, message_id)
        prompt = _send_message(chat_id, "Почему скрываем (не удаляем) эту новость? Ответь реплаем.", force_reply=True)
        if prompt.get("ok"):
            prompt_id = str((prompt.get("result") or {}).get("message_id") or "")
            if prompt_id:
                with session_scope() as session:
                    session.add(
                        TelegramPendingReason(
                            chat_id=chat_id,
                            user_id=user_id or "0",
                            article_id=article_id,
                            action="hide",
                            prompt_message_id=prompt_id,
                            created_at=datetime.utcnow(),
                        )
                    )
        return {"ok": True, "action": "hide", "article_id": article_id}

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
    msg_id = str(msg.get("message_id") or "")
    text = str(msg.get("text") or "")
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

    ok_input, safe_text, input_error = _sanitize_reason_input(text)
    if not ok_input:
        _send_message(chat_id, f"Не могу принять такой ввод: {input_error}. Напиши причину обычным текстом и ответь на то же сообщение.")
        return {"ok": True, "skipped": "invalid_reason_input"}

    if action == "publish":
        with session_scope() as session:
            pending = session.scalars(
                select(TelegramPendingReason).where(
                    TelegramPendingReason.prompt_message_id == prompt_id,
                    TelegramPendingReason.chat_id == chat_id,
                )
            ).first()
            if pending:
                session.delete(pending)
            session.add(EditorFeedback(article_id=article_id, explanation_text=safe_text))
            job = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == article_id)).first()
            if job:
                job.decision_reason = safe_text
                job.updated_at = datetime.utcnow()
        # Keep chat clean: remove bot prompt + user's reply + original preview message.
        _delete_message(chat_id, prompt_id)
        if msg_id:
            _delete_message(chat_id, msg_id)
        with session_scope() as session:
            job = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == article_id)).first()
            if job and (job.review_message_id or "").strip():
                _delete_message(chat_id, str(job.review_message_id))
        _post_decision_recalc()
        return {"ok": True, "action": "publish_reason_saved", "article_id": article_id}

    if action == "publish_now":
        out = publish_article(article_id)
        with session_scope() as session:
            pending = session.scalars(
                select(TelegramPendingReason).where(
                    TelegramPendingReason.prompt_message_id == prompt_id,
                    TelegramPendingReason.chat_id == chat_id,
                )
            ).first()
            if pending:
                session.delete(pending)
            session.add(EditorFeedback(article_id=article_id, explanation_text=safe_text))
            job = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == article_id)).first()
            if job:
                job.status = "published" if out.get("ok") else "failed"
                job.decision_reason = safe_text
                job.updated_at = datetime.utcnow()
        # Keep chat clean: remove bot prompt + user's reply + original preview message.
        _delete_message(chat_id, prompt_id)
        if msg_id:
            _delete_message(chat_id, msg_id)
        with session_scope() as session:
            job = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == article_id)).first()
            if job and (job.review_message_id or "").strip():
                _delete_message(chat_id, str(job.review_message_id))
        _post_decision_recalc()
        return {"ok": True, "action": "publish_now_done", "article_id": article_id, "publish": out}

    if action == "schedule_1h":
        with session_scope() as session:
            pending = session.scalars(
                select(TelegramPendingReason).where(
                    TelegramPendingReason.prompt_message_id == prompt_id,
                    TelegramPendingReason.chat_id == chat_id,
                )
            ).first()
            if pending:
                session.delete(pending)
            session.add(EditorFeedback(article_id=article_id, explanation_text=f"SCHEDULE(+1h): {safe_text}"))
            job = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == article_id)).first()
            if job:
                job.decision_reason = safe_text
                job.updated_at = datetime.utcnow()
        _delete_message(chat_id, prompt_id)
        if msg_id:
            _delete_message(chat_id, msg_id)
        with session_scope() as session:
            job = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == article_id)).first()
            if job and (job.review_message_id or "").strip():
                _delete_message(chat_id, str(job.review_message_id))
        _post_decision_recalc()
        return {"ok": True, "action": "schedule_reason_saved", "article_id": article_id}

    if action == "pick_time":
        # Parse a Moscow-time timestamp from user's reply and store as UTC in scheduled_publish_at.
        raw = safe_text
        m = re.match(r"^(\d{4}-\d{2}-\d{2})\s+(\d{1,2}:\d{2})$", raw)
        m2 = re.match(r"^(\d{1,2}):(\d{2})$", raw)
        dt_msk: datetime | None = None
        try:
            tz = ZoneInfo(telegram_timezone_name() or get_runtime_str("timezone_name") or "Europe/Moscow")
        except Exception:
            tz = ZoneInfo("Europe/Moscow")
        try:
            if m:
                dt_msk = datetime.fromisoformat(f"{m.group(1)} {m.group(2)}").replace(tzinfo=tz)
            elif m2:
                now_msk = datetime.now(tz=tz)
                hh = int(m2.group(1))
                mm = int(m2.group(2))
                dt_msk = now_msk.replace(hour=hh, minute=mm, second=0, microsecond=0)
                if dt_msk < now_msk:
                    dt_msk = dt_msk + timedelta(days=1)
        except Exception:
            dt_msk = None
        if dt_msk is None:
            _send_message(chat_id, "Не понял время. Напиши `HH:MM` или `YYYY-MM-DD HH:MM` (по Москве) и ответь на то же сообщение.")
            return {"ok": True, "skipped": "invalid_publish_time"}
        dt_utc = dt_msk.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

        with session_scope() as session:
            pending = session.scalars(
                select(TelegramPendingReason).where(
                    TelegramPendingReason.prompt_message_id == prompt_id,
                    TelegramPendingReason.chat_id == chat_id,
                )
            ).first()
            if pending:
                session.delete(pending)
            article = session.get(Article, article_id)
            if not article:
                _send_message(chat_id, "Не нашел статью для планирования публикации.")
                return {"ok": True, "action": "pick_time_missing_article", "article_id": article_id}
            article.scheduled_publish_at = dt_utc
            article.updated_at = datetime.utcnow()
        prompt2 = _send_message(chat_id, f"Ок, поставил публикацию на {dt_msk.strftime('%Y-%m-%d %H:%M')} МСК. Почему выбрал эту новость? Ответь реплаем.", force_reply=True)
        if prompt2.get("ok"):
            prompt2_id = str((prompt2.get("result") or {}).get("message_id") or "")
            if prompt2_id:
                with session_scope() as session:
                    session.add(
                        TelegramPendingReason(
                            chat_id=chat_id,
                            user_id=user_id or "0",
                            article_id=article_id,
                            action="schedule_custom",
                            prompt_message_id=prompt2_id,
                            created_at=datetime.utcnow(),
                        )
                    )
        return {"ok": True, "action": "scheduled_custom_time", "article_id": article_id, "scheduled_utc": dt_utc.isoformat(sep=" ", timespec="seconds")}

    if action == "schedule_custom":
        with session_scope() as session:
            pending = session.scalars(
                select(TelegramPendingReason).where(
                    TelegramPendingReason.prompt_message_id == prompt_id,
                    TelegramPendingReason.chat_id == chat_id,
                )
            ).first()
            if pending:
                session.delete(pending)
            session.add(EditorFeedback(article_id=article_id, explanation_text=f"SCHEDULE(custom): {safe_text}"))
            job = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == article_id)).first()
            if job:
                job.decision_reason = safe_text
                job.updated_at = datetime.utcnow()
        _delete_message(chat_id, prompt_id)
        if msg_id:
            _delete_message(chat_id, msg_id)
        with session_scope() as session:
            job = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == article_id)).first()
            if job and (job.review_message_id or "").strip():
                _delete_message(chat_id, str(job.review_message_id))
        _post_decision_recalc()
        return {"ok": True, "action": "schedule_custom_reason_saved", "article_id": article_id}

    if action == "delete":
        out = _archive_article_with_reason(article_id=article_id, reason=safe_text)
        with session_scope() as session:
            pending = session.scalars(
                select(TelegramPendingReason).where(
                    TelegramPendingReason.prompt_message_id == prompt_id,
                    TelegramPendingReason.chat_id == chat_id,
                )
            ).first()
            if pending:
                session.delete(pending)
            job = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == article_id)).first()
            if job:
                job.status = "deleted" if out.get("ok") else "failed"
                job.decision_reason = safe_text
                job.updated_at = datetime.utcnow()
        _delete_message(chat_id, prompt_id)
        if msg_id:
            _delete_message(chat_id, msg_id)
        with session_scope() as session:
            job = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == article_id)).first()
            if job and (job.review_message_id or "").strip():
                _delete_message(chat_id, str(job.review_message_id))
        _post_decision_recalc()
        return {"ok": True, "action": "delete_reason_saved", "article_id": article_id, "delete": out}

    if action == "hide":
        with session_scope() as session:
            pending = session.scalars(
                select(TelegramPendingReason).where(
                    TelegramPendingReason.prompt_message_id == prompt_id,
                    TelegramPendingReason.chat_id == chat_id,
                )
            ).first()
            if pending:
                session.delete(pending)
            article = session.get(Article, article_id)
            if article:
                article.status = ArticleStatus.ARCHIVED
                article.archived_kind = "hide"
                article.archived_reason = safe_text
                article.archived_at = datetime.utcnow()
                article.updated_at = datetime.utcnow()
            session.add(EditorFeedback(article_id=article_id, explanation_text=f"HIDE: {safe_text}"))
            job = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == article_id)).first()
            if job:
                job.status = "deleted"
                job.decision_reason = safe_text
                job.updated_at = datetime.utcnow()
        _delete_message(chat_id, prompt_id)
        if msg_id:
            _delete_message(chat_id, msg_id)
        with session_scope() as session:
            job = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == article_id)).first()
            if job and (job.review_message_id or "").strip():
                _delete_message(chat_id, str(job.review_message_id))
        _post_decision_recalc()
        return {"ok": True, "action": "hide_reason_saved", "article_id": article_id}

    if action == "later":
        with session_scope() as session:
            pending = session.scalars(
                select(TelegramPendingReason).where(
                    TelegramPendingReason.prompt_message_id == prompt_id,
                    TelegramPendingReason.chat_id == chat_id,
                )
            ).first()
            if pending:
                session.delete(pending)
            article = session.get(Article, article_id)
            if not article:
                _send_message(chat_id, "Не нашел статью для отложенной отправки.")
                return {"ok": True, "action": "later_missing_article", "article_id": article_id}
            article.scheduled_publish_at = datetime.utcnow() + timedelta(hours=3)
            article.updated_at = datetime.utcnow()
            session.add(
                EditorFeedback(
                    article_id=article_id,
                    explanation_text=f"LATER: {safe_text}",
                )
            )
            job = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == article_id)).first()
            if job:
                job.status = "sent"
                job.decision_reason = f"later: {safe_text}"
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
