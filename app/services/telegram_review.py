from __future__ import annotations

import logging
import re
import time
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
    ReasonTagCatalog,
    Score,
    SelectionDecision,
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
from app.services.runtime_settings import get_runtime_float, get_runtime_int, get_runtime_str
from app.services.pipeline import pick_hourly_backfill, pick_hourly_top
from app.services.preference import log_training_event
from app.services.scoring import reclassify_all_articles
from app.services.scoring import score_article_by_id

logger = logging.getLogger(__name__)

_SQLI_PATTERNS = [
    re.compile(r"(?i)(?:'|\"|`)\s*or\s+1\s*=\s*1"),
    re.compile(r"(?i)\bunion\s+select\b"),
    re.compile(r"(?i);\s*(?:drop|truncate|alter|delete|insert|update|create)\b"),
    re.compile(r"(--|/\*|\*/)"),
]

_PUBLISH_TAGS_RU_TO_EN = [
    ("Практичный инструмент", "practical_tool"),
    ("Практичный кейс", "practical_case"),
    ("Радар индустрии", "industry_watch"),
    ("Релевантно РФ", "ru_relevance"),
    ("Вау-эффект", "wow_positive"),
    ("Влияние в будущем", "future_impact"),
    ("Влияние на бизнес", "business_impact"),
]

_DELETE_TAGS_RU_TO_EN = [
    ("Недостаточно контента", "insufficient_content"),
    ("Слишком техническое", "too_technical"),
    ("Политика/шум", "politics_noise"),
    ("Инвестиции/оценка", "investment_noise"),
    ("Кадровые назначения", "hiring_roles_noise"),
    ("Низкая значимость", "low_significance"),
    ("Нет практической пользы", "no_business_use"),
    ("Не про AI/ML", "non_ai"),
    ("Дубль", "duplicate"),
]

_CUSTOM_TAG_INPUT_RE = re.compile(r"^\s*([a-z][a-z0-9_]{1,63})\s-\s(.{2,120})\s*$")
_TG_REVIEW_MAX_RETRIES = 3
_TG_REVIEW_BASE_DELAY_SECONDS = 0.7


def _tg_request(method: str, payload: dict, *, timeout: float = 30.0) -> dict:
    base = _bot_base_url()
    if not base:
        return {"ok": False, "error": "telegram_not_configured"}
    url = f"{base}/{method}"
    last_error = "telegram_request_failed"
    for attempt in range(1, _TG_REVIEW_MAX_RETRIES + 1):
        try:
            resp = httpx.post(url, json=payload, timeout=timeout)
            data = resp.json()
            if data.get("ok"):
                return {"ok": True, "result": data.get("result") or {}}
            error_code = int(data.get("error_code") or 0)
            retry_after = 0
            try:
                retry_after = int((data.get("parameters") or {}).get("retry_after") or 0)
            except Exception:
                retry_after = 0
            if error_code == 429 and attempt < _TG_REVIEW_MAX_RETRIES:
                time.sleep(max(_TG_REVIEW_BASE_DELAY_SECONDS, float(retry_after or 1)))
                continue
            last_error = str(data)
        except Exception as exc:
            last_error = str(exc)
            if attempt < _TG_REVIEW_MAX_RETRIES:
                time.sleep(_TG_REVIEW_BASE_DELAY_SECONDS * attempt)
                continue
    return {"ok": False, "error": last_error}


def _reason_scope_for_action(action: str) -> str:
    action_l = str(action or "").strip().lower()
    if action_l in {"publish_now", "schedule_1h", "schedule_custom", "publish"}:
        return "publish"
    return "delete"


def _reason_tag_scope_kv_key(slug: str) -> str:
    return f"telegram_reason_tag_scope:{_normalize_reason_tag_slug(slug)}"


def _set_reason_tag_scope(slug: str, scope: str) -> None:
    clean_slug = _normalize_reason_tag_slug(slug)
    clean_scope = str(scope or "").strip().lower()
    if not clean_slug or clean_scope not in {"publish", "delete", "both"}:
        return
    with session_scope() as session:
        current = (_get_kv(session, _reason_tag_scope_kv_key(clean_slug), "") or "").strip().lower()
        if current in {"publish", "delete"} and clean_scope in {"publish", "delete"} and current != clean_scope:
            _set_kv(session, _reason_tag_scope_kv_key(clean_slug), "both")
            return
        if current != clean_scope:
            _set_kv(session, _reason_tag_scope_kv_key(clean_slug), clean_scope)


def _parse_ml_reason_payload(raw: str | None) -> tuple[str, list[str], float | None]:
    text = str(raw or "").strip()
    if not text:
        return "", [], None
    lines = [ln.strip() for ln in text.replace("\r", "").split("\n") if ln.strip()]
    reason = ""
    tags: list[str] = []
    ml_prob: float | None = None
    for ln in lines:
        low = ln.lower()
        if low.startswith("reason_text="):
            reason = ln.split("=", 1)[1].strip()
        elif low.startswith("reason=") and not reason:
            reason = ln.split("=", 1)[1].strip()
        elif low.startswith("tags="):
            tags = [x.strip() for x in ln.split("=", 1)[1].split(",") if x.strip()]
        elif low.startswith("ml_prob="):
            try:
                ml_prob = float(ln.split("=", 1)[1].strip())
            except Exception:
                ml_prob = None
    if not reason:
        reason = text[:220]
    return reason, tags, ml_prob

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


def _safe_user_id_int(user_id: str | None) -> int | None:
    try:
        return int(str(user_id or "").strip())
    except Exception:
        return None


def _safe_log_training_event(**kwargs) -> None:
    try:
        log_training_event(**kwargs)
    except Exception as exc:
        logger.warning(
            "tg training_event_failed error=%s article_id=%s decision=%s label=%s",
            str(exc),
            kwargs.get("article_id"),
            kwargs.get("decision"),
            kwargs.get("label"),
        )


