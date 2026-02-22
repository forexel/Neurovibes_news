from datetime import date, datetime
from enum import Enum

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Enum as SQLEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


class ArticleStatus(str, Enum):
    NEW = "new"
    INBOX = "inbox"
    REVIEW = "review"
    DOUBLE = "double"
    SCORED = "scored"
    SELECTED_HOURLY = "selected_hourly"
    READY = "ready"
    PUBLISHED = "published"
    ARCHIVED = "archived"
    REJECTED = "rejected"


class PublishStatus(str, Enum):
    PENDING = "pending"
    SUCCESS = "success"
    FAILED = "failed"


class UserRole(str, Enum):
    ADMIN = "admin"
    EDITOR = "editor"
    REVIEWER = "reviewer"


class DecisionMode(str, Enum):
    MANUAL = "manual"
    AUTO = "auto"


class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    rss_url: Mapped[str] = mapped_column(String(1024), unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="rss")  # rss|html
    priority_rank: Mapped[int] = mapped_column(Integer, nullable=False)
    trust_score: Mapped[float] = mapped_column(Float, nullable=False, default=7.0)
    is_active: Mapped[bool] = mapped_column(default=True)
    is_deleted: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class RawFeedEntry(Base):
    __tablename__ = "raw_feed_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)
    entry_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    parsed_article: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("source_id", "external_id", name="uq_raw_feed_source_external"),
        UniqueConstraint("source_id", "content_hash", name="uq_raw_feed_source_content"),
    )


class Article(Base):
    __tablename__ = "articles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    raw_feed_entry_id: Mapped[int | None] = mapped_column(ForeignKey("raw_feed_entries.id"), nullable=True)
    external_id: Mapped[str | None] = mapped_column(String(512), nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    subtitle: Mapped[str] = mapped_column(Text, default="", nullable=False)
    tags: Mapped[dict] = mapped_column(JSON, default=list, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    content_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="summary_only")
    image_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    canonical_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    cluster_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    double_of_article_id: Mapped[int | None] = mapped_column(ForeignKey("articles.id"), nullable=True)
    status: Mapped[ArticleStatus] = mapped_column(SQLEnum(ArticleStatus), default=ArticleStatus.NEW, nullable=False)

    ru_title: Mapped[str | None] = mapped_column(Text, nullable=True)
    ru_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    short_hook: Mapped[str | None] = mapped_column(Text, nullable=True)
    generated_image_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    scheduled_publish_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    # Hour bucket (UTC, naive) for Selected Hour backfill/dedup.
    selected_hour_bucket_utc: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    archived_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)  # delete|hide|filter
    archived_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    source = relationship("Source")
    scores = relationship("Score", back_populates="article", uselist=False)

    __table_args__ = (
        UniqueConstraint("canonical_url", name="uq_articles_canonical_url"),
        UniqueConstraint("source_id", "external_id", name="uq_article_source_external"),
        Index("ix_articles_status_created", "status", "created_at"),
    )


class RawPageSnapshot(Base):
    __tablename__ = "raw_page_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    article_id: Mapped[int | None] = mapped_column(ForeignKey("articles.id"), nullable=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    final_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    html_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    latency_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    parse_quality: Mapped[float | None] = mapped_column(Float, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class SourceHealthMetric(Base):
    __tablename__ = "source_health_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("sources.id"), nullable=False)
    window_started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    window_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    success_rate: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    avg_latency_ms: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    parse_quality_avg: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    stale_minutes: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (Index("ix_source_health_source_window", "source_id", "window_started_at"),)


class ArticleEmbedding(Base):
    __tablename__ = "article_embeddings"

    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), primary_key=True)
    embedding: Mapped[list[float]] = mapped_column(Vector(1536), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class Score(Base):
    __tablename__ = "scores"

    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), primary_key=True)
    significance: Mapped[float] = mapped_column(Float, nullable=False)
    freshness: Mapped[float] = mapped_column(Float, nullable=False)
    relevance: Mapped[float] = mapped_column(Float, nullable=False)
    virality: Mapped[float] = mapped_column(Float, nullable=False)
    uniqueness: Mapped[float] = mapped_column(Float, nullable=False)
    source_trust: Mapped[float] = mapped_column(Float, nullable=False)
    longevity: Mapped[float] = mapped_column(Float, nullable=False)
    scale: Mapped[float] = mapped_column(Float, nullable=False)
    final_score: Mapped[float] = mapped_column(Float, nullable=False)
    reasoning: Mapped[str | None] = mapped_column(Text, nullable=True)
    features: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    uncertainty: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    article = relationship("Article", back_populates="scores")


class ContentVersion(Base):
    __tablename__ = "content_versions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), nullable=False)
    version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    ru_title: Mapped[str] = mapped_column(Text, nullable=False)
    ru_summary: Mapped[str] = mapped_column(Text, nullable=False)
    short_hook: Mapped[str] = mapped_column(Text, nullable=False)
    extraction_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    quality_report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    image_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    image_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    selected_by_editor: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (UniqueConstraint("article_id", "version_no", name="uq_content_versions_article_version"),)


