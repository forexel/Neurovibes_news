from __future__ import annotations

import os
import time
from datetime import datetime

from app.db import init_db
from app.db import session_scope
from app.models import EditorFeedback
from app.models import UserWorkspace
from app.models import TelegramBotKV
from sqlalchemy import func, select
from app.services.bootstrap import seed_sources
from app.services.pipeline import pick_hourly_top, run_hourly_cycle
from app.services.telegram_publisher import publish_article
from app.services.telegram_publisher import publish_scheduled_due
from app.services.telegram_review import (
    poll_review_updates,
    send_hourly_top_for_review,
    send_review_status_once_per_hour,
    send_selected_backlog_for_review,
)
from app.services.telegram_context import load_workspace_telegram_context
from app.services.llm import get_workspace_api_key, set_user_api_key


INTERVAL_SECONDS = int(os.getenv("WORKER_INTERVAL_SECONDS", "3600"))
BACKFILL_DAYS = int(os.getenv("WORKER_BACKFILL_DAYS", "1"))
SCHEDULE_TICK_SECONDS = int(os.getenv("SCHEDULE_TICK_SECONDS", "30"))
AUTO_PUBLISH_TIMES_UTC = os.getenv("AUTO_PUBLISH_TIMES_UTC", "").strip()

_DEFAULT_USER_ID: int | None = None
_DEFAULT_USER_LOADED_AT: float = 0.0


def _set_worker_kv(key: str, value: str) -> None:
    # Worker is single-process per deploy, KV is just for UI visibility.
    try:
        with session_scope() as session:
            row = session.get(TelegramBotKV, key)  # reuse existing KV table
            if row:
                row.value = value
                row.updated_at = datetime.utcnow()
            else:
                session.add(TelegramBotKV(key=key, value=value, updated_at=datetime.utcnow()))
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


def _parse_publish_times(value: str) -> set[int]:
    out: set[int] = set()
    if not value:
        return out
    for chunk in value.split(","):
        s = chunk.strip()
        if not s or ":" not in s:
            continue
        hh, mm = s.split(":", 1)
        try:
            h = int(hh)
            m = int(mm)
        except ValueError:
            continue
        if 0 <= h <= 23 and 0 <= m <= 59:
            out.add(h * 60 + m)
    return out


def main() -> None:
    init_db()
    seed_sources()
    print("[worker] started", {"interval_seconds": INTERVAL_SECONDS, "backfill_days": BACKFILL_DAYS}, flush=True)
    next_cycle_ts = 0.0
    publish_minutes = _parse_publish_times(AUTO_PUBLISH_TIMES_UTC)
    published_slots: set[str] = set()
    while True:
        _load_default_user_context()
        now = time.time()
        if now >= next_cycle_ts:
            try:
                _set_worker_kv("worker_last_cycle_start_utc", datetime.utcnow().isoformat())
                print("[worker] cycle start", {"backfill_days": BACKFILL_DAYS}, flush=True)
                result = run_hourly_cycle(backfill_days=BACKFILL_DAYS)
                print("[worker] cycle done", result, flush=True)
                top_article_id = result.get("top_article_id")
                ingest = result.get("ingestion") or {}
                inserted_total = 0
                try:
                    inserted_total = int(sum(int(v or 0) for v in ingest.values()))
                except Exception:
                    inserted_total = 0

                # Early stage: we want to collect feedback. If you have very few feedback items,
                # allow re-sending the same candidate again (force resend).
                feedback_count = 0
                try:
                    with session_scope() as session:
                        feedback_count = int(session.execute(select(func.count(EditorFeedback.id))).scalar() or 0)
                except Exception:
                    feedback_count = 0
                force_resend = feedback_count < 10

                if top_article_id:
                    review_out = send_hourly_top_for_review(int(top_article_id), force=force_resend)
                    print("[worker] telegram review send", review_out, flush=True)
                    # If the same top was already sent earlier, still send a status once per hour.
                    if review_out.get("skipped") == "already_sent":
                        status_out = send_review_status_once_per_hour(
                            "top_unchanged",
                            "За последний час новый топ не появился: лучший кандидат не изменился.",
                        )
                        print("[worker] telegram review status", status_out, flush=True)
                    # Also deliver any missed auto-selected hourly items (backlog) one-by-one.
                    backlog_out = send_selected_backlog_for_review(limit=1)
                    print("[worker] telegram review backlog", backlog_out, flush=True)
                else:
                    # Always report to review chat once per hour if there is no selected hourly candidate.
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
                    backlog_out = send_selected_backlog_for_review(limit=1)
                    print("[worker] telegram review backlog", backlog_out, flush=True)
            except Exception as exc:
                print(f"[worker] cycle failed: {exc}", flush=True)
            next_cycle_ts = now + INTERVAL_SECONDS
            _set_worker_kv("worker_last_cycle_finish_utc", datetime.utcnow().isoformat())
            _set_worker_kv("worker_next_cycle_utc", datetime.utcfromtimestamp(next_cycle_ts).isoformat())

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

        # Optional fixed times auto-publish (UTC), e.g. "09:00,18:00".
        if publish_minutes:
            now_utc = datetime.utcnow()
            minute_of_day = now_utc.hour * 60 + now_utc.minute
            if minute_of_day in publish_minutes:
                slot_key = f"{now_utc.date().isoformat()}-{minute_of_day}"
                if slot_key not in published_slots:
                    try:
                        target = pick_hourly_top()
                        if target:
                            out = publish_article(int(target))
                            print("[worker] timed publish", {"slot": slot_key, "article_id": target, **out}, flush=True)
                        else:
                            print("[worker] timed publish skipped: no candidate", {"slot": slot_key}, flush=True)
                    except Exception as exc:
                        print(f"[worker] timed publish failed: {exc}", flush=True)
                    published_slots.add(slot_key)

            # Keep small in-memory history.
            if len(published_slots) > 64:
                published_slots = set(sorted(published_slots)[-32:])

        time.sleep(max(5, SCHEDULE_TICK_SECONDS))


if __name__ == "__main__":
    main()