def _bot_base_url() -> str | None:
    token = (telegram_bot_token() or settings.telegram_bot_token or "").strip()
    if not token:
        return None
    return f"https://api.telegram.org/bot{token}"


def _review_chat_id() -> str:
    return (telegram_review_chat_id() or get_runtime_str("telegram_review_chat_id") or settings.telegram_review_chat_id or "").strip()

def _hour_slot_key() -> str:
    """
    Slot key for deduping hourly notifications (in user's configured timezone).

    IMPORTANT: This refers to the *previous completed* hour window.
    Example: now=20:15 => slot is 19:00-20:00, key is "...20" (end hour).
    """
    try:
        tz = ZoneInfo(telegram_timezone_name() or get_runtime_str("timezone_name") or "Europe/Moscow")
    except Exception:
        tz = ZoneInfo("Europe/Moscow")

    now = datetime.now(tz=tz)
    end = now.replace(minute=0, second=0, microsecond=0)
    # If we are exactly at HH:00, still treat it as the previous hour window.
    if now == end:
        end = end - timedelta(hours=1)
    end = end  # end is the end of previous window
    return end.strftime("%Y%m%d%H")

def _hour_window_label_ru(start: datetime, end: datetime, tz_label: str) -> str:
    """
    Human label for the current hour window in user's timezone.
    Example: "Новость 18 февраля 2026 года с 18:00 до 19:00 (МСК)"
    """
    months = [
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
    ]
    m = months[start.month - 1] if 1 <= start.month <= 12 else str(start.month)
    return f"Новость {start.day} {m} {start.year} года с {start:%H:%M} до {end:%H:%M} ({tz_label})"

def _current_window_local() -> tuple[datetime, datetime, str]:
    """
    Previous completed hour window in user's timezone.
    Returns (start_local, end_local, tz_label).
    """
    try:
        tz_name = telegram_timezone_name() or get_runtime_str("timezone_name") or "Europe/Moscow"
        tz = ZoneInfo(tz_name)
    except Exception:
        tz_name = "Europe/Moscow"
        tz = ZoneInfo("Europe/Moscow")
    tz_label = "МСК" if tz_name == "Europe/Moscow" else tz_name

    now = datetime.now(tz=tz)
    end = now.replace(minute=0, second=0, microsecond=0)
    if now == end:
        end = end - timedelta(hours=1)
    start = end - timedelta(hours=1)
    return start, end, tz_label


def _previous_completed_window_local(hours: int = 1) -> tuple[datetime, datetime, str]:
    hours_n = max(1, int(hours or 1))
    try:
        tz_name = telegram_timezone_name() or get_runtime_str("timezone_name") or "Europe/Moscow"
        tz = ZoneInfo(tz_name)
    except Exception:
        tz_name = "Europe/Moscow"
        tz = ZoneInfo("Europe/Moscow")
    tz_label = "МСК" if tz_name == "Europe/Moscow" else tz_name

    now = datetime.now(tz=tz)
    end = now.replace(minute=0, second=0, microsecond=0)
    if now == end:
        end = end - timedelta(hours=1)
    start = end - timedelta(hours=hours_n)
    return start, end, tz_label

def _slot_window_local(slot_key: str) -> tuple[datetime, datetime, str]:
    """
    Convert slot key (YYYYMMDDHH) to a local hour window in user's timezone.
    Slot key represents the END of the previous hour window in local time.
    """
    try:
        tz_name = telegram_timezone_name() or get_runtime_str("timezone_name") or "Europe/Moscow"
        tz = ZoneInfo(tz_name)
    except Exception:
        tz_name = "Europe/Moscow"
        tz = ZoneInfo("Europe/Moscow")
    tz_label = "МСК" if tz_name == "Europe/Moscow" else tz_name

    try:
        end_local = datetime.strptime(slot_key, "%Y%m%d%H").replace(tzinfo=tz)
    except Exception:
        return _current_window_local()
    start_local = end_local - timedelta(hours=1)
    return start_local, end_local, tz_label

def _window_for_article_local(article: Article, tz: ZoneInfo) -> tuple[datetime, datetime, str]:
    # If article already has an hourly bucket (UTC naive), use it.
    if getattr(article, "selected_hour_bucket_utc", None):
        bucket_start_utc = article.selected_hour_bucket_utc.replace(tzinfo=ZoneInfo("UTC"))
        start_local = bucket_start_utc.astimezone(tz)
        end_local = (bucket_start_utc + timedelta(hours=1)).astimezone(tz)
        return start_local, end_local, ("МСК" if str(tz) == "Europe/Moscow" else str(tz))
    start_local, end_local, tz_label = _current_window_local()
    return start_local, end_local, tz_label

def _format_dt_ru(dt_local: datetime) -> str:
    months = [
        "января",
        "февраля",
        "марта",
        "апреля",
        "мая",
        "июня",
        "июля",
        "августа",
        "сентября",
        "октября",
        "ноября",
        "декабря",
    ]
    m = months[dt_local.month - 1] if 1 <= dt_local.month <= 12 else str(dt_local.month)
    return f"{dt_local.day} {m} {dt_local.year}, {dt_local:%H:%M}"


def _age_ru(now_local: datetime, published_local: datetime) -> str:
    delta = now_local - published_local
    secs = int(max(0, delta.total_seconds()))
    mins = secs // 60
    hours = mins // 60
    days = hours // 24
    if days > 0:
        h = hours % 24
        return f"{days}д {h}ч назад"
    if hours > 0:
        m = mins % 60
        return f"{hours}ч {m}м назад"
    return f"{mins}м назад"


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
        # Prefix with previous completed hour window so operator always sees the exact hour context.
        if (text or "").strip().startswith("Новость "):
            payload_text = text
        else:
            w = _current_window_local()
            payload_text = f"{_hour_window_label_ru(*w)}\n{text}"
        sent = _send_message(chat_id=chat_id, text=payload_text)
        if sent.get("ok"):
            _set_kv(session, kv_key, slot)
            _set_kv(session, "telegram_review_status_kind", kind[:64])
    return {"ok": True, "slot": slot, "kind": kind}


