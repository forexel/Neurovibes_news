from __future__ import annotations

import argparse
import time
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select

from app.db import init_db, session_scope
from app.models import Article, ArticleStatus, TelegramBotKV
from app.services.auto_decision import decide_and_maybe_publish
from app.services.bootstrap import seed_sources
from app.services.content_generation import generate_image_card, generate_ru_summary
from app.services.embedding_dedup import process_embeddings_and_dedup
from app.services.ingestion import run_backfill_batched, run_ingestion
from app.services.pipeline import pick_hourly_top, run_hourly_cycle
from app.services.preference import (
    backfill_training_and_restore_unreasoned_archived,
    build_editor_choice_dataset,
    build_ranking_dataset,
    detect_preference_drift,
    infer_audience_tags_for_workspaces,
    reclassify_training_reasons_llm,
    reretag_training_event_reasons,
    rebuild_preference_profile,
    train_editor_choice_model,
    train_ranking_model,
)
from app.services.scoring import run_scoring
from app.services.telegram_context import load_workspace_telegram_context, telegram_bot_token, telegram_review_chat_id
from app.services.telegram_publisher import publish_article, send_test_message


def latest_article_by_status(status: ArticleStatus) -> int | None:
    with session_scope() as session:
        article = session.scalars(
            select(Article).where(Article.status == status).order_by(Article.updated_at.desc()).limit(1)
        ).first()
    return article.id if article else None


def cmd_ingest(days_back: int) -> None:
    print(run_ingestion(days_back=days_back))


def cmd_backfill(days: int, batch_days: int) -> None:
    print(run_backfill_batched(total_days=days, batch_days=batch_days))


def cmd_dedup(limit: int) -> None:
    print({"embedded": process_embeddings_and_dedup(limit=limit)})


def cmd_score(limit: int) -> None:
    print({"scored": run_scoring(limit=limit)})


def cmd_pick() -> None:
    print({"top_article_id": pick_hourly_top()})


def cmd_prepare(article_id: int | None) -> None:
    target = article_id or latest_article_by_status(ArticleStatus.SELECTED_HOURLY)
    if not target:
        print({"ok": False, "error": "no_selected_article"})
        return
    ok = generate_ru_summary(target)
    image = generate_image_card(target)
    print({"ok": ok, "article_id": target, "image": image})


def cmd_publish(article_id: int | None) -> None:
    target = article_id or latest_article_by_status(ArticleStatus.READY) or latest_article_by_status(ArticleStatus.SELECTED_HOURLY)
    if not target:
        print({"ok": False, "error": "no_article_to_publish"})
        return
    print(publish_article(target))


def cmd_cycle(backfill_days: int, auto_publish: bool) -> None:
    result = run_hourly_cycle(backfill_days=backfill_days)
    print(result)
    if auto_publish and result.get("top_article_id"):
        print(publish_article(int(result["top_article_id"])))


def cmd_auto_decision() -> None:
    print(decide_and_maybe_publish(top_n=5))


def cmd_rebuild_profile() -> None:
    print(rebuild_preference_profile(min_feedback=20))


def cmd_telegram_test() -> None:
    print(send_test_message())


def cmd_trainer(days: int) -> None:
    ds = build_ranking_dataset(days=days)
    print(ds)
    if ds.get("ok") and ds.get("batch_id"):
        print(train_ranking_model(ds["batch_id"]))


def cmd_editor_choice_trainer(days: int) -> None:
    print(build_editor_choice_dataset(days_back=days))
    print(train_editor_choice_model(days_back=days))


def cmd_recover_manual_week() -> None:
    print(backfill_training_and_restore_unreasoned_archived())


def cmd_rereview_reasons(limit: int, overwrite: bool) -> None:
    print(reretag_training_event_reasons(limit=limit, overwrite=overwrite))


def cmd_reclassify_reasons_llm(limit: int, only_null: bool, allow_new_tags: bool) -> None:
    print(reclassify_training_reasons_llm(limit=limit, only_null=only_null, allow_new_tags=allow_new_tags))


def cmd_infer_audience_tags(limit: int, overwrite: bool) -> None:
    print(infer_audience_tags_for_workspaces(limit=limit, overwrite=overwrite))


def cmd_drift() -> None:
    print(detect_preference_drift())


def _kv_get(session, key: str, default: str = "") -> str:
    row = session.get(TelegramBotKV, key)
    return str(row.value if row else default)


def _kv_set(session, key: str, value: str) -> None:
    row = session.get(TelegramBotKV, key)
    if row is None:
        session.add(TelegramBotKV(key=key, value=str(value), updated_at=datetime.utcnow()))
        return
    row.value = str(value)
    row.updated_at = datetime.utcnow()


