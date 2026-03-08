"""article previews projection table

Revision ID: 0002_article_previews_projection
Revises: 0001_initial
Create Date: 2026-03-08
"""

from alembic import op


revision = "0002_article_previews_projection"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS public.article_previews (
            id INTEGER PRIMARY KEY REFERENCES public.articles(id) ON DELETE CASCADE,
            status VARCHAR(32) NOT NULL,
            content_mode VARCHAR(20) NOT NULL DEFAULT 'summary_only',
            double_of_article_id INTEGER NULL,
            title TEXT NOT NULL,
            subtitle TEXT NOT NULL DEFAULT '',
            ru_title TEXT NULL,
            ru_summary TEXT NULL,
            short_hook TEXT NULL,
            source_id INTEGER NOT NULL REFERENCES public.sources(id),
            published_at TIMESTAMP NULL,
            created_at TIMESTAMP NOT NULL,
            canonical_url VARCHAR(2048) NOT NULL,
            generated_image_path VARCHAR(1024) NULL,
            scheduled_publish_at TIMESTAMP NULL,
            ml_recommendation VARCHAR(32) NULL,
            ml_recommendation_confidence DOUBLE PRECISION NULL,
            ml_recommendation_reason TEXT NULL,
            ml_model_version VARCHAR(64) NULL,
            ml_recommendation_at TIMESTAMP NULL,
            archived_kind VARCHAR(32) NULL,
            archived_reason TEXT NULL,
            archived_at TIMESTAMP NULL,
            ml_verdict_confirmed BOOLEAN NULL,
            ml_verdict_comment TEXT NULL,
            ml_verdict_tags JSON NULL,
            ml_verdict_updated_at TIMESTAMP NULL
        )
        """
    )

    op.execute("CREATE INDEX IF NOT EXISTS ix_article_previews_created_at ON public.article_previews (created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_article_previews_status_created ON public.article_previews (status, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_article_previews_source_created ON public.article_previews (source_id, created_at DESC)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_article_previews_ml_conf ON public.article_previews (ml_recommendation_confidence DESC)")

    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.sync_article_preview()
        RETURNS TRIGGER AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            DELETE FROM public.article_previews WHERE id = OLD.id;
            RETURN OLD;
          END IF;

          INSERT INTO public.article_previews (
            id, status, content_mode, double_of_article_id, title, subtitle, ru_title, ru_summary, short_hook, source_id,
            published_at, created_at, canonical_url, generated_image_path, scheduled_publish_at,
            ml_recommendation, ml_recommendation_confidence, ml_recommendation_reason, ml_model_version, ml_recommendation_at,
            archived_kind, archived_reason, archived_at, ml_verdict_confirmed, ml_verdict_comment, ml_verdict_tags, ml_verdict_updated_at
          ) VALUES (
            NEW.id, NEW.status::text, NEW.content_mode, NEW.double_of_article_id, NEW.title, NEW.subtitle, NEW.ru_title, NEW.ru_summary, NEW.short_hook, NEW.source_id,
            NEW.published_at, NEW.created_at, NEW.canonical_url, NEW.generated_image_path, NEW.scheduled_publish_at,
            NEW.ml_recommendation, NEW.ml_recommendation_confidence, NEW.ml_recommendation_reason, NEW.ml_model_version, NEW.ml_recommendation_at,
            NEW.archived_kind, NEW.archived_reason, NEW.archived_at, NEW.ml_verdict_confirmed, NEW.ml_verdict_comment, NEW.ml_verdict_tags, NEW.ml_verdict_updated_at
          )
          ON CONFLICT (id) DO UPDATE SET
            status = EXCLUDED.status,
            content_mode = EXCLUDED.content_mode,
            double_of_article_id = EXCLUDED.double_of_article_id,
            title = EXCLUDED.title,
            subtitle = EXCLUDED.subtitle,
            ru_title = EXCLUDED.ru_title,
            ru_summary = EXCLUDED.ru_summary,
            short_hook = EXCLUDED.short_hook,
            source_id = EXCLUDED.source_id,
            published_at = EXCLUDED.published_at,
            created_at = EXCLUDED.created_at,
            canonical_url = EXCLUDED.canonical_url,
            generated_image_path = EXCLUDED.generated_image_path,
            scheduled_publish_at = EXCLUDED.scheduled_publish_at,
            ml_recommendation = EXCLUDED.ml_recommendation,
            ml_recommendation_confidence = EXCLUDED.ml_recommendation_confidence,
            ml_recommendation_reason = EXCLUDED.ml_recommendation_reason,
            ml_model_version = EXCLUDED.ml_model_version,
            ml_recommendation_at = EXCLUDED.ml_recommendation_at,
            archived_kind = EXCLUDED.archived_kind,
            archived_reason = EXCLUDED.archived_reason,
            archived_at = EXCLUDED.archived_at,
            ml_verdict_confirmed = EXCLUDED.ml_verdict_confirmed,
            ml_verdict_comment = EXCLUDED.ml_verdict_comment,
            ml_verdict_tags = EXCLUDED.ml_verdict_tags,
            ml_verdict_updated_at = EXCLUDED.ml_verdict_updated_at;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )

    op.execute("DROP TRIGGER IF EXISTS trg_articles_sync_preview ON public.articles")
    op.execute(
        """
        CREATE TRIGGER trg_articles_sync_preview
        AFTER INSERT OR UPDATE OR DELETE ON public.articles
        FOR EACH ROW EXECUTE FUNCTION public.sync_article_preview()
        """
    )

    # Backfill existing production data to ensure preview reads are instant and complete.
    op.execute(
        """
        INSERT INTO public.article_previews (
          id, status, content_mode, double_of_article_id, title, subtitle, ru_title, ru_summary, short_hook, source_id,
          published_at, created_at, canonical_url, generated_image_path, scheduled_publish_at,
          ml_recommendation, ml_recommendation_confidence, ml_recommendation_reason, ml_model_version, ml_recommendation_at,
          archived_kind, archived_reason, archived_at, ml_verdict_confirmed, ml_verdict_comment, ml_verdict_tags, ml_verdict_updated_at
        )
        SELECT
          a.id, a.status::text, a.content_mode, a.double_of_article_id, a.title, a.subtitle, a.ru_title, a.ru_summary, a.short_hook, a.source_id,
          a.published_at, a.created_at, a.canonical_url, a.generated_image_path, a.scheduled_publish_at,
          a.ml_recommendation, a.ml_recommendation_confidence, a.ml_recommendation_reason, a.ml_model_version, a.ml_recommendation_at,
          a.archived_kind, a.archived_reason, a.archived_at, a.ml_verdict_confirmed, a.ml_verdict_comment, a.ml_verdict_tags, a.ml_verdict_updated_at
        FROM public.articles a
        ON CONFLICT (id) DO UPDATE SET
          status = EXCLUDED.status,
          content_mode = EXCLUDED.content_mode,
          double_of_article_id = EXCLUDED.double_of_article_id,
          title = EXCLUDED.title,
          subtitle = EXCLUDED.subtitle,
          ru_title = EXCLUDED.ru_title,
          ru_summary = EXCLUDED.ru_summary,
          short_hook = EXCLUDED.short_hook,
          source_id = EXCLUDED.source_id,
          published_at = EXCLUDED.published_at,
          created_at = EXCLUDED.created_at,
          canonical_url = EXCLUDED.canonical_url,
          generated_image_path = EXCLUDED.generated_image_path,
          scheduled_publish_at = EXCLUDED.scheduled_publish_at,
          ml_recommendation = EXCLUDED.ml_recommendation,
          ml_recommendation_confidence = EXCLUDED.ml_recommendation_confidence,
          ml_recommendation_reason = EXCLUDED.ml_recommendation_reason,
          ml_model_version = EXCLUDED.ml_model_version,
          ml_recommendation_at = EXCLUDED.ml_recommendation_at,
          archived_kind = EXCLUDED.archived_kind,
          archived_reason = EXCLUDED.archived_reason,
          archived_at = EXCLUDED.archived_at,
          ml_verdict_confirmed = EXCLUDED.ml_verdict_confirmed,
          ml_verdict_comment = EXCLUDED.ml_verdict_comment,
          ml_verdict_tags = EXCLUDED.ml_verdict_tags,
          ml_verdict_updated_at = EXCLUDED.ml_verdict_updated_at
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_articles_sync_preview ON public.articles")
    op.execute("DROP FUNCTION IF EXISTS public.sync_article_preview()")
    op.execute("DROP TABLE IF EXISTS public.article_previews")