class EditorFeedback(Base):
    __tablename__ = "editor_feedback"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), nullable=False)
    explanation_text: Mapped[str] = mapped_column(Text, nullable=False)
    reason_codes: Mapped[list | None] = mapped_column(JSON, nullable=True)
    confidence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    liked_aspects: Mapped[str | None] = mapped_column(Text, nullable=True)
    disliked_aspects: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class SelectionDecision(Base):
    __tablename__ = "selection_decisions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chosen_article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), nullable=False)
    rejected_article_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    candidates: Mapped[list | None] = mapped_column(JSON, nullable=True)
    decision_mode: Mapped[DecisionMode] = mapped_column(SQLEnum(DecisionMode), default=DecisionMode.MANUAL, nullable=False)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class DailySelection(Base):
    __tablename__ = "daily_selections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    selected_date: Mapped[date] = mapped_column(Date, nullable=False)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("selected_date", "article_id", name="uq_daily_selection_date_article"),
        Index("ix_daily_selections_date_active", "selected_date", "active"),
    )


class PublishJob(Base):
    __tablename__ = "publish_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), nullable=False)
    telegram_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[PublishStatus] = mapped_column(SQLEnum(PublishStatus), default=PublishStatus.PENDING, nullable=False)
    error_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class PreferenceProfile(Base):
    __tablename__ = "preference_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    profile_text: Mapped[str] = mapped_column(Text, nullable=False)
    active: Mapped[bool] = mapped_column(default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class RankingExample(Base):
    __tablename__ = "ranking_examples"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(64), nullable=False)
    context_hour: Mapped[int] = mapped_column(Integer, nullable=False)
    context_day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)
    topic: Mapped[str | None] = mapped_column(String(64), nullable=True)
    label: Mapped[int] = mapped_column(Integer, nullable=False)
    features: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (Index("ix_ranking_examples_batch", "batch_id"),)


class ModelArtifact(Base):
    __tablename__ = "model_artifacts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(64), nullable=False)
    artifact_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class TrainingEvent(Base):
    __tablename__ = "training_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), nullable=False)
    decision: Mapped[str] = mapped_column(String(32), nullable=False)  # publish|top_pick|hide|delete|defer|skip
    label: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # 1/0
    hour_bucket: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    candidate_set_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    features_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    reason_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason_tags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    reason_positive_tags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    reason_negative_tags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    reason_sentiment: Mapped[str | None] = mapped_column(String(16), nullable=True)  # positive|negative|mixed|neutral
    rule_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    ml_score_at_decision: Mapped[float | None] = mapped_column(Float, nullable=True)
    model_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    override: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    event_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    article_published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    delay_minutes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    final_outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)  # published|expired|deleted|hidden

    __table_args__ = (
        Index("ix_training_events_created", "created_at"),
        Index("ix_training_events_article_created", "article_id", "created_at"),
        Index("ix_training_events_decision_created", "decision", "created_at"),
    )


class DriftMetric(Base):
    __tablename__ = "drift_metrics"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    metric_name: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    drifted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[UserRole] = mapped_column(SQLEnum(UserRole), nullable=False, default=UserRole.EDITOR)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (Index("ix_audit_logs_created", "created_at"),)


class UserWorkspace(Base):
    __tablename__ = "user_workspaces"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, unique=True)
    channel_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    channel_theme: Mapped[str | None] = mapped_column(Text, nullable=True)
    sources_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    audience_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    audience_tags: Mapped[list | None] = mapped_column(JSON, nullable=True)
    scoring_notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    openrouter_api_key_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_bot_token_enc: Mapped[str | None] = mapped_column(Text, nullable=True)
    telegram_review_chat_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    telegram_channel_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    telegram_signature: Mapped[str | None] = mapped_column(String(255), nullable=True)
    timezone_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    onboarding_step: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    onboarding_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class ScoreParameter(Base):
    __tablename__ = "score_parameters"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    weight: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    influence_rule: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class ReasonTagCatalog(Base):
    __tablename__ = "reason_tag_catalog"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    title_ru: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)


class LLMUsageLog(Base):
    __tablename__ = "llm_usage_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    operation: Mapped[str] = mapped_column(String(128), nullable=False, default="unknown")
    model: Mapped[str] = mapped_column(String(128), nullable=False, default="unknown")
    kind: Mapped[str] = mapped_column(String(32), nullable=False, default="chat")  # chat|embedding|image
    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (Index("ix_llm_usage_logs_created", "created_at"),)


class RuntimeSetting(Base):
    __tablename__ = "runtime_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    scope: Mapped[str] = mapped_column(String(32), nullable=False, default="global")  # global|topic
    topic_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("scope", "topic_key", "key", name="uq_runtime_settings_scope_topic_key"),
        Index("ix_runtime_settings_scope_topic", "scope", "topic_key"),
    )


class TelegramReviewJob(Base):
    __tablename__ = "telegram_review_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(128), nullable=False)
    review_message_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="sent")  # sent|published|deleted|failed
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("article_id", name="uq_telegram_review_job_article"),
        Index("ix_telegram_review_jobs_status_created", "status", "created_at"),
    )


class TelegramPendingReason(Base):
    __tablename__ = "telegram_pending_reasons"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(128), nullable=False)
    user_id: Mapped[str] = mapped_column(String(64), nullable=False)
    article_id: Mapped[int] = mapped_column(ForeignKey("articles.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)  # publish|delete
    prompt_message_id: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint("prompt_message_id", name="uq_telegram_pending_prompt_msg"),
        Index("ix_telegram_pending_chat_user", "chat_id", "user_id"),
    )


class TelegramBotKV(Base):
    __tablename__ = "telegram_bot_kv"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)