def _parse_iso_utc_maybe(v: str | None) -> datetime | None:
    s = (v or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def _send_watchdog_alert(text: str) -> dict:
    load_workspace_telegram_context(None)
    token = (telegram_bot_token() or "").strip()
    chat_id = (telegram_review_chat_id() or "").strip()
    if not token or not chat_id:
        return {"ok": False, "error": "telegram_not_configured"}
    try:
        with httpx.Client(timeout=15) as client:
            r = client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": text[:3500]},
            )
        return {"ok": bool(r.status_code == 200), "status_code": r.status_code, "text": (r.text or "")[:400]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def cmd_watchdog_check(
    *,
    max_running_seconds: int,
    stale_next_cycle_seconds: int,
    notify: bool,
    dedupe_minutes: int,
) -> int:
    now = datetime.now(timezone.utc)
    with session_scope() as session:
        state = _kv_get(session, "worker_cycle_state", "")
        last_start = _parse_iso_utc_maybe(_kv_get(session, "worker_last_cycle_start_utc", ""))
        last_finish = _parse_iso_utc_maybe(_kv_get(session, "worker_last_cycle_finish_utc", ""))
        last_error = _kv_get(session, "worker_last_cycle_error", "")
        next_cycle = _parse_iso_utc_maybe(_kv_get(session, "worker_next_cycle_utc", ""))
        last_slot = _kv_get(session, "worker_last_cycle_slot", "")
        tg_offset = _kv_get(session, "telegram_review_offset", "")
        last_article_slot = _kv_get(session, "telegram_review_last_article_slot", "")

        problems: list[str] = []
        if state.strip().lower() == "running" and last_start:
            run_age = (now - last_start).total_seconds()
            if run_age > max_running_seconds:
                problems.append(f"worker stuck running for {int(run_age)}s since {last_start.isoformat()}")
        if next_cycle:
            lag = (now - next_cycle).total_seconds()
            if lag > stale_next_cycle_seconds:
                problems.append(f"next cycle overdue by {int(lag)}s (next={next_cycle.isoformat()})")
        if last_error.strip():
            problems.append(f"last cycle error: {last_error.strip()}")

        healthy = len(problems) == 0
        result = {
            "ok": True,
            "healthy": healthy,
            "now_utc": now.isoformat(),
            "worker_cycle_state": state,
            "worker_last_cycle_slot": last_slot,
            "worker_last_cycle_start_utc": last_start.isoformat() if last_start else None,
            "worker_last_cycle_finish_utc": last_finish.isoformat() if last_finish else None,
            "worker_next_cycle_utc": next_cycle.isoformat() if next_cycle else None,
            "worker_last_cycle_error": last_error or None,
            "telegram_review_offset": tg_offset or None,
            "telegram_review_last_article_slot": last_article_slot or None,
            "problems": problems,
        }
        print(result)

        if healthy or not notify:
            if healthy:
                # Clear alert dedupe window on recovery.
                _kv_set(session, "watchdog_pipeline_last_alert_sig", "")
                _kv_set(session, "watchdog_pipeline_last_alert_at", "")
            return 0 if healthy else 2

        alert_sig = "|".join(problems)[:500]
        prev_sig = _kv_get(session, "watchdog_pipeline_last_alert_sig", "")
        prev_at = _parse_iso_utc_maybe(_kv_get(session, "watchdog_pipeline_last_alert_at", ""))
        should_send = True
        if prev_sig == alert_sig and prev_at:
            if (now - prev_at) < timedelta(minutes=max(1, dedupe_minutes)):
                should_send = False

        if should_send:
            text = (
                "ALERT: pipeline watchdog detected issue\n"
                f"- state: {state or '-'}\n"
                f"- last_slot: {last_slot or '-'}\n"
                f"- last_start_utc: {last_start.isoformat() if last_start else '-'}\n"
                f"- last_finish_utc: {last_finish.isoformat() if last_finish else '-'}\n"
                f"- next_cycle_utc: {next_cycle.isoformat() if next_cycle else '-'}\n"
                f"- tg_last_article_slot: {last_article_slot or '-'}\n"
                f"- tg_offset: {tg_offset or '-'}\n"
                f"- problems:\n  - " + "\n  - ".join(problems)
            )
            send_res = _send_watchdog_alert(text)
            _kv_set(session, "watchdog_pipeline_last_alert_sig", alert_sig)
            _kv_set(session, "watchdog_pipeline_last_alert_at", now.isoformat())
            _kv_set(session, "watchdog_pipeline_last_alert_result", str(send_res))
        return 2


def cmd_loop(backfill_days: int, interval_seconds: int, auto_publish: bool) -> None:
    while True:
        try:
            cmd_cycle(backfill_days=backfill_days, auto_publish=auto_publish)
        except Exception as exc:
            print({"ok": False, "error": str(exc)})
        time.sleep(interval_seconds)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Neurovibes full processing script")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="read RSS feeds and save articles")
    ingest.add_argument("--days-back", type=int, default=30)

    backfill = sub.add_parser("backfill", help="batched historical backfill")
    backfill.add_argument("--days", type=int, default=30)
    backfill.add_argument("--batch-days", type=int, default=3)

    dedup = sub.add_parser("dedup", help="compute embeddings and mark doubles")
    dedup.add_argument("--limit", type=int, default=300)

    score = sub.add_parser("score", help="score new articles")
    score.add_argument("--limit", type=int, default=300)

    sub.add_parser("pick", help="pick top article for last hour")

    prepare = sub.add_parser("prepare", help="build RU summary + image")
    prepare.add_argument("--article-id", type=int)

    publish = sub.add_parser("publish", help="publish to telegram")
    publish.add_argument("--article-id", type=int)

    cycle = sub.add_parser("cycle", help="full cycle: ingest+dedup+score+pick+prepare")
    cycle.add_argument("--backfill-days", type=int, default=1)
    cycle.add_argument("--auto-publish", action="store_true")

    loop = sub.add_parser("loop", help="run full cycle repeatedly")
    loop.add_argument("--backfill-days", type=int, default=1)
    loop.add_argument("--interval-seconds", type=int, default=3600)
    loop.add_argument("--auto-publish", action="store_true")

    trainer = sub.add_parser("trainer", help="build dataset and train ranking model")
    trainer.add_argument("--days", type=int, default=14)

    editor_trainer = sub.add_parser("editor-choice-train", help="train editor choice model from training_events")
    editor_trainer.add_argument("--days", type=int, default=30)
    sub.add_parser("recover-manual-week", help="backfill delete/hide reasons to training_events and restore archived without reasons")
    rr = sub.add_parser("rereview-reasons", help="re-tag reason_text into reason_tags for training_events")
    rr.add_argument("--limit", type=int, default=50000)
    rr.add_argument("--overwrite", action="store_true")
    rrllm = sub.add_parser("reclassify-reasons-llm", help="LLM multi-tag classification for reason_text")
    rrllm.add_argument("--limit", type=int, default=300)
    rrllm.add_argument("--all", action="store_true", help="process all rows, not only reason_tags IS NULL")
    rrllm.add_argument("--no-new-tags", action="store_true", help="do not create new tags suggested by LLM")
    at = sub.add_parser("infer-audience-tags", help="derive audience tags from workspace audience_description")
    at.add_argument("--limit", type=int, default=100)
    at.add_argument("--overwrite", action="store_true")

    sub.add_parser("drift", help="detect preference drift")
    sub.add_parser("auto-decision", help="decide publish candidate using trained model")
    sub.add_parser("rebuild-profile", help="rebuild preference profile from feedback")
    sub.add_parser("telegram-test", help="send test message to telegram channel")
    wd = sub.add_parser("watchdog-check", help="health-check worker state and optionally notify Telegram")
    wd.add_argument("--max-running-seconds", type=int, default=1800)
    wd.add_argument("--stale-next-cycle-seconds", type=int, default=900)
    wd.add_argument("--notify", action="store_true")
    wd.add_argument("--dedupe-minutes", type=int, default=30)

    return parser


def main() -> None:
    init_db()
    seed_sources()

    parser = build_parser()
    args = parser.parse_args()

    if args.command == "ingest":
        cmd_ingest(days_back=args.days_back)
    elif args.command == "backfill":
        cmd_backfill(days=args.days, batch_days=args.batch_days)
    elif args.command == "dedup":
        cmd_dedup(limit=args.limit)
    elif args.command == "score":
        cmd_score(limit=args.limit)
    elif args.command == "pick":
        cmd_pick()
    elif args.command == "prepare":
        cmd_prepare(article_id=args.article_id)
    elif args.command == "publish":
        cmd_publish(article_id=args.article_id)
    elif args.command == "cycle":
        cmd_cycle(backfill_days=args.backfill_days, auto_publish=args.auto_publish)
    elif args.command == "loop":
        cmd_loop(backfill_days=args.backfill_days, interval_seconds=args.interval_seconds, auto_publish=args.auto_publish)
    elif args.command == "trainer":
        cmd_trainer(days=args.days)
    elif args.command == "editor-choice-train":
        cmd_editor_choice_trainer(days=args.days)
    elif args.command == "recover-manual-week":
        cmd_recover_manual_week()
    elif args.command == "rereview-reasons":
        cmd_rereview_reasons(limit=args.limit, overwrite=args.overwrite)
    elif args.command == "reclassify-reasons-llm":
        cmd_reclassify_reasons_llm(limit=args.limit, only_null=not args.all, allow_new_tags=not args.no_new_tags)
    elif args.command == "infer-audience-tags":
        cmd_infer_audience_tags(limit=args.limit, overwrite=args.overwrite)
    elif args.command == "drift":
        cmd_drift()
    elif args.command == "auto-decision":
        cmd_auto_decision()
    elif args.command == "rebuild-profile":
        cmd_rebuild_profile()
    elif args.command == "telegram-test":
        cmd_telegram_test()
    elif args.command == "watchdog-check":
        raise SystemExit(
            cmd_watchdog_check(
                max_running_seconds=args.max_running_seconds,
                stale_next_cycle_seconds=args.stale_next_cycle_seconds,
                notify=args.notify,
                dedupe_minutes=args.dedupe_minutes,
            )
        )


if __name__ == "__main__":
    main()