# Backward-compatible alias (internal name used by older worker code).
_send_review_status_once_per_hour = send_review_status_once_per_hour


def _build_review_text(
    article: Article,
    window: tuple[datetime, datetime, str] | None = None,
    *,
    origin: str = "hourly",
) -> str:
    title = (article.ru_title or "").strip()
    summary = (article.ru_summary or "").strip()
    if not title or not summary:
        if settings.allow_online_llm_generation:
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
    badge_map = {
        "tool": "🧰 Инструмент",
        "case": "🧪 Кейс",
        "hot": "⚡ Срочная новость",
        "playbook": "🛠 Пошаговый гайд",
        "trend": "🧭 Тренд",
    }
    badge = badge_map.get((article.content_type or "").strip().lower(), "")
    url = escape((article.canonical_url or "").strip())
    signature = escape(telegram_signature() or get_runtime_str("telegram_signature") or settings.telegram_signature or "@neuro_vibes_future")
    source_name = ""
    try:
        source_name = (article.source.name or "").strip()
    except Exception:
        source_name = ""

    try:
        tz_name = telegram_timezone_name() or get_runtime_str("timezone_name") or "Europe/Moscow"
        tz = ZoneInfo(tz_name)
    except Exception:
        tz_name = "Europe/Moscow"
        tz = ZoneInfo("Europe/Moscow")
    now_local = datetime.now(tz=tz)
    published_base = article.published_at or article.created_at
    published_utc = published_base.replace(tzinfo=ZoneInfo("UTC"))
    published_local = published_utc.astimezone(tz)
    pub_line = f"Опубликовано: {_format_dt_ru(published_local)} ({_age_ru(now_local, published_local)})"
    if (now_local - published_local) > timedelta(hours=24):
        pub_line = "⚠️ " + pub_line

    meta = escape(pub_line)
    if source_name:
        meta = meta + "\n" + escape("Источник: " + source_name)

    selector_line = ""
    criteria_lines: list[str] = []
    ml_reason_line = ""
    ml_tags_line = ""
    with session_scope() as session:
        selection = session.scalars(
            select(SelectionDecision)
            .where(SelectionDecision.chosen_article_id == int(article.id))
            .order_by(SelectionDecision.created_at.desc())
            .limit(1)
        ).first()
        score = session.get(Score, int(article.id))
        ml_reason_text, ml_reason_tags, ml_prob_from_reason = _parse_ml_reason_payload(article.ml_recommendation_reason)
        if selection is not None:
            selector_kind = (selection.selector_kind or "").strip().lower()
            if selector_kind == "ml":
                selector_line = "Выбор: ML-кандидат на публикацию"
                if selection.confidence is not None:
                    selector_line += f" (confidence {float(selection.confidence):.3f})"
                    criteria_lines.append(f"Вероятность публикации: {float(selection.confidence):.3f}")
            elif selector_kind == "script":
                selector_line = "Выбор: скрипт/правила"
            elif selector_kind:
                selector_line = f"Выбор: {selector_kind}"

            chosen = None
            for cand in list(selection.candidates or []):
                try:
                    if int(cand.get("article_id") or 0) == int(article.id):
                        chosen = cand
                        break
                except Exception:
                    continue
            if isinstance(chosen, dict):
                model_version = str(chosen.get("model_version") or "").strip()
                if model_version:
                    criteria_lines.append("Версия модели: " + model_version)
                top_drivers = [str(x).strip() for x in list(chosen.get("top_drivers") or []) if str(x).strip()]
                if top_drivers:
                    criteria_lines.append("Факторы: " + "; ".join(top_drivers[:3]))
                novelty_reason = str(chosen.get("novelty_reason") or "").strip()
                if novelty_reason:
                    criteria_lines.append("Комментарий: " + novelty_reason[:180])
        confidence_01 = None
        if isinstance(ml_prob_from_reason, float):
            confidence_01 = ml_prob_from_reason
        elif article.ml_recommendation_confidence is not None:
            confidence_01 = float(article.ml_recommendation_confidence)
        elif score is not None and score.final_score is not None:
            confidence_01 = float(score.final_score)
        if confidence_01 is not None:
            criteria_lines.append(f"Уверенность ML: {max(0.0, min(10.0, float(confidence_01) * 10.0)):.1f}/10")
        if ml_reason_text:
            ml_reason_line = "Причина ML: " + ml_reason_text[:220]
        if ml_reason_tags:
            ml_tags_line = "Теги ML: " + ", ".join(ml_reason_tags[:8])

    if selector_line:
        meta = meta + "\n" + escape(selector_line)
    if criteria_lines:
        meta = meta + "\n" + escape("Критерии ML:")
        for line in criteria_lines:
            meta = meta + "\n" + escape(f"• {line}")
    if ml_reason_line:
        meta = meta + "\n" + escape(ml_reason_line)
    if ml_tags_line:
        meta = meta + "\n" + escape(ml_tags_line)

    origin_l = str(origin or "hourly").strip().lower()
    if origin_l == "request":
        body = (
            "Статья по запросу\n\n"
            f"{meta}\n\n"
        )
    else:
        if window is None:
            start_local, end_local, tz_label = _window_for_article_local(article, tz)
        else:
            start_local, end_local, tz_label = window
        window_label = _hour_window_label_ru(start_local, end_local, tz_label)
        body = (
            f"{escape(window_label)}\n"
            "Кандидат на публикацию. Публиковать?\n\n"
            f"{meta}\n\n"
        )
    if badge:
        body += f"{escape(badge)}\n\n"
    body += (
        f"<b>{escape(title)}</b>\n\n"
        f"{escape(summary)}\n"
        f"<a href=\"{url}\">Подробнее</a>\n\n"
        f"{signature}"
    )
    return body


