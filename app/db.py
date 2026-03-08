import logging
import re
import threading
from datetime import datetime
from contextlib import contextmanager
from time import perf_counter

from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.config import settings


logger = logging.getLogger(__name__)


engine = create_engine(
    settings.database_url,
    future=True,
    pool_pre_ping=True,
    connect_args={"options": "-csearch_path=public"} if settings.database_url.startswith("postgresql") else {},
)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)
Base = declarative_base()

_SQL_METRICS_LOCK = threading.Lock()
_SQL_SLOW_MS = 250.0
_SQL_TOP_LIMIT = 50
_SQL_METRICS = {
    "total_count": 0,
    "total_time_ms": 0.0,
    "slow_count": 0,
    "max_ms": 0.0,
    "started_at": datetime.utcnow().isoformat(),
    "top_slow": {},
}


def _normalize_sql(sql_text: str) -> str:
    txt = str(sql_text or "").strip()
    if not txt:
        return ""
    txt = re.sub(r"'[^']*'", "?", txt)
    txt = re.sub(r"\b\d+(?:\.\d+)?\b", "?", txt)
    txt = re.sub(r"\s+", " ", txt).strip()
    return txt[:1000]


def _record_sql_metric(statement: str, elapsed_ms: float) -> None:
    norm = _normalize_sql(statement)
    if not norm:
        return
    low = norm.lower()
    if low in {"begin", "commit", "rollback"}:
        return

    with _SQL_METRICS_LOCK:
        _SQL_METRICS["total_count"] += 1
        _SQL_METRICS["total_time_ms"] += float(elapsed_ms)
        _SQL_METRICS["max_ms"] = max(float(_SQL_METRICS["max_ms"]), float(elapsed_ms))
        if float(elapsed_ms) >= _SQL_SLOW_MS:
            _SQL_METRICS["slow_count"] += 1
            bucket = _SQL_METRICS["top_slow"].get(norm)
            if bucket is None:
                bucket = {"sql": norm, "count": 0, "total_ms": 0.0, "max_ms": 0.0, "last_ms": 0.0}
                _SQL_METRICS["top_slow"][norm] = bucket
            bucket["count"] += 1
            bucket["total_ms"] += float(elapsed_ms)
            bucket["max_ms"] = max(float(bucket["max_ms"]), float(elapsed_ms))
            bucket["last_ms"] = float(elapsed_ms)

            if len(_SQL_METRICS["top_slow"]) > _SQL_TOP_LIMIT:
                # Keep heaviest buckets only.
                ordered = sorted(
                    _SQL_METRICS["top_slow"].items(),
                    key=lambda kv: (float(kv[1]["max_ms"]), float(kv[1]["total_ms"])),
                    reverse=True,
                )
                _SQL_METRICS["top_slow"] = dict(ordered[:_SQL_TOP_LIMIT])


def get_sql_metrics_snapshot(top_n: int = 10) -> dict:
    with _SQL_METRICS_LOCK:
        total_count = int(_SQL_METRICS["total_count"])
        total_time_ms = float(_SQL_METRICS["total_time_ms"])
        slow_count = int(_SQL_METRICS["slow_count"])
        max_ms = float(_SQL_METRICS["max_ms"])
        started_at = str(_SQL_METRICS["started_at"])
        top = list(_SQL_METRICS["top_slow"].values())

    top = sorted(top, key=lambda x: (float(x.get("max_ms") or 0.0), float(x.get("total_ms") or 0.0)), reverse=True)
    out_top = []
    for row in top[: max(1, int(top_n or 1))]:
        cnt = max(1, int(row.get("count") or 1))
        out_top.append(
            {
                "sql": str(row.get("sql") or ""),
                "count": cnt,
                "avg_ms": round(float(row.get("total_ms") or 0.0) / cnt, 2),
                "max_ms": round(float(row.get("max_ms") or 0.0), 2),
                "last_ms": round(float(row.get("last_ms") or 0.0), 2),
            }
        )

    return {
        "started_at": started_at,
        "threshold_ms": _SQL_SLOW_MS,
        "total_count": total_count,
        "total_time_ms": round(total_time_ms, 2),
        "avg_ms": round((total_time_ms / total_count), 2) if total_count else 0.0,
        "slow_count": slow_count,
        "max_ms": round(max_ms, 2),
        "top_slow": out_top,
    }


