import logging
from contextlib import contextmanager

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

    try:
        Base.metadata.create_all(bind=engine)
    except SQLAlchemyError as exc:
        logger.warning("metadata create_all skipped due to database capabilities/schema mismatch: %s", exc)
