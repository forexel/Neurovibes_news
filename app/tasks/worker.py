from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from app.db import init_db
from app.db import session_scope
from app.models import Article
from app.models import ArticleStatus
from app.models import EditorFeedback
from app.models import Source
from app.models import UserWorkspace
from app.models import TelegramBotKV
from sqlalchemy import case, func, select
from app.services.bootstrap import seed_sources
from app.services.pipeline import pick_hourly_top, run_hourly_cycle
from app.services.preference import reretag_today_training_event_reasons, train_editor_choice_model
from app.services.telegram_publisher import publish_scheduled_due
from app.services.telegram_review import (
    poll_review_updates,
    send_hourly_top_for_review,
    send_review_status_once_per_hour,
)
from app.services.telegram_context import load_workspace_telegram_context
from app.services.llm import get_workspace_api_key, set_user_api_key
from app.services.telegram_context import telegram_timezone_name
from app.services.runtime_settings import get_runtime_float
from app.services.runtime_settings import get_runtime_bool
from app.services.runtime_settings import get_runtime_int


INTERVAL_SECONDS = int(os.getenv("WORKER_INTERVAL_SECONDS", "3600"))
BACKFILL_DAYS = int(os.getenv("WORKER_BACKFILL_DAYS", "1"))
SCRAPER_SLOT_MINUTES = max(1, int(os.getenv("SCRAPER_SLOT_MINUTES", "30")))
# Telegram polling interval (controls how fast inline кнопки реагируют).
TELEGRAM_POLL_INTERVAL_SECONDS = float(os.getenv("TELEGRAM_POLL_INTERVAL_SECONDS", "2"))
# How often we check scheduled publications.
PUBLISH_TICK_SECONDS = float(os.getenv("PUBLISH_TICK_SECONDS", "10"))
# Legacy: keep for backwards compatibility (do NOT use it to delay Telegram polling).
SCHEDULE_TICK_SECONDS = int(os.getenv("SCHEDULE_TICK_SECONDS", "30"))
# If the hourly cycle hangs, we can't kill the thread, but we can surface it.
MAX_CYCLE_SECONDS = int(os.getenv("MAX_CYCLE_SECONDS", str(20 * 60)))
DAILY_ML_RETRAIN_HOUR = int(os.getenv("DAILY_ML_RETRAIN_HOUR", "0"))
DAILY_ML_RETRAIN_MINUTE = int(os.getenv("DAILY_ML_RETRAIN_MINUTE", "30"))

_DEFAULT_USER_ID: int | None = None
_DEFAULT_USER_LOADED_AT: float = 0.0


def _set_worker_kv(key: str, value: str) -> None:
    # Worker is single-process per deploy, KV is just for UI visibility.
    try:
        with session_scope() as session:
            row = session.get(TelegramBotKV, key)  # reuse existing KV table
            if row:
                row.value = value
                row.updated_at = datetime.now(timezone.utc).replace(tzinfo=None)
            else:
                session.add(TelegramBotKV(key=key, value=value, updated_at=datetime.now(timezone.utc).replace(tzinfo=None)))
    except Exception:
        pass


def _load_default_user_context() -> None:
    """
    Worker runs out of request context, so it must load per-user secrets (bot token, OpenRouter key)
    from DB. If not found, it will fall back to env/runtime settings.
    """
    global _DEFAULT_USER_ID
    global _DEFAULT_USER_LOADED_AT
    now = time.time()
    if _DEFAULT_USER_ID is not None and (now - _DEFAULT_USER_LOADED_AT) < 60.0:
        load_workspace_telegram_context(_DEFAULT_USER_ID)
        set_user_api_key(get_workspace_api_key(_DEFAULT_USER_ID))
        return

    user_id = None
    with session_scope() as session:
        ws = session.scalars(
            select(UserWorkspace)
            .where(
                UserWorkspace.telegram_bot_token_enc.is_not(None),
                UserWorkspace.telegram_bot_token_enc != "",
                UserWorkspace.telegram_review_chat_id.is_not(None),
                UserWorkspace.telegram_review_chat_id != "",
            )
            .order_by(UserWorkspace.updated_at.desc())
            .limit(1)
        ).first()
        if ws is not None:
            user_id = int(ws.user_id)

    _DEFAULT_USER_ID = user_id
    _DEFAULT_USER_LOADED_AT = now
    load_workspace_telegram_context(user_id)
    if user_id:
        set_user_api_key(get_workspace_api_key(user_id))
    else:
        set_user_api_key(None)