@event.listens_for(engine, "connect")
def _set_postgres_search_path(dbapi_connection, connection_record):
    try:
        cursor = dbapi_connection.cursor()
        cursor.execute("SET search_path TO public")
        cursor.close()
        dbapi_connection.commit()
    except Exception:
        # Non-Postgres or restricted connections can continue with defaults.
        pass


@event.listens_for(engine, "before_cursor_execute")
def _before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    conn.info.setdefault("_query_start_time", []).append(perf_counter())


@event.listens_for(engine, "after_cursor_execute")
def _after_cursor_execute(conn, cursor, statement, parameters, context, executemany):
    stack = conn.info.get("_query_start_time") or []
    if not stack:
        return
    started = stack.pop()
    elapsed_ms = (perf_counter() - started) * 1000.0
    try:
        _record_sql_metric(str(statement or ""), float(elapsed_ms))
    except Exception:
        pass


@contextmanager
def session_scope():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def init_db() -> None:
    if not settings.db_auto_patch_schema:
        logger.info("DB_AUTO_PATCH_SCHEMA disabled: skipping startup schema patching.")
        return

    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE IF EXISTS sources ADD COLUMN IF NOT EXISTS kind VARCHAR(20) NOT NULL DEFAULT 'rss'"))
        conn.execute(text("ALTER TABLE IF EXISTS sources ADD COLUMN IF NOT EXISTS is_deleted BOOLEAN NOT NULL DEFAULT FALSE"))
        conn.execute(text("ALTER TABLE IF EXISTS articles ADD COLUMN IF NOT EXISTS scheduled_publish_at TIMESTAMP NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS articles ADD COLUMN IF NOT EXISTS selected_hour_bucket_utc TIMESTAMP NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS articles ADD COLUMN IF NOT EXISTS archived_kind VARCHAR(32) NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS articles ADD COLUMN IF NOT EXISTS archived_reason TEXT NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS articles ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS articles ADD COLUMN IF NOT EXISTS content_type VARCHAR(32) NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS articles ADD COLUMN IF NOT EXISTS practical_value INTEGER NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS articles ADD COLUMN IF NOT EXISTS audience_fit INTEGER NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS articles ADD COLUMN IF NOT EXISTS ml_prob DOUBLE PRECISION NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS articles ADD COLUMN IF NOT EXISTS ml_recommendation VARCHAR(32) NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS articles ADD COLUMN IF NOT EXISTS ml_recommendation_confidence DOUBLE PRECISION NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS articles ADD COLUMN IF NOT EXISTS ml_recommendation_reason TEXT NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS articles ADD COLUMN IF NOT EXISTS ml_model_version VARCHAR(64) NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS articles ADD COLUMN IF NOT EXISTS ml_recommendation_at TIMESTAMP NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS articles ADD COLUMN IF NOT EXISTS ml_verdict_confirmed BOOLEAN NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS articles ADD COLUMN IF NOT EXISTS ml_verdict_comment TEXT NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS articles ADD COLUMN IF NOT EXISTS ml_verdict_tags JSON NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS articles ADD COLUMN IF NOT EXISTS ml_verdict_updated_at TIMESTAMP NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS selection_decisions ADD COLUMN IF NOT EXISTS selector_kind VARCHAR(32) NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS user_workspaces ADD COLUMN IF NOT EXISTS openrouter_api_key_enc TEXT NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS user_workspaces ADD COLUMN IF NOT EXISTS telegram_bot_token_enc TEXT NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS user_workspaces ADD COLUMN IF NOT EXISTS telegram_review_chat_id VARCHAR(255) NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS user_workspaces ADD COLUMN IF NOT EXISTS telegram_channel_id VARCHAR(255) NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS user_workspaces ADD COLUMN IF NOT EXISTS telegram_signature VARCHAR(255) NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS user_workspaces ADD COLUMN IF NOT EXISTS timezone_name VARCHAR(64) NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS user_workspaces ADD COLUMN IF NOT EXISTS audience_tags JSON NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS training_events ADD COLUMN IF NOT EXISTS reason_positive_tags JSON NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS training_events ADD COLUMN IF NOT EXISTS reason_negative_tags JSON NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS training_events ADD COLUMN IF NOT EXISTS reason_sentiment VARCHAR(16) NULL"))
        articles_table_exists = bool(
            conn.execute(
                text(
                    "SELECT EXISTS ("
                    "  SELECT 1 FROM information_schema.tables "
                    "  WHERE table_schema='public' AND table_name='articles'"
                    ")"
                )
            ).scalar()
        )
        if articles_table_exists:
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_articles_created_at ON public.articles (created_at DESC)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_articles_status_published_at ON public.articles (status, published_at DESC)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_articles_source_created_at ON public.articles (source_id, created_at DESC)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_articles_ml_confidence ON public.articles (ml_recommendation_confidence DESC)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_articles_scheduled_publish_at ON public.articles (scheduled_publish_at)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_articles_selected_hour_bucket ON public.articles (selected_hour_bucket_utc)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_scores_final_score ON public.scores (final_score DESC)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_daily_selections_active_date_article ON public.daily_selections (active, selected_date, article_id)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_publish_jobs_article_created ON public.publish_jobs (article_id, created_at DESC)"))
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS public.article_previews ("
                    "id INTEGER PRIMARY KEY REFERENCES public.articles(id) ON DELETE CASCADE,"
                    "status VARCHAR(32) NOT NULL,"
                    "content_mode VARCHAR(20) NOT NULL DEFAULT 'summary_only',"
                    "double_of_article_id INTEGER NULL,"
                    "title TEXT NOT NULL,"
                    "subtitle TEXT NOT NULL DEFAULT '',"
                    "ru_title TEXT NULL,"
                    "ru_summary TEXT NULL,"
                    "short_hook TEXT NULL,"
                    "source_id INTEGER NOT NULL REFERENCES public.sources(id),"
                    "published_at TIMESTAMP NULL,"
                    "created_at TIMESTAMP NOT NULL,"
                    "canonical_url VARCHAR(2048) NOT NULL,"
                    "generated_image_path VARCHAR(1024) NULL,"
                    "scheduled_publish_at TIMESTAMP NULL,"
                    "ml_recommendation VARCHAR(32) NULL,"
                    "ml_recommendation_confidence DOUBLE PRECISION NULL,"
                    "ml_recommendation_reason TEXT NULL,"
                    "ml_model_version VARCHAR(64) NULL,"
                    "ml_recommendation_at TIMESTAMP NULL,"
                    "archived_kind VARCHAR(32) NULL,"
                    "archived_reason TEXT NULL,"
                    "archived_at TIMESTAMP NULL,"
                    "ml_verdict_confirmed BOOLEAN NULL,"
                    "ml_verdict_comment TEXT NULL,"
                    "ml_verdict_tags JSON NULL,"
                    "ml_verdict_updated_at TIMESTAMP NULL"
                    ")"
                )
            )
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_article_previews_created_at ON public.article_previews (created_at DESC)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_article_previews_status_created ON public.article_previews (status, created_at DESC)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_article_previews_source_created ON public.article_previews (source_id, created_at DESC)"))
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_article_previews_ml_conf ON public.article_previews (ml_recommendation_confidence DESC)"))
            conn.execute(text("ALTER TABLE IF EXISTS public.article_previews ADD COLUMN IF NOT EXISTS double_of_article_id INTEGER NULL"))
            conn.execute(
                text(
                    "CREATE OR REPLACE FUNCTION public.sync_article_preview() "
                    "RETURNS TRIGGER AS $$ "
                    "BEGIN "
                    "  IF TG_OP = 'DELETE' THEN "
                    "    DELETE FROM public.article_previews WHERE id = OLD.id; "
                    "    RETURN OLD; "
                    "  END IF; "
                    "  INSERT INTO public.article_previews ("
                    "    id, status, content_mode, double_of_article_id, title, subtitle, ru_title, ru_summary, short_hook, source_id, "
                    "    published_at, created_at, canonical_url, generated_image_path, scheduled_publish_at, "
                    "    ml_recommendation, ml_recommendation_confidence, ml_recommendation_reason, ml_model_version, ml_recommendation_at, "
                    "    archived_kind, archived_reason, archived_at, ml_verdict_confirmed, ml_verdict_comment, ml_verdict_tags, ml_verdict_updated_at"
                    "  ) VALUES ("
                    "    NEW.id, NEW.status::text, NEW.content_mode, NEW.double_of_article_id, NEW.title, NEW.subtitle, NEW.ru_title, NEW.ru_summary, NEW.short_hook, NEW.source_id, "
                    "    NEW.published_at, NEW.created_at, NEW.canonical_url, NEW.generated_image_path, NEW.scheduled_publish_at, "
                    "    NEW.ml_recommendation, NEW.ml_recommendation_confidence, NEW.ml_recommendation_reason, NEW.ml_model_version, NEW.ml_recommendation_at, "
                    "    NEW.archived_kind, NEW.archived_reason, NEW.archived_at, NEW.ml_verdict_confirmed, NEW.ml_verdict_comment, NEW.ml_verdict_tags, NEW.ml_verdict_updated_at"
                    "  ) "
                    "  ON CONFLICT (id) DO UPDATE SET "
                    "    status = EXCLUDED.status, "
                    "    content_mode = EXCLUDED.content_mode, "
                    "    double_of_article_id = EXCLUDED.double_of_article_id, "
                    "    title = EXCLUDED.title, "
                    "    subtitle = EXCLUDED.subtitle, "
                    "    ru_title = EXCLUDED.ru_title, "
                    "    ru_summary = EXCLUDED.ru_summary, "
                    "    short_hook = EXCLUDED.short_hook, "
                    "    source_id = EXCLUDED.source_id, "
                    "    published_at = EXCLUDED.published_at, "
                    "    created_at = EXCLUDED.created_at, "
                    "    canonical_url = EXCLUDED.canonical_url, "
                    "    generated_image_path = EXCLUDED.generated_image_path, "
                    "    scheduled_publish_at = EXCLUDED.scheduled_publish_at, "
                    "    ml_recommendation = EXCLUDED.ml_recommendation, "
                    "    ml_recommendation_confidence = EXCLUDED.ml_recommendation_confidence, "
                    "    ml_recommendation_reason = EXCLUDED.ml_recommendation_reason, "
                    "    ml_model_version = EXCLUDED.ml_model_version, "
                    "    ml_recommendation_at = EXCLUDED.ml_recommendation_at, "
                    "    archived_kind = EXCLUDED.archived_kind, "
                    "    archived_reason = EXCLUDED.archived_reason, "
                    "    archived_at = EXCLUDED.archived_at, "
                    "    ml_verdict_confirmed = EXCLUDED.ml_verdict_confirmed, "
                    "    ml_verdict_comment = EXCLUDED.ml_verdict_comment, "
                    "    ml_verdict_tags = EXCLUDED.ml_verdict_tags, "
                    "    ml_verdict_updated_at = EXCLUDED.ml_verdict_updated_at; "
                    "  RETURN NEW; "
                    "END; "
                    "$$ LANGUAGE plpgsql"
                )
            )
            conn.execute(text("DROP TRIGGER IF EXISTS trg_articles_sync_preview ON public.articles"))
            conn.execute(
                text(
                    "CREATE TRIGGER trg_articles_sync_preview "
                    "AFTER INSERT OR UPDATE OR DELETE ON public.articles "
                    "FOR EACH ROW EXECUTE FUNCTION public.sync_article_preview()"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO public.article_previews ("
                    "  id, status, content_mode, double_of_article_id, title, subtitle, ru_title, ru_summary, short_hook, source_id, "
                    "  published_at, created_at, canonical_url, generated_image_path, scheduled_publish_at, "
                    "  ml_recommendation, ml_recommendation_confidence, ml_recommendation_reason, ml_model_version, ml_recommendation_at, "
                    "  archived_kind, archived_reason, archived_at, ml_verdict_confirmed, ml_verdict_comment, ml_verdict_tags, ml_verdict_updated_at"
                    ") "
                    "SELECT "
                    "  a.id, a.status::text, a.content_mode, a.double_of_article_id, a.title, a.subtitle, a.ru_title, a.ru_summary, a.short_hook, a.source_id, "
                    "  a.published_at, a.created_at, a.canonical_url, a.generated_image_path, a.scheduled_publish_at, "
                    "  a.ml_recommendation, a.ml_recommendation_confidence, a.ml_recommendation_reason, a.ml_model_version, a.ml_recommendation_at, "
                    "  a.archived_kind, a.archived_reason, a.archived_at, a.ml_verdict_confirmed, a.ml_verdict_comment, a.ml_verdict_tags, a.ml_verdict_updated_at "
                    "FROM public.articles a "
                    "ON CONFLICT (id) DO UPDATE SET "
                    "  status = EXCLUDED.status, "
                    "  content_mode = EXCLUDED.content_mode, "
                    "  double_of_article_id = EXCLUDED.double_of_article_id, "
                    "  title = EXCLUDED.title, "
                    "  subtitle = EXCLUDED.subtitle, "
                    "  ru_title = EXCLUDED.ru_title, "
                    "  ru_summary = EXCLUDED.ru_summary, "
                    "  short_hook = EXCLUDED.short_hook, "
                    "  source_id = EXCLUDED.source_id, "
                    "  published_at = EXCLUDED.published_at, "
                    "  created_at = EXCLUDED.created_at, "
                    "  canonical_url = EXCLUDED.canonical_url, "
                    "  generated_image_path = EXCLUDED.generated_image_path, "
                    "  scheduled_publish_at = EXCLUDED.scheduled_publish_at, "
                    "  ml_recommendation = EXCLUDED.ml_recommendation, "
                    "  ml_recommendation_confidence = EXCLUDED.ml_recommendation_confidence, "
                    "  ml_recommendation_reason = EXCLUDED.ml_recommendation_reason, "
                    "  ml_model_version = EXCLUDED.ml_model_version, "
                    "  ml_recommendation_at = EXCLUDED.ml_recommendation_at, "
                    "  archived_kind = EXCLUDED.archived_kind, "
                    "  archived_reason = EXCLUDED.archived_reason, "
                    "  archived_at = EXCLUDED.archived_at, "
                    "  ml_verdict_confirmed = EXCLUDED.ml_verdict_confirmed, "
                    "  ml_verdict_comment = EXCLUDED.ml_verdict_comment, "
                    "  ml_verdict_tags = EXCLUDED.ml_verdict_tags, "
                    "  ml_verdict_updated_at = EXCLUDED.ml_verdict_updated_at"
                )
            )
            conn.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS article_enrichment ("
                    "id SERIAL PRIMARY KEY,"
                    "article_id INTEGER NOT NULL UNIQUE REFERENCES articles(id),"
                    "content_type VARCHAR(32) NOT NULL DEFAULT 'other',"
                    "practical_value INTEGER NOT NULL DEFAULT 0,"
                    "audience_fit INTEGER NOT NULL DEFAULT 0,"
                    "actionability INTEGER NOT NULL DEFAULT 0,"
                    "use_cases JSON NULL,"
                    "tool_detected BOOLEAN NOT NULL DEFAULT FALSE,"
                    "tool_name TEXT NULL,"
                    "tool_is_free_tier BOOLEAN NULL,"
                    "requires_code BOOLEAN NULL,"
                    "setup_time_minutes INTEGER NULL,"
                    "risk_flags JSON NULL,"
                    "why_short TEXT NULL,"
                    "enrichment_json JSON NULL,"
                    "enriched_at TIMESTAMP NOT NULL DEFAULT NOW()"
                    ")"
                )
            )
            conn.execute(text("CREATE INDEX IF NOT EXISTS ix_article_enrichment_content_type ON article_enrichment (content_type)"))

        # Only backfill content_mode when the column is added for the first time.
        col_exists = bool(
            conn.execute(
                text(
                    "SELECT EXISTS ("
                    "  SELECT 1 FROM information_schema.columns "
                    "  WHERE table_schema='public' AND table_name='articles' AND column_name='content_mode'"
                    ")"
                )
            ).scalar()
        )
        if articles_table_exists and not col_exists:
            conn.execute(
                text(
                    "ALTER TABLE IF EXISTS public.articles "
                    "ADD COLUMN IF NOT EXISTS content_mode VARCHAR(20) NOT NULL DEFAULT 'summary_only'"
                )
            )
            conn.execute(
                text(
                    "UPDATE public.articles "
                    "SET content_mode = CASE "
                    "  WHEN COALESCE(length(text), 0) >= 800 "
                    "   AND COALESCE(length(text), 0) >= COALESCE(length(subtitle), 0) + 400 "
                    "    THEN 'full' "
                    "  ELSE 'summary_only' "
                    "END"
                )
            )

    # Import inside function to avoid circular import at module import time.
    from app import models  # noqa: F401

    if settings.db_auto_create:
        try:
            Base.metadata.create_all(bind=engine)
        except SQLAlchemyError as exc:
            logger.warning("metadata create_all skipped due to database capabilities/schema mismatch: %s", exc)
