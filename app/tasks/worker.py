from __future__ import annotations

import os
import threading
import time
from datetime import datetime, timezone

from app.db import init_db
from app.db import session_scope
from app.models import EditorFeedback
from app.models import UserWorkspace
from app.models import TelegramBotKV
from sqlalchemy import func, select
from app.services.bootstrap import seed_sources
from app.services.pipeline import pick_hourly_top, run_hourly_cycle
from app.services.telegram_publisher import publish_scheduled_due
from app.services.telegram_review import (
    poll_review_updates,
    send_hourly_top_for_review,
    send_review_status_once_per_hour,
)
from app.services.telegram_context import load_workspace_telegram_context
from app.services.llm import get_workspace_api_key, set_user_api_key


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

        if top_article_id:
            # Auto-mode: never spam the same hour slot. Force resend is only for manual backfill endpoints.
            review_out = send_hourly_top_for_review(int(top_article_id), force=False)
            print("[worker] telegram review send", review_out, flush=True)
            # If the top did not change (article already sent previously), still send 1 status per hour window.
            if review_out.get("skipped") == "already_sent":
                status_out = send_review_status_once_per_hour(
                    "top_unchanged",
                    "За последний час новый топ не появился: лучший кандидат не изменился.",
                )
                print("[worker] telegram review status", status_out, flush=True)
            # If already sent for this slot, do nothing (we already produced 1 message for the window).
        else:
            if inserted_total <= 0:
                status_out = send_review_status_once_per_hour(
                    "no_new_articles",
                    "За последний час новых статей нет (по источникам пришло 0 новых ссылок).",
                )
            else:
                status_out = send_review_status_once_per_hour(
                    "all_filtered",
                    f"За последний час новые статьи были (+{inserted_total}), но все не прошли фильтры/скоринг.",
                )
            print("[worker] telegram review status", status_out, flush=True)
    except Exception as exc:
        print(f"[worker] cycle failed: {exc}", flush=True)
        _set_worker_kv("worker_last_cycle_error", str(exc)[:800])
    finally:
        _set_worker_kv("worker_last_cycle_finish_utc", datetime.now(timezone.utc).isoformat())
        _set_worker_kv("worker_cycle_state", "idle")


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
