from contextlib import contextmanager

from sqlalchemy import create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.config import settings


engine = create_engine(settings.database_url, future=True, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False, future=True)
Base = declarative_base()


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
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.execute(text("ALTER TABLE IF EXISTS sources ADD COLUMN IF NOT EXISTS kind VARCHAR(20) NOT NULL DEFAULT 'rss'"))
        conn.execute(text("ALTER TABLE IF EXISTS articles ADD COLUMN IF NOT EXISTS scheduled_publish_at TIMESTAMP NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS articles ADD COLUMN IF NOT EXISTS selected_hour_bucket_utc TIMESTAMP NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS articles ADD COLUMN IF NOT EXISTS archived_kind VARCHAR(32) NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS articles ADD COLUMN IF NOT EXISTS archived_reason TEXT NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS articles ADD COLUMN IF NOT EXISTS archived_at TIMESTAMP NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS user_workspaces ADD COLUMN IF NOT EXISTS openrouter_api_key_enc TEXT NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS user_workspaces ADD COLUMN IF NOT EXISTS telegram_bot_token_enc TEXT NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS user_workspaces ADD COLUMN IF NOT EXISTS telegram_review_chat_id VARCHAR(255) NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS user_workspaces ADD COLUMN IF NOT EXISTS telegram_channel_id VARCHAR(255) NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS user_workspaces ADD COLUMN IF NOT EXISTS telegram_signature VARCHAR(255) NULL"))
        conn.execute(text("ALTER TABLE IF EXISTS user_workspaces ADD COLUMN IF NOT EXISTS timezone_name VARCHAR(64) NULL"))

        # Only backfill content_mode when the column is added for the first time.
        col_exists = bool(
            conn.execute(
                text(
                    "SELECT EXISTS ("
                    "  SELECT 1 FROM information_schema.columns "
                    "  WHERE table_name='articles' AND column_name='content_mode'"
                    ")"
                )
            ).scalar()
        )
        if not col_exists:
            conn.execute(
                text(
                    "ALTER TABLE IF EXISTS articles "
                    "ADD COLUMN IF NOT EXISTS content_mode VARCHAR(20) NOT NULL DEFAULT 'summary_only'"
                )
            )
            conn.execute(
                text(
                    "UPDATE articles "
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

    Base.metadata.create_all(bind=engine)