def _send_message(chat_id: str, text: str, reply_markup: dict | None = None, force_reply: bool = False) -> dict:
    payload: dict = {"chat_id": chat_id, "text": text[:4096], "parse_mode": "HTML"}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    if force_reply:
        payload["reply_markup"] = {"force_reply": True, "input_field_placeholder": "Напиши причину и отправь ответом"}
    return _tg_request("sendMessage", payload, timeout=30)


def _answer_callback(callback_query_id: str, text: str = "") -> None:
    _tg_request("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text[:180]}, timeout=20)


def _edit_message_reply_markup(chat_id: str, message_id: str, reply_markup: dict | None = None) -> None:
    if not chat_id or not message_id:
        return
    markup = reply_markup if reply_markup is not None else {"inline_keyboard": []}
    _tg_request(
        "editMessageReplyMarkup",
        {"chat_id": chat_id, "message_id": int(message_id), "reply_markup": markup},
        timeout=20,
    )


def _edit_message_caption_action(chat_id: str, message_id: str, action_label: str) -> None:
    if not chat_id or not message_id:
        return
    _tg_request(
        "editMessageReplyMarkup",
        {"chat_id": chat_id, "message_id": int(message_id), "reply_markup": {"inline_keyboard": []}},
        timeout=20,
    )


def _delete_message(chat_id: str, message_id: str) -> None:
    if not chat_id or not message_id:
        return
    _tg_request("deleteMessage", {"chat_id": chat_id, "message_id": int(message_id)}, timeout=20)


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


def _pending_tags_key(chat_id: str, article_id: int, action: str) -> str:
    return f"telegram_pending_tags:{chat_id}:{int(article_id)}:{(action or '').strip().lower()}"


def _append_pending_tag(chat_id: str, article_id: int, action: str, tag: str) -> list[str]:
    clean_tag = str(tag or "").strip().lower()
    if not clean_tag:
        return []
    key = _pending_tags_key(chat_id, article_id, action)
    with session_scope() as session:
        raw = (_get_kv(session, key, "") or "").strip()
        tags = [x.strip() for x in raw.split(",") if x.strip()]
        if clean_tag not in tags:
            tags.append(clean_tag)
        _set_kv(session, key, ",".join(tags))
        return tags


def _consume_pending_tags(chat_id: str, article_id: int, action: str) -> list[str]:
    key = _pending_tags_key(chat_id, article_id, action)
    with session_scope() as session:
        raw = (_get_kv(session, key, "") or "").strip()
        tags = [x.strip() for x in raw.split(",") if x.strip()]
        row = session.get(TelegramBotKV, key)
        if row is not None:
            session.delete(row)
        return tags


def _build_tag_picker_kb(article_id: int, action: str, selected_tags: list[str] | None = None) -> dict:
    action_l = (action or "").strip().lower()
    scope = _reason_scope_for_action(action_l)
    selected = {str(x or "").strip().lower() for x in (selected_tags or []) if str(x or "").strip()}
    base_pairs = _PUBLISH_TAGS_RU_TO_EN if action_l in {"publish_now", "schedule_1h", "schedule_custom"} else _DELETE_TAGS_RU_TO_EN
    pairs: list[tuple[str, str]] = list(base_pairs)
    for _, slug in base_pairs:
        _set_reason_tag_scope(slug, scope)
    existing_slugs = {str(slug or "").strip().lower() for _, slug in pairs}
    with session_scope() as session:
        rows = session.scalars(
            select(ReasonTagCatalog)
            .where(ReasonTagCatalog.is_active.is_(True))
            .order_by(ReasonTagCatalog.updated_at.desc(), ReasonTagCatalog.created_at.desc())
            .limit(300)
        ).all()
        for row in rows:
            slug = _normalize_reason_tag_slug(row.slug or "")
            if not slug or slug in existing_slugs:
                continue
            tag_scope = (_get_kv(session, _reason_tag_scope_kv_key(slug), "") or "").strip().lower()
            if tag_scope not in {"both", scope}:
                continue
            title = (row.title_ru or "").strip() or slug
            pairs.append((title, slug))
            existing_slugs.add(slug)
    rows: list[list[dict[str, str]]] = []
    row: list[dict[str, str]] = []
    for ru_label, en_tag in pairs:
        if str(en_tag or "").strip().lower() in selected:
            continue
        row.append({"text": ru_label, "callback_data": f"rv:tag:{int(article_id)}:{action_l}:{en_tag}"})
        if len(row) >= 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "Добавить тег (key - label)", "callback_data": f"rv:addtag:{int(article_id)}:{action_l}"}])
    rows.append([{"text": "Готово, ввести причину", "callback_data": f"rv:tagdone:{int(article_id)}:{action_l}"}])
    return {"inline_keyboard": rows}


def _normalize_reason_tag_slug(raw: str) -> str:
    val = str(raw or "").strip().lower()
    val = re.sub(r"[^a-z0-9_]+", "_", val)
    val = re.sub(r"_+", "_", val).strip("_")
    return val[:64]


def _parse_custom_tag_line(raw: str) -> tuple[str | None, str | None, str | None]:
    text = str(raw or "").strip()
    m = _CUSTOM_TAG_INPUT_RE.match(text)
    if not m:
        return None, None, "Неверный формат. Нужно строго: key - label (с пробелами вокруг дефиса)."
    slug = _normalize_reason_tag_slug(m.group(1))
    title_ru = re.sub(r"\s+", " ", m.group(2)).strip()
    if not slug:
        return None, None, "Ключ тега пустой."
    if not title_ru:
        return None, None, "Лейбл тега пустой."
    return slug, title_ru[:120], None


