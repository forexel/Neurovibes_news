from sqlalchemy import select

from app.core.config import settings
from app.db import session_scope
from app.models import ScoreParameter, Source
from app.services.auth import ensure_admin_user
from app.sources import SOURCES


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

    _seed_score_parameters()
    ensure_admin_user()
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