def _wall_clock_slot(now_ts: float, slot_minutes: int) -> tuple[str, bool, float]:
    """
    Align worker runs to wall-clock slots (:00/:30 by default), independent of process start time.
    Returns (slot_key_utc, is_full_hour_boundary, next_slot_ts).
    """
    slot_seconds = max(60, int(slot_minutes) * 60)
    slot_start_ts = int(now_ts // slot_seconds) * slot_seconds
    dt_utc = datetime.fromtimestamp(slot_start_ts, tz=timezone.utc)
    slot_key = dt_utc.strftime("%Y%m%d%H%M")
    is_full_hour = dt_utc.minute == 0
    return slot_key, is_full_hour, float(slot_start_ts + slot_seconds)


def _run_cycle_thread(backfill_days: int, decision_mode: bool, slot_key: str) -> None:
    # Cycle runs in background so Telegram polling stays responsive.
    _load_default_user_context()
    _set_worker_kv("worker_cycle_state", "running")
    _set_worker_kv("worker_last_cycle_error", "")
    _set_worker_kv("worker_last_cycle_start_utc", datetime.now(timezone.utc).isoformat())
    try:
        print(
            "[worker] cycle start",
            {
                "backfill_days": backfill_days,
                "slot": slot_key,
                "cycle_mode": "hour_close" if decision_mode else "half_hour_ingest",
            },
            flush=True,
        )
        result = run_hourly_cycle(backfill_days=backfill_days, select_hourly_top=decision_mode)
        print("[worker] cycle done", result, flush=True)

        top_article_id = result.get("top_article_id")
        ingest = result.get("ingestion") or {}
        inserted_total = 0
        try:
            inserted_total = int(sum(int(v or 0) for v in ingest.values()))
        except Exception:
            inserted_total = 0

        if not decision_mode:
            return

        # Strategy is intentionally disabled for this hour slot (e.g. ML every 2 hours).
        # Do not send "all filtered" noise in skipped slots.
        if str(result.get("selection_strategy") or "").strip().lower() == "off":
            return

        interval_hours = int(max(1, round(get_runtime_float("ml_review_every_n_hours", default=2.0))))
        hours_phrase = "час" if interval_hours == 1 else ("2 часа" if interval_hours == 2 else f"{interval_hours} часов")

        if top_article_id:
            # Auto-mode: never spam the same hour slot. Force resend is only for manual backfill endpoints.
            review_out = send_hourly_top_for_review(int(top_article_id), force=False)
            print("[worker] telegram review send", review_out, flush=True)
            # If the top did not change (article already sent previously), still send 1 status per hour window.
            if review_out.get("skipped") == "already_sent":
                status_out = send_review_status_once_per_hour(
                    "top_unchanged",
                    f"За последние {hours_phrase} новый топ не появился: лучший кандидат не изменился.",
                )
                print("[worker] telegram review status", status_out, flush=True)
            # If already sent for this slot, do nothing (we already produced 1 message for the window).
        else:
            if inserted_total <= 0:
                status_out = send_review_status_once_per_hour(
                    "no_new_articles",
                    f"За последние {hours_phrase} новых статей нет (по источникам пришло 0 новых ссылок).",
                )
            else:
                status_out = send_review_status_once_per_hour(
                    "all_filtered",
                    f"За последние {hours_phrase} новые статьи были (+{inserted_total}), но все не прошли фильтры/скоринг.",
                )
            print("[worker] telegram review status", status_out, flush=True)
    except Exception as exc:
        print(f"[worker] cycle failed: {exc}", flush=True)
        _set_worker_kv("worker_last_cycle_error", str(exc)[:800])
    finally:
        _set_worker_kv("worker_last_cycle_finish_utc", datetime.now(timezone.utc).isoformat())
        _set_worker_kv("worker_cycle_state", "idle")


def _worker_local_now() -> datetime:
    tz_name = (telegram_timezone_name() or "Europe/Moscow").strip() or "Europe/Moscow"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Moscow")
    return datetime.now(tz)


def _auto_disable_cold_sources() -> dict:
    enabled = get_runtime_bool("source_auto_disable_enabled", default=True)
    if not enabled:
        return {"ok": True, "enabled": False, "disabled": 0}
    days = int(max(1, get_runtime_int("source_auto_disable_days", default=14)))
    min_attempts = int(max(1, get_runtime_int("source_auto_disable_min_attempts", default=12)))
    since = datetime.utcnow() - timedelta(days=days)

    with session_scope() as session:
        rows = session.execute(
            select(
                Article.source_id.label("source_id"),
                func.count(Article.id).label("attempts"),
                func.sum(case((Article.status == ArticleStatus.PUBLISHED, 1), else_=0)).label("published"),
            )
            .where(
                Article.created_at >= since,
                Article.source_id.is_not(None),
            )
            .group_by(Article.source_id)
        ).all()

        disabled_ids: list[int] = []
        scanned = 0
        for row in rows:
            source_id = int(row.source_id or 0)
            if source_id <= 0:
                continue
            attempts = int(row.attempts or 0)
            published = int(row.published or 0)
            scanned += 1
            if attempts < min_attempts or published > 0:
                continue
            source = session.get(Source, source_id)
            if not source or not bool(source.is_active):
                continue
            source.is_active = False
            disabled_ids.append(source_id)
    return {
        "ok": True,
        "enabled": True,
        "days": days,
        "min_attempts": min_attempts,
        "scanned": scanned,
        "disabled": len(disabled_ids),
        "source_ids": disabled_ids[:100],
    }


def _run_daily_ml_maintenance_thread(local_day_key: str) -> None:
    _load_default_user_context()
    _set_worker_kv("worker_daily_ml_state", "running")
    _set_worker_kv("worker_daily_ml_error", "")
    _set_worker_kv("worker_daily_ml_started_at_utc", datetime.now(timezone.utc).isoformat())
    try:
        reasons_out = reretag_today_training_event_reasons(limit=50, overwrite=False)
        editor_out = train_editor_choice_model(days_back=1, min_samples=8)
        source_out = _auto_disable_cold_sources()
        result = {
            "reason_retag_today": reasons_out,
            "editor_choice_train": editor_out,
            "source_auto_disable": source_out,
        }
        print("[worker] daily ml maintenance", result, flush=True)
        _set_worker_kv("worker_daily_ml_last_run_local_date", local_day_key)
        _set_worker_kv("worker_daily_ml_last_result", str(result)[:3000])
    except Exception as exc:
        print(f"[worker] daily ml maintenance failed: {exc}", flush=True)
        _set_worker_kv("worker_daily_ml_error", str(exc)[:1000])
    finally:
        _set_worker_kv("worker_daily_ml_finished_at_utc", datetime.now(timezone.utc).isoformat())
        _set_worker_kv("worker_daily_ml_state", "idle")


def main() -> None:
    init_db()
    seed_sources()
    print(
        "[worker] started",
        {
            "interval_seconds": INTERVAL_SECONDS,
            "backfill_days": BACKFILL_DAYS,
            "scraper_slot_minutes": SCRAPER_SLOT_MINUTES,
        },
        flush=True,
    )
    last_cycle_slot_key = ""
    cycle_thread: threading.Thread | None = None
    daily_ml_thread: threading.Thread | None = None
    cycle_started_at: float | None = None
    last_publish_check_ts = 0.0
    while True:
        _load_default_user_context()
        now = time.time()
        cycle_running = bool(cycle_thread and cycle_thread.is_alive())
        if cycle_running and cycle_started_at and MAX_CYCLE_SECONDS > 0 and (now - cycle_started_at) > MAX_CYCLE_SECONDS:
            _set_worker_kv("worker_last_cycle_error", f"cycle_timeout>{MAX_CYCLE_SECONDS}s (still running)")

        current_slot_key, is_full_hour, next_slot_ts = _wall_clock_slot(now, SCRAPER_SLOT_MINUTES)
        _set_worker_kv("worker_next_cycle_utc", datetime.fromtimestamp(next_slot_ts, tz=timezone.utc).isoformat())

        if current_slot_key != last_cycle_slot_key and not cycle_running:
            last_cycle_slot_key = current_slot_key
            decision_mode = bool(is_full_hour)
            _set_worker_kv("worker_last_cycle_slot", current_slot_key)
            _set_worker_kv("worker_last_cycle_mode", "decision" if decision_mode else "ingest_only")
            cycle_thread = threading.Thread(
                target=_run_cycle_thread,
                args=(BACKFILL_DAYS, decision_mode, current_slot_key),
                daemon=True,
                name="hourly-cycle",
            )
            cycle_started_at = now
            cycle_thread.start()

        local_now = _worker_local_now()
        local_day_key = local_now.strftime("%Y-%m-%d")
        daily_ml_running = bool(daily_ml_thread and daily_ml_thread.is_alive())
        with session_scope() as session:
            last_daily_ml_day = (session.get(TelegramBotKV, "worker_daily_ml_last_run_local_date").value if session.get(TelegramBotKV, "worker_daily_ml_last_run_local_date") else "")
        if (
            not daily_ml_running
            and local_now.hour == DAILY_ML_RETRAIN_HOUR
            and local_now.minute >= DAILY_ML_RETRAIN_MINUTE
            and last_daily_ml_day != local_day_key
        ):
            daily_ml_thread = threading.Thread(
                target=_run_daily_ml_maintenance_thread,
                args=(local_day_key,),
                daemon=True,
                name="daily-ml-maintenance",
            )
            daily_ml_thread.start()

        # Scheduled publishing check (separate cadence, should not delay Telegram polling).
        if (now - last_publish_check_ts) >= max(1.0, PUBLISH_TICK_SECONDS):
            last_publish_check_ts = now
            try:
                out = publish_scheduled_due(limit=20)
                if out.get("processed", 0):
                    print("[worker] scheduled publish", out, flush=True)
            except Exception as exc:
                print(f"[worker] scheduled publish failed: {exc}", flush=True)

        try:
            bot_out = poll_review_updates(limit=50)
            if bot_out.get("processed", 0):
                print("[worker] telegram review updates", bot_out, flush=True)
        except Exception as exc:
            print(f"[worker] telegram review poll failed: {exc}", flush=True)

        # Poll Telegram frequently for fast inline кнопок реакцию.
        time.sleep(max(0.5, TELEGRAM_POLL_INTERVAL_SECONDS))


if __name__ == "__main__":
    main()