def _upsert_reason_tag_catalog(
    slug: str,
    title_ru: str,
    created_by_user_id: int | None = None,
    scope: str | None = None,
) -> None:
    if not slug or not title_ru:
        return
    with session_scope() as session:
        row = session.scalars(select(ReasonTagCatalog).where(ReasonTagCatalog.slug == slug)).first()
        if row:
            row.title_ru = title_ru
            row.is_active = True
            row.updated_at = datetime.utcnow()
            return
        session.add(
            ReasonTagCatalog(
                slug=slug,
                title_ru=title_ru,
                description="",
                is_active=True,
                is_system=False,
                created_by_user_id=created_by_user_id,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
        )
    if scope:
        _set_reason_tag_scope(slug, _reason_scope_for_action(scope))


def _review_actions_kb(article_id: int) -> dict:
    return {
        "inline_keyboard": [
            [
                {"text": "Опубликовать", "callback_data": f"rv:pub:{int(article_id)}"},
                {"text": "Скрыть", "callback_data": f"rv:hide:{int(article_id)}"},
                {"text": "Удалить", "callback_data": f"rv:del:{int(article_id)}"},
            ],
        ]
    }


def _pick_best_unsorted_article_id() -> int | None:
    with session_scope() as session:
        selected_any_day_ids = list(
            int(x)
            for x in session.scalars(select(DailySelection.article_id).where(DailySelection.active.is_(True))).all()
        )
        recent_days = int(max(1, get_runtime_int("unsorted_recent_days", default=3)))
        cutoff = datetime.utcnow() - timedelta(days=recent_days)
        q = (
            select(Article.id)
            .join(Score, Score.article_id == Article.id, isouter=True)
            .where(
                Article.status != ArticleStatus.ARCHIVED,
                Article.status != ArticleStatus.PUBLISHED,
                Article.status != ArticleStatus.SELECTED_HOURLY,
                Article.status != ArticleStatus.REJECTED,
                Article.created_at >= cutoff,
            )
            .order_by(Score.final_score.desc().nullslast(), Article.created_at.desc())
            .limit(1)
        )
        if selected_any_day_ids:
            q = q.where(Article.id.not_in(list(set(int(x) for x in selected_any_day_ids))))
        row = session.execute(q).first()
        return int(row[0]) if row else None


def _pick_best_recent_ml_article_id() -> int | None:
    """Fallback candidate for /new_article when unsorted queue is empty.

    Picks the strongest recent article by ML confidence, then score, then recency.
    """
    with session_scope() as session:
        q = (
            select(Article.id)
            .join(Score, Score.article_id == Article.id, isouter=True)
            .where(
                Article.status != ArticleStatus.PUBLISHED,
                Article.status != ArticleStatus.ARCHIVED,
                Article.status != ArticleStatus.REJECTED,
                Article.status != ArticleStatus.DOUBLE,
                Article.ml_recommendation_confidence.is_not(None),
            )
            .order_by(
                Article.ml_recommendation_confidence.desc().nullslast(),
                Score.final_score.desc().nullslast(),
                Article.created_at.desc(),
            )
            .limit(1)
        )
        row = session.execute(q).first()
        return int(row[0]) if row else None


def send_best_unsorted_for_review(chat_id: str | None = None) -> dict:
    configured_chat = _review_chat_id()
    if not configured_chat and not chat_id:
        return {"ok": False, "error": "telegram_review_chat_not_configured"}
    with session_scope() as session:
        runtime_chat = _get_kv(session, "telegram_review_runtime_chat_id", "").strip()
    target_chat = str(chat_id or runtime_chat or configured_chat).strip()
    if not target_chat:
        return {"ok": False, "error": "telegram_review_chat_not_configured"}

    target_article_id = _pick_best_unsorted_article_id()
    fallback_used = False
    if not target_article_id:
        target_article_id = _pick_best_recent_ml_article_id()
        fallback_used = bool(target_article_id)
    if not target_article_id:
        return {"ok": False, "error": "no_unsorted_candidate"}

    try:
        score_article_by_id(int(target_article_id))
    except Exception:
        pass
    if settings.allow_online_llm_generation:
        try:
            generate_ru_summary(int(target_article_id))
        except Exception:
            pass

    with session_scope() as session:
        article = session.get(Article, int(target_article_id))
        if not article:
            return {"ok": False, "error": "article_not_found"}
        text = _build_review_text(article, origin="request")

    out = _send_message(chat_id=target_chat, text=text, reply_markup=_review_actions_kb(int(target_article_id)))
    if not out.get("ok"):
        return {"ok": False, "error": out.get("error"), "article_id": int(target_article_id), "chat_id": target_chat}
    message_id = str((out.get("result") or {}).get("message_id") or "")
    with session_scope() as session:
        existing = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == int(target_article_id))).first()
        if existing:
            existing.chat_id = target_chat
            existing.review_message_id = message_id or None
            existing.status = "resent"
            existing.updated_at = datetime.utcnow()
        else:
            session.add(
                TelegramReviewJob(
                    article_id=int(target_article_id),
                    chat_id=target_chat,
                    review_message_id=message_id or None,
                    status="sent",
                    created_at=datetime.utcnow(),
                    updated_at=datetime.utcnow(),
                )
            )
    return {
        "ok": True,
        "article_id": int(target_article_id),
        "chat_id": target_chat,
        "message_id": message_id,
        "fallback": fallback_used,
    }


