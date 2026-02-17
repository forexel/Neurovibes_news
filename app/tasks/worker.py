from __future__ import annotations

import os
import time
from datetime import datetime

from app.db import init_db
from app.services.bootstrap import seed_sources
from app.services.pipeline import pick_hourly_top, run_hourly_cycle
from app.services.telegram_publisher import publish_article
from app.services.telegram_publisher import publish_scheduled_due
from app.services.telegram_review import poll_review_updates, send_hourly_top_for_review


INTERVAL_SECONDS = int(os.getenv("WORKER_INTERVAL_SECONDS", "3600"))
BACKFILL_DAYS = int(os.getenv("WORKER_BACKFILL_DAYS", "1"))
SCHEDULE_TICK_SECONDS = int(os.getenv("SCHEDULE_TICK_SECONDS", "30"))
AUTO_PUBLISH_TIMES_UTC = os.getenv("AUTO_PUBLISH_TIMES_UTC", "").strip()


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
        now = time.time()
        if now >= next_cycle_ts:
            try:
                print("[worker] cycle start", {"backfill_days": BACKFILL_DAYS}, flush=True)
                result = run_hourly_cycle(backfill_days=BACKFILL_DAYS)
                print("[worker] cycle done", result, flush=True)
                top_article_id = result.get("top_article_id")
                if top_article_id:
                    review_out = send_hourly_top_for_review(int(top_article_id))
                    print("[worker] telegram review send", review_out, flush=True)
            except Exception as exc:
                print(f"[worker] cycle failed: {exc}", flush=True)
            next_cycle_ts = now + INTERVAL_SECONDS

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
