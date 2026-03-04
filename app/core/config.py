import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    app_env: str = os.getenv("APP_ENV", "development")
    database_url: str = os.getenv("DATABASE_URL", "postgresql+psycopg://news_user:news_pass_local@db:5432/news_publisher")

    openrouter_api_key: str = os.getenv("OPENROUTER_API_KEY", "")
    openrouter_base_url: str = os.getenv("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
    openai_timeout_seconds: float = float(os.getenv("OPENAI_TIMEOUT_SECONDS", "60"))
    openai_max_retries: int = int(os.getenv("OPENAI_MAX_RETRIES", "2"))

    llm_text_model: str = os.getenv("LLM_TEXT_MODEL", "openai/gpt-4o-mini")
    llm_image_model: str = os.getenv("LLM_IMAGE_MODEL", "openai/gpt-5.2-chat")
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "openai/text-embedding-3-small")

    telegram_bot_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_channel_id: str = os.getenv("TELEGRAM_CHANNEL_ID", "")
    telegram_review_chat_id: str = os.getenv("TELEGRAM_REVIEW_CHAT_ID", "")
    telegram_signature: str = os.getenv("TELEGRAM_SIGNATURE", "@neuro_vibes_future")
    auto_publish_confidence_threshold: float = float(os.getenv("AUTO_PUBLISH_CONFIDENCE_THRESHOLD", "0.82"))
    auto_publish_uncertainty_threshold: float = float(os.getenv("AUTO_PUBLISH_UNCERTAINTY_THRESHOLD", "0.18"))

    jwt_secret: str = os.getenv("JWT_SECRET", "change_this_in_production")
    jwt_algo: str = os.getenv("JWT_ALGO", "HS256")
    jwt_ttl_minutes: int = int(os.getenv("JWT_TTL_MINUTES", "720"))
    admin_email: str = os.getenv("ADMIN_EMAIL", "admin@local")
    admin_password: str = os.getenv("ADMIN_PASSWORD", "admin123")

    dedup_similarity_threshold: float = float(os.getenv("DEDUP_SIMILARITY_THRESHOLD", "0.86"))
    source_trust_default: float = float(os.getenv("SOURCE_TRUST_DEFAULT", "7.0"))
    model_artifacts_dir: str = os.getenv("MODEL_ARTIFACTS_DIR", "app/static/models")

    score_significance_weight: float = float(os.getenv("W_SIGNIFICANCE", "0.25"))
    score_freshness_weight: float = float(os.getenv("W_FRESHNESS", "0.15"))
    score_relevance_weight: float = float(os.getenv("W_RELEVANCE", "0.20"))
    score_virality_weight: float = float(os.getenv("W_VIRALITY", "0.15"))
    score_uniqueness_weight: float = float(os.getenv("W_UNIQUENESS", "0.10"))
    score_longevity_weight: float = float(os.getenv("W_LONGEVITY", "0.10"))
    score_scale_weight: float = float(os.getenv("W_SCALE", "0.05"))
    score_source_trust_weight: float = float(os.getenv("W_SOURCE_TRUST", "0.10"))

    auto_score_on_ingest: bool = os.getenv("AUTO_SCORE_ON_INGEST", "true").lower() in {"1", "true", "yes", "on"}
    browser_cookies_json: str = os.getenv("BROWSER_COOKIES_JSON", "")
    minio_endpoint: str = os.getenv("MINIO_ENDPOINT", "minio:9000")
    minio_access_key: str = os.getenv("MINIO_ACCESS_KEY", "minioadmin")
    minio_secret_key: str = os.getenv("MINIO_SECRET_KEY", "minioadmin123")
    minio_bucket: str = os.getenv("MINIO_BUCKET", "news-images")
    minio_use_ssl: bool = os.getenv("MINIO_USE_SSL", "false").lower() in {"1", "true", "yes", "on"}
    minio_public_base_url: str = os.getenv("MINIO_PUBLIC_BASE_URL", "http://localhost:9010")
    minio_compress_enabled: bool = os.getenv("MINIO_COMPRESS_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    minio_compress_format: str = os.getenv("MINIO_COMPRESS_FORMAT", "WEBP")
    minio_compress_quality: int = int(os.getenv("MINIO_COMPRESS_QUALITY", "82"))
    minio_max_width: int = int(os.getenv("MINIO_MAX_WIDTH", "1920"))
    llm_chat_input_cost_per_mtok: float = float(os.getenv("LLM_CHAT_INPUT_COST_PER_MTOK", "0.15"))
    llm_chat_output_cost_per_mtok: float = float(os.getenv("LLM_CHAT_OUTPUT_COST_PER_MTOK", "0.60"))
    llm_embedding_input_cost_per_mtok: float = float(os.getenv("LLM_EMBED_INPUT_COST_PER_MTOK", "0.02"))


settings = Settings()