def _reason_with_tags(action: str, reason: str, tags: list[str]) -> str:
    txt = (reason or "").strip()
    clean_tags = [str(x).strip().lower() for x in (tags or []) if str(x).strip()]
    tag_set = set(clean_tags)
    action_l = (action or "").strip().lower()

    decision = "publish"
    if action_l in {"delete", "hide"}:
        decision = "delete"
    elif action_l in {"schedule_1h", "schedule_custom", "later"}:
        decision = "defer"

    ai_ml_relevance = 0 if "non_ai" in tag_set else 1
    audience_fit = 0 if "too_technical" in tag_set else 1
    practical_value = 0 if "no_business_use" in tag_set else 1
    risk_level_ok = 0 if {"politics_noise", "investment_noise", "hiring_roles_noise"} & tag_set else 1

    novelty_positive = {
        "practical_tool",
        "practical_case",
        "ru_relevance",
        "wow_positive",
        "future_impact",
        "business_impact",
    }
    novelty_signal = 1 if (novelty_positive & tag_set) else 0

    lines = [
        f"decision={decision}",
        f"ai_ml_relevance={ai_ml_relevance}",
        f"audience_fit={audience_fit}",
        f"practical_value={practical_value}",
        f"risk_level_ok={risk_level_ok}",
        f"novelty_signal={novelty_signal}",
        f"tags={','.join(clean_tags)}" if clean_tags else "tags=",
        f"reason_text={txt}",
    ]
    return "\n".join(lines)


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

        # One message per hour-window slot (unless forced/manual).
        slot = _hour_slot_key()
        last_slot = (_get_kv(session, "telegram_review_last_article_slot", "") or "").strip()
        if last_slot == slot and not force:
            return {"ok": True, "skipped": "already_sent_slot", "slot": slot, "article_id": article.id}

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
        interval_hours = int(max(1, round(get_runtime_float("ml_review_every_n_hours", default=2.0))))
        window = _previous_completed_window_local(hours=interval_hours)
        text = _build_review_text(article, window=window, origin="hourly")

    markup = _review_actions_kb(target_article_id)
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

        markup = _review_actions_kb(target_article_id)
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


def send_hourly_backfill_for_review(hours_back: int = 24, limit: int = 24, force: bool = False) -> dict:
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

        existing_job: TelegramReviewJob | None = None
        with session_scope() as session:
            existing_job = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == target_article_id)).first()
            if existing_job is not None and not force:
                skipped_exists += 1
                continue
            article = session.get(Article, target_article_id)
            if not article:
                continue
            text = _build_review_text(article)

        markup = _review_actions_kb(target_article_id)
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
            job = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == target_article_id)).first()
            if job is None:
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
            else:
                # UniqueConstraint(article_id) prevents multiple jobs per article. On forced resend,
                # update the job to point to the newest preview message.
                job.chat_id = chat_id
                job.review_message_id = message_id or None
                job.status = "resent" if force else (job.status or "sent")
                job.updated_at = datetime.utcnow()
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


