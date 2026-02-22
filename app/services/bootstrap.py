from sqlalchemy import select

from app.core.config import settings
from app.db import session_scope
from app.models import ScoreParameter, Source, User, UserRole, UserWorkspace
from app.services.auth import ensure_admin_user
from app.services.runtime_settings import seed_runtime_settings
from app.services.user_secrets import encrypt_secret
from app.sources import SOURCES

DISABLED_DEFAULT_SOURCES = {"Synced Review"}


def seed_sources() -> int:
    inserted = 0
    with session_scope() as session:
        existing_by_name = {row.name: row for row in session.scalars(select(Source)).all()}
        existing_by_url = {row.rss_url: row for row in existing_by_name.values()}
        for item in SOURCES:
            # Back-compat: (name, url, rank, trust) or (name, url, rank, trust, kind)
            if len(item) == 5:
                name, rss_url, priority_rank, trust_score, kind = item
            else:
                name, rss_url, priority_rank, trust_score = item
                kind = "rss"
            source = existing_by_name.get(name) or existing_by_url.get(rss_url)
            if source is None:
                source = Source(
                    name=name,
                    rss_url=rss_url,
                    kind=str(kind or "rss"),
                    priority_rank=priority_rank,
                    trust_score=trust_score if trust_score is not None else settings.source_trust_default,
                    is_active=True,
                )
                session.add(source)
                inserted += 1
            else:
                # Keep config in sync on startup and avoid duplicate insert crashes.
                source.name = name
                source.rss_url = rss_url
                source.kind = str(kind or "rss")
                source.priority_rank = priority_rank
                source.trust_score = trust_score if trust_score is not None else settings.source_trust_default
                if source.is_active is None:
                    source.is_active = True
            if source.name in DISABLED_DEFAULT_SOURCES:
                source.is_active = False
                if hasattr(source, "is_deleted"):
                    source.is_deleted = True

        # Disable known-stale defaults even if they were removed from SOURCES.
        for stale_name in DISABLED_DEFAULT_SOURCES:
            stale = existing_by_name.get(stale_name)
            if stale is not None:
                stale.is_active = False
                if hasattr(stale, "is_deleted"):
                    stale.is_deleted = True

    _seed_score_parameters()
    seed_runtime_settings()
    ensure_admin_user()
    _migrate_env_secrets_to_workspace()
    return inserted


def _seed_score_parameters() -> None:
    defaults = [
        ("significance", "Significance", 0.16, "Выше при большом влиянии на рынок/пользователей."),
        ("relevance", "Relevance", 0.17, "Выше, если это core AI/LLM тема канала."),
        ("novelty", "Novelty", 0.13, "Выше, если это качественно новое событие."),
        ("trend_velocity", "Trend Velocity", 0.09, "Выше, если тема быстро набирает покрытие."),
        ("coverage", "Cross-source Coverage", 0.07, "Выше при подтверждении несколькими источниками."),
        ("virality", "Virality", 0.07, "Выше при потенциале обсуждения/репостов."),
        ("longevity", "Longevity", 0.06, "Выше, если новость не устаревает за сутки."),
        ("scale", "Scale", 0.05, "Выше при глобальном эффекте."),
        ("freshness", "Freshness", 0.05, "Выше для более свежих публикаций."),
        ("entity_count", "Entity Count", 0.03, "Выше при наличии значимых акторов."),
        ("number_count", "Number Count", 0.02, "Выше при наличии конкретных цифр/метрик."),
        ("source_priority", "Source Priority", 0.02, "Выше для более приоритетного источника."),
        ("business_it", "Business IT Impact", 0.06, "Выше для тем, понятных аудитории бизнеса/обычных пользователей."),
        ("editor_style", "Editor Style Match", 0.02, "Выше, если похоже на ранее одобренные редактором статьи."),
    ]
    with session_scope() as session:
        existing = {row.key: row for row in session.scalars(select(ScoreParameter)).all()}
        for key, title, weight, rule in defaults:
            row = existing.get(key)
            if row is None:
                session.add(
                    ScoreParameter(
                        key=key,
                        title=title,
                        description=title,
                        weight=weight,
                        influence_rule=rule,
                        is_active=True,
                    )
                )
            else:
                if not row.title:
                    row.title = title
                if not row.description:
                    row.description = title
                if not row.influence_rule:
                    row.influence_rule = rule


def _migrate_env_secrets_to_workspace() -> None:
    """
    One-time helper for legacy deployments: copy secrets from .env into DB (encrypted),
    so the owner can later remove them from env and manage per-user keys in UI.

    Safety: only runs when it's unambiguous who should receive the secrets:
    - if there is exactly 1 user, OR
    - if admin_email user exists and is ADMIN.
    """
    env_or_key = (settings.openrouter_api_key or "").strip()
    env_tg_token = (settings.telegram_bot_token or "").strip()
    env_tg_review = (settings.telegram_review_chat_id or "").strip()
    env_tg_channel = (settings.telegram_channel_id or "").strip()
    env_tg_sig = (settings.telegram_signature or "").strip()

    if not any([env_or_key, env_tg_token, env_tg_review, env_tg_channel, env_tg_sig]):
        return

    with session_scope() as session:
        users = session.scalars(select(User).where(User.is_active.is_(True))).all()
        target_user: User | None = None
        if len(users) == 1:
            target_user = users[0]
        else:
            admin = session.scalars(select(User).where(User.email == settings.admin_email)).first()
            if admin and admin.role == UserRole.ADMIN:
                target_user = admin
        if not target_user:
            return

        ws = session.scalars(select(UserWorkspace).where(UserWorkspace.user_id == target_user.id)).first()
        if ws is None:
            ws = UserWorkspace(user_id=target_user.id)
            session.add(ws)

        if env_or_key and not (ws.openrouter_api_key_enc or "").strip():
            ws.openrouter_api_key_enc = encrypt_secret(env_or_key)

        if env_tg_token and not (ws.telegram_bot_token_enc or "").strip():
            ws.telegram_bot_token_enc = encrypt_secret(env_tg_token)

        if env_tg_review and not (ws.telegram_review_chat_id or "").strip():
            ws.telegram_review_chat_id = env_tg_review
        if env_tg_channel and not (ws.telegram_channel_id or "").strip():
            ws.telegram_channel_id = env_tg_channel
        if env_tg_sig and not (ws.telegram_signature or "").strip():
            ws.telegram_signature = env_tg_sig
        if not (ws.timezone_name or "").strip():
            ws.timezone_name = "Europe/Moscow"