def _disable_webhook_once() -> None:
    """
    This project uses getUpdates polling. If webhook is set, Telegram will not deliver updates
    via getUpdates (buttons will appear to "do nothing"). So we disable it once per bot token.
    """
    base = _bot_base_url()
    if not base:
        return
    with session_scope() as session:
        done = (_get_kv(session, "telegram_review_webhook_disabled", "0") or "0").strip()
        if done == "1":
            return
        try:
            httpx.post(f"{base}/deleteWebhook", json={"drop_pending_updates": False}, timeout=20)
        except Exception:
            return
        _set_kv(session, "telegram_review_webhook_disabled", "1")


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

    # Do not log raw callback payload to avoid leaking user data/tags.
    logger.debug("tg callback chat_id=%s message_id=%s user_id=%s", chat_id, message_id, user_id)

    if not data.startswith("rv:") or not chat_id:
        if callback_id:
            _answer_callback(callback_id, "ok")
        return {"ok": True, "skipped": "not_review_callback"}

    parts = data.split(":")
    if len(parts) < 3 or not parts[2].isdigit():
        if callback_id:
            _answer_callback(callback_id, "ok")
        return {"ok": True, "skipped": "bad_callback"}
    action = parts[1]
    article_id = int(parts[2])

    # Fast feedback in Telegram (toast) so user sees the click was received.
    if callback_id:
        ack_map = {
            "pub": "pending: выбор времени",
            "pubnow": "pending: жду причину",
            "pub1h": "pending: +1 час",
            "pubpick": "pending: жду время",
            "del": "pending: жду причину",
            "hide": "pending: жду причину",
            "later": "pending: жду причину",
            "review": "ищу лучшую статью",
            "addtag": "жду формат: key - label",
        }
        _answer_callback(callback_id, ack_map.get(action, "pending"))

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

    if action == "tag":
        if len(parts) < 5:
            return {"ok": True, "skipped": "bad_tag_callback"}
        pending_action = str(parts[3] or "").strip().lower()
        tag = str(parts[4] or "").strip().lower()
        tags = _append_pending_tag(chat_id, article_id, pending_action, tag)
        # Refresh picker in-place: hide already selected tags so user sees progress.
        kb = _build_tag_picker_kb(article_id, pending_action, selected_tags=tags)
        _edit_message_reply_markup(chat_id, message_id, reply_markup=kb)
        if callback_id:
            _answer_callback(callback_id, f"Добавлено тегов: {len(tags)}")
        return {"ok": True, "action": "tag_added", "article_id": article_id, "pending_action": pending_action, "tag": tag, "tags": tags}

    if action == "tagdone":
        if len(parts) < 4:
            return {"ok": True, "skipped": "bad_tagdone_callback"}
        pending_action = str(parts[3] or "").strip().lower()
        tags = _consume_pending_tags(chat_id, article_id, pending_action)
        _delete_message(chat_id, message_id)
        tags_line = ",".join(tags) if tags else "нет"
        prompt = _send_message(
            chat_id,
            f"Теги: {tags_line}\nНапиши краткую причину (1-2 предложения). Ответь реплаем.",
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
                            action=pending_action,
                            prompt_message_id=prompt_id,
                            created_at=datetime.utcnow(),
                        )
                    )
                    if tags:
                        _set_kv(session, _pending_tags_key(chat_id, article_id, f"{pending_action}:ready"), ",".join(tags))
        return {"ok": True, "action": "tag_done_wait_reason", "article_id": article_id, "pending_action": pending_action, "tags": tags}

    if action == "addtag":
        if len(parts) < 4:
            return {"ok": True, "skipped": "bad_addtag_callback"}
        pending_action = str(parts[3] or "").strip().lower()
        prompt = _send_message(
            chat_id,
            "Введи новый тег строго в формате: key - label\nПример: industry_watch - Радар индустрии",
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
                            action=f"tag_add:{pending_action}",
                            prompt_message_id=prompt_id,
                            created_at=datetime.utcnow(),
                        )
                    )
        return {"ok": True, "action": "tag_add_prompted", "article_id": article_id, "pending_action": pending_action}

    if action == "pubnow":
        kb = _build_tag_picker_kb(article_id, "publish_now")
        out = _send_message(chat_id, "Выбери теги причины публикации (можно несколько), потом нажми «Готово»:", reply_markup=kb)
        if out.get("ok"):
            # This callback comes from the temporary "Когда публиковать?" chooser; remove it entirely.
            _delete_message(chat_id, message_id)
        else:
            _send_message(chat_id, f"Не удалось открыть выбор тегов. Попробуй ещё раз.\n{out.get('error','')[:180]}")
        return {"ok": True, "action": "publish_now_pick_tags", "article_id": article_id}

    if action == "pub1h":
        with session_scope() as session:
            article = session.get(Article, article_id)
            if article:
                article.scheduled_publish_at = datetime.utcnow() + timedelta(hours=1)
                article.updated_at = datetime.utcnow()
        kb = _build_tag_picker_kb(article_id, "schedule_1h")
        out = _send_message(chat_id, "Поставил +1 час. Выбери теги причины, затем нажми «Готово»:", reply_markup=kb)
        if out.get("ok"):
            # This callback comes from the temporary "Когда публиковать?" chooser; remove it entirely.
            _delete_message(chat_id, message_id)
        else:
            _send_message(chat_id, f"Не удалось открыть выбор тегов. Попробуй ещё раз.\n{out.get('error','')[:180]}")
        return {"ok": True, "action": "publish_plus_1h_pick_tags", "article_id": article_id}

    if action == "pubpick":
        # This callback comes from the temporary "Когда публиковать?" chooser; remove it entirely.
        _delete_message(chat_id, message_id)
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
        # Immediately cancel any delayed publication while we wait for the reason.
        with session_scope() as session:
            art = session.get(Article, article_id)
            if art:
                art.scheduled_publish_at = None
                art.updated_at = datetime.utcnow()
        kb = _build_tag_picker_kb(article_id, "delete")
        out = _send_message(chat_id, "Выбери теги причины удаления (можно несколько), затем «Готово»:", reply_markup=kb)
        if out.get("ok"):
            _edit_message_reply_markup(chat_id, message_id)
        else:
            _send_message(chat_id, f"Не удалось открыть выбор тегов. Попробуй ещё раз.\n{out.get('error','')[:180]}")
        return {"ok": True, "action": "delete_pick_tags", "article_id": article_id}

    if action == "hide":
        # Immediately cancel any delayed publication while we wait for the reason.
        with session_scope() as session:
            art = session.get(Article, article_id)
            if art:
                art.scheduled_publish_at = None
                art.updated_at = datetime.utcnow()
        kb = _build_tag_picker_kb(article_id, "hide")
        out = _send_message(chat_id, "Выбери теги причины скрытия, затем «Готово»:", reply_markup=kb)
        if out.get("ok"):
            _edit_message_reply_markup(chat_id, message_id)
        else:
            _send_message(chat_id, f"Не удалось открыть выбор тегов. Попробуй ещё раз.\n{out.get('error','')[:180]}")
        return {"ok": True, "action": "hide_pick_tags", "article_id": article_id}

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

    cmd = text.strip().split()[0].lower() if text.strip().startswith("/") else ""
    if cmd in {"/new_article", "/review"}:
        out = send_best_unsorted_for_review(chat_id=chat_id or None)
        if not out.get("ok"):
            _send_message(chat_id, "Не нашёл подходящую статью для ревью прямо сейчас.")
        return {"ok": True, "action": "review_best_unsorted_cmd", **out}
    if cmd in {"/help", "/start"}:
        _send_message(
            chat_id,
            "Команды:\n"
            "/new_article — прислать лучшую статью на ревью\n"
            "/review — то же самое\n"
            "/help — показать это сообщение",
        )
        return {"ok": True, "action": "help_shown"}

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
    ready_tags = _consume_pending_tags(chat_id, article_id, f"{action}:ready")

    ok_input, safe_text, input_error = _sanitize_reason_input(text)
    if not ok_input:
        _send_message(chat_id, f"Не могу принять такой ввод: {input_error}. Напиши причину обычным текстом и ответь на то же сообщение.")
        return {"ok": True, "skipped": "invalid_reason_input"}

    if str(action).startswith("tag_add:"):
        pending_action = str(action).split(":", 1)[1].strip().lower()
        slug, title_ru, err = _parse_custom_tag_line(text)
        if err or not slug or not title_ru:
            _send_message(chat_id, "Не принял тег. Формат только: key - label (с пробелами вокруг дефиса).")
            return {"ok": True, "skipped": "invalid_custom_tag_format"}
        _upsert_reason_tag_catalog(
            slug,
            title_ru,
            created_by_user_id=_safe_user_id_int(user_id),
            scope=pending_action,
        )
        tags = _append_pending_tag(chat_id, article_id, pending_action, slug)
        with session_scope() as session:
            pending = session.scalars(
                select(TelegramPendingReason).where(
                    TelegramPendingReason.prompt_message_id == prompt_id,
                    TelegramPendingReason.chat_id == chat_id,
                )
            ).first()
            if pending:
                session.delete(pending)
        _delete_message(chat_id, prompt_id)
        if msg_id:
            _delete_message(chat_id, msg_id)
        kb = _build_tag_picker_kb(article_id, pending_action, selected_tags=tags)
        _send_message(chat_id, f"Добавил тег: {slug}. Выбери ещё теги и нажми «Готово».", reply_markup=kb)
        return {"ok": True, "action": "custom_tag_added", "article_id": article_id, "tag": slug, "pending_action": pending_action}

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
            reason_payload = _reason_with_tags(action, safe_text, ready_tags)
            session.add(EditorFeedback(article_id=article_id, explanation_text=reason_payload))
            job = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == article_id)).first()
            if job:
                job.decision_reason = reason_payload
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
        reason_payload = _reason_with_tags(action, safe_text, ready_tags)
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
            session.add(EditorFeedback(article_id=article_id, explanation_text=reason_payload))
            job = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == article_id)).first()
            if job:
                job.status = "published" if out.get("ok") else "failed"
                job.decision_reason = reason_payload
                job.updated_at = datetime.utcnow()
        if out.get("ok"):
            _safe_log_training_event(
                article_id=article_id,
                decision="publish",
                label=1,
                reason_text=reason_payload,
                user_id=_safe_user_id_int(user_id),
                override=False,
                final_outcome="published",
            )
        # Keep chat clean: remove bot prompt + user's reply + original preview message.
        _delete_message(chat_id, prompt_id)
        if msg_id:
            _delete_message(chat_id, msg_id)
        reason_payload = _reason_with_tags(action, safe_text, ready_tags)
        with session_scope() as session:
            job = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == article_id)).first()
            if job and (job.review_message_id or "").strip():
                _delete_message(chat_id, str(job.review_message_id))
        _post_decision_recalc()
        return {"ok": True, "action": "publish_now_done", "article_id": article_id, "publish": out}

    if action == "schedule_1h":
        reason_payload = _reason_with_tags(action, safe_text, ready_tags)
        with session_scope() as session:
            pending = session.scalars(
                select(TelegramPendingReason).where(
                    TelegramPendingReason.prompt_message_id == prompt_id,
                    TelegramPendingReason.chat_id == chat_id,
                )
            ).first()
            if pending:
                session.delete(pending)
            session.add(EditorFeedback(article_id=article_id, explanation_text=f"SCHEDULE(+1h): {reason_payload}"))
            job = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == article_id)).first()
            if job:
                job.status = "scheduled"
                job.decision_reason = reason_payload
                job.updated_at = datetime.utcnow()
        _safe_log_training_event(
            article_id=article_id,
            decision="defer",
            label=0,
            reason_text=reason_payload,
            user_id=_safe_user_id_int(user_id),
            final_outcome=None,
        )
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
        # Cleanup intermediate time-entry prompt and the user's reply with time.
        _delete_message(chat_id, prompt_id)
        if msg_id:
            _delete_message(chat_id, msg_id)
        kb = _build_tag_picker_kb(article_id, "schedule_custom")
        out = _send_message(
            chat_id,
            f"Ок, поставил публикацию на {dt_msk.strftime('%Y-%m-%d %H:%M')} МСК. "
            "Теперь выбери теги причины (можно несколько), затем «Готово»:",
            reply_markup=kb,
        )
        if not out.get("ok"):
            _send_message(chat_id, f"Не удалось открыть выбор тегов. Попробуй ещё раз.\n{out.get('error','')[:180]}")
        return {
            "ok": True,
            "action": "scheduled_custom_time_pick_tags",
            "article_id": article_id,
            "scheduled_utc": dt_utc.isoformat(sep=" ", timespec="seconds"),
        }

    if action == "schedule_custom":
        reason_payload = _reason_with_tags(action, safe_text, ready_tags)
        with session_scope() as session:
            pending = session.scalars(
                select(TelegramPendingReason).where(
                    TelegramPendingReason.prompt_message_id == prompt_id,
                    TelegramPendingReason.chat_id == chat_id,
                )
            ).first()
            if pending:
                session.delete(pending)
            session.add(EditorFeedback(article_id=article_id, explanation_text=f"SCHEDULE(custom): {reason_payload}"))
            job = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == article_id)).first()
            if job:
                job.status = "scheduled"
                job.decision_reason = reason_payload
                job.updated_at = datetime.utcnow()
        _safe_log_training_event(
            article_id=article_id,
            decision="defer",
            label=0,
            reason_text=reason_payload,
            user_id=_safe_user_id_int(user_id),
            final_outcome=None,
        )
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
        reason_payload = _reason_with_tags(action, safe_text, ready_tags)
        out = _archive_article_with_reason(article_id=article_id, reason=reason_payload)
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
                job.decision_reason = reason_payload
                job.updated_at = datetime.utcnow()
        if out.get("ok"):
            _safe_log_training_event(
                article_id=article_id,
                decision="delete",
                label=0,
                reason_text=reason_payload,
                user_id=_safe_user_id_int(user_id),
                final_outcome="deleted",
            )
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
        reason_payload = _reason_with_tags(action, safe_text, ready_tags)
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
                article.archived_reason = reason_payload
                article.archived_at = datetime.utcnow()
                article.updated_at = datetime.utcnow()
            session.add(EditorFeedback(article_id=article_id, explanation_text=f"HIDE: {reason_payload}"))
            job = session.scalars(select(TelegramReviewJob).where(TelegramReviewJob.article_id == article_id)).first()
            if job:
                job.status = "deleted"
                job.decision_reason = reason_payload
                job.updated_at = datetime.utcnow()
        _safe_log_training_event(
            article_id=article_id,
            decision="hide",
            label=0,
            reason_text=reason_payload,
            user_id=_safe_user_id_int(user_id),
            final_outcome="hidden",
        )
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
                job.status = "scheduled"
                job.decision_reason = f"later: {safe_text}"
                job.updated_at = datetime.utcnow()
        _safe_log_training_event(
            article_id=article_id,
            decision="defer",
            label=0,
            reason_text=safe_text,
            user_id=_safe_user_id_int(user_id),
            final_outcome=None,
        )
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

    # Ensure webhook is disabled; otherwise getUpdates will not receive callback queries.
    _disable_webhook_once()

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
        # If webhook was set, Telegram can respond with 409 Conflict in plain text.
        # Try disabling webhook and retry once.
        if resp.status_code == 409:
            _disable_webhook_once()
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
