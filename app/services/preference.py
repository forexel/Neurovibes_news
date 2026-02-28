from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
from sqlalchemy import func, select

from app.core.config import settings
from app.db import session_scope
from app.models import (
    Article,
    ArticleStatus,
    AuditLog,
    DecisionMode,
    DriftMetric,
    EditorFeedback,
    ModelArtifact,
    PreferenceProfile,
    ReasonTagCatalog,
    RankingExample,
    Score,
    SelectionDecision,
    TelegramReviewJob,
    TrainingEvent,
    User,
    UserWorkspace,
)
from app.services.runtime_settings import get_runtime_float
from app.services.llm import get_client, track_usage_from_response
from app.services.utils import stable_hash


MODEL_DIR = Path("app/static/models")
MODEL_DIR.mkdir(parents=True, exist_ok=True)

EDITOR_CHOICE_MODEL_NAME = "editor_choice"
PRACTICAL_RANKER_MODEL_NAME = "practical_ranker"
EDITOR_CHOICE_FEATURES = [
    "freshness",
    "source_priority",
    "entity_count",
    "number_count",
    "trend_velocity",
    "coverage",
    "cluster_size",
    "duplicate_flag",
    "has_image",
    "content_short",
    "content_medium",
    "content_long",
    "significance",
    "relevance",
    "virality",
    "longevity",
    "scale",
    "novelty",
    "business_it",
    "geek_penalty",
    "rule_score",
    "uncertainty",
    "rank_by_rule_score",
    "delta_to_best_rule",
    "hour_sin",
    "hour_cos",
    "hours_since_published_norm",
    "published_so_far_today_norm",
    "hours_left_today_norm",
    "aud_mass_audience",
    "aud_business",
    "aud_future",
    "aud_hype",
    "aud_ru_relevance",
    # reason tags (one-hot)
    "tag_breakthrough",
    "tag_funding",
    "tag_product_release",
    "tag_benchmark",
    "tag_regulation",
    "tag_practical_tool",
    "tag_global_shift",
    "tag_hype",
    "tag_too_local",
    "tag_duplicate",
    # polarity-aware reason tags (same tag can be positive or negative in different decisions)
    "tag_pos_hype",
    "tag_neg_hype",
    "tag_pos_mass_audience",
    "tag_neg_not_mass_audience",
    "tag_pos_business_impact",
    "tag_neg_no_business_use",
    "tag_pos_future_trend",
    "tag_neg_short_lived",
    "tag_neg_too_technical",
    "tag_neg_low_significance",
    "tag_pos_market_signal",
    "tag_neg_too_local",
]
PRACTICAL_RANKER_FEATURES = [
    "practical_value",
    "audience_fit",
    "actionability",
    "content_type_tool",
    "content_type_case",
    "content_type_playbook",
    "content_type_hot",
    "content_type_trend",
    "use_case_marketing",
    "use_case_sales",
    "use_case_support",
    "use_case_operations",
    "use_case_founder",
    "risk_too_technical",
    "risk_funding_hype",
    "risk_infra_noise",
    "risk_weak_source",
    "risk_wow_but_risky",
    "freshness",
    "source_priority",
    "text_length_norm",
    "digit_count_norm",
    "contains_how_to",
    "contains_template",
    "contains_prompt",
    "rule_score",
]
_TAGS = [
    "breakthrough",
    "funding",
    "product_release",
    "benchmark",
    "regulation",
    "practical_tool",
    "global_shift",
    "hype",
    "too_local",
    "duplicate",
    "too_technical",
    "not_mass_audience",
    "short_lived",
    "low_significance",
    "no_business_use",
    "market_signal",
    "future_trend",
    "mass_audience",
    "business_impact",
    "ru_relevance",
]
_BASE_REASON_TAGS_RU: dict[str, str] = {
    "breakthrough": "Потенциальный прорыв",
    "funding": "Инвестиции / сделка",
    "product_release": "Релиз / новая версия",
    "benchmark": "Бенчмарк / цифры / сравнение",
    "regulation": "Безопасность / регулирование / риски",
    "practical_tool": "Понятная практическая польза",
    "global_shift": "Сильный сигнал рынку / крупным игрокам",
    "hype": "Хайп / короткая значимость / шум",
    "too_local": "Слишком локально / не для РФ",
    "duplicate": "Повтор темы / дубль",
    # richer custom tags (can be added by user / llm)
    "too_technical": "Слишком техническая / гиковская",
    "not_mass_audience": "Не для массовой аудитории",
    "short_lived": "Короткоживущая новость",
    "low_significance": "Низкая значимость",
    "no_business_use": "Непонятна польза для бизнеса",
    "security_risk": "Безопасность / мошенничество / риск",
    "market_signal": "Сигнал рынку / стратегия",
    "future_trend": "Будущее / стратегический тренд",
    "mass_audience": "Массовый сегмент / широкая аудитория",
    "business_impact": "Важна для бизнеса / экономики проектов",
    "ru_relevance": "Релевантно для РФ / русскоязычной аудитории",
}
_AUDIENCE_BASE_TAGS = ["mass_audience", "business", "future", "hype", "technology", "security", "practical", "ru_relevance"]
_POSITIVE_DEFAULT_TAGS = {
    "breakthrough",
    "product_release",
    "practical_tool",
    "global_shift",
    "market_signal",
    "future_trend",
    "mass_audience",
    "business_impact",
    "ru_relevance",
    "benchmark",
    "regulation",
}
_NEGATIVE_DEFAULT_TAGS = {
    "too_local",
    "duplicate",
    "too_technical",
    "not_mass_audience",
    "short_lived",
    "low_significance",
    "no_business_use",
}
_AMBIVALENT_TAGS = {"hype", "funding"}


def _sigmoid(z: float) -> float:
    z = float(z)
    if z >= 0:
        ez = np.exp(-z)
        return float(1.0 / (1.0 + ez))
    ez = np.exp(z)
    return float(ez / (1.0 + ez))


def _norm01(v: float | int | None, denom: float = 10.0) -> float:
    try:
        x = float(v or 0.0) / float(denom)
    except Exception:
        x = 0.0
    return float(max(0.0, min(1.0, x)))


def _guess_reason_tags(reason_text: str | None) -> list[str]:
    text = (reason_text or "").lower()
    tags: list[str] = []
    # Direct mappings from internal gate names used in archived_reason / filters.
    gate_map = {
        "personnel_move_gate": ["hype", "too_local"],
        "geek_gate": ["hype"],
        "deep_technical_gate": ["hype"],
        "technical_gate": ["hype"],
        "low_relevance": ["hype"],
        "local_practical_gate": ["too_local"],
        "summary_boring_gate": ["hype"],
        "investing_gate": ["funding"],
    }
    for key, mapped in gate_map.items():
        if key in text:
            tags.extend(mapped)

    rules = {
        "breakthrough": ["прорыв", "breakthrough", "революц", "first", "впервые"],
        "funding": ["инвест", "funding", "m&a", "сделк", "acquire", "раунд", "финанс", "valuation", "оценк"],
        "product_release": ["релиз", "release", "launch", "launched", "update", "версия", "запуск", "доступн", "rollout"],
        "benchmark": ["benchmark", "бенчмарк", "точност", "latency", "скорость", "сравнен"],
        "regulation": ["регуля", "закон", "policy", "compliance", "безопас", "fraud", "мошенн", "privacy", "данных", "security"],
        "practical_tool": ["практич", "tool", "инструмент", "для бизнеса", "workflow", "use case", "полезн", "применим", "можно использовать"],
        "global_shift": ["рынок", "global", "стратег", "монопол", "platform shift", "сигнал", "крупный игрок", "google", "openai", "meta", "nvidia"],
        "hype": [
            "хайп", "скучн", "мнение", "opinion", "noise", "неважно", "не очень интересно",
            "массе не интересно", "массовому сегменту не интересно", "гиковск", "слишком техническ",
            "узконише", "нудн", "завтра никто не вспомнит", "короткоигра", "точечная новость"
        ],
        "too_local": ["локал", "india", "индия", "узко", "too local", "не для нашей", "для рф не", "не актуален для рф", "далеко от нас"],
        "duplicate": ["дубл", "повтор", "duplicate", "already"],
        "too_technical": ["техническ", "гиков", "узконише", "инженерн", "алгоритм", "benchmark only"],
        "not_mass_audience": ["массовому сегменту не интересно", "массе не интересно", "не для широкой", "обывател"],
        "short_lived": ["завтра никто не вспомнит", "короткоигра", "быстро забудут", "точечная новость"],
        "low_significance": ["низкая значимость", "неважно", "слабая новость", "мелкая новость"],
        "no_business_use": ["не понятно как использовать бизнесу", "непонятна польза бизнесу", "нет пользы для бизнеса"],
        "market_signal": ["сигнал рынку", "рынку важно", "стратегический сигнал", "крупная сумма"],
        "future_trend": ["будущее", "тренд", "куда идет рынок", "что происходит в мире ии"],
        "mass_audience": ["массовый сегмент", "широкой аудитории", "обычным людям", "много кому интересно", "пользователям интересно"],
        "business_impact": ["бизнесу важно", "повлияет на бизнес", "повлияет на экономику разработки", "расходы на проекты", "для предпринимателей"],
        "ru_relevance": ["для рф", "русскому рынку", "нашей аудитории", "для россии"],
    }
    for tag, keywords in rules.items():
        if any(k in text for k in keywords):
            tags.append(tag)
    return sorted(set(tags))


def _guess_reason_tag_polarity(reason_text: str | None, decision: str | None = None, tags: list[str] | None = None) -> dict:
    text = (reason_text or "").lower()
    d = (decision or "").strip().lower()
    union_tags = sorted(set(tags or _guess_reason_tags(reason_text)))
    pos: set[str] = set()
    neg: set[str] = set()

    # Default polarity by tag semantics.
    for t in union_tags:
        if t in _POSITIVE_DEFAULT_TAGS:
            pos.add(t)
        if t in _NEGATIVE_DEFAULT_TAGS:
            neg.add(t)

    # Ambivalent tags: infer from wording and decision context.
    if "hype" in union_tags:
        positive_hype_cues = [
            "хайпов", "вау", "резонанс", "модн", "интересно пользователям", "много кому интересно", "филлер", "горяч"
        ]
        negative_hype_cues = [
            "шум", "скучн", "не очень интересно", "завтра никто не вспомнит", "точечная новость", "короткоигра"
        ]
        if any(c in text for c in positive_hype_cues):
            pos.add("hype")
        if any(c in text for c in negative_hype_cues):
            neg.add("hype")
        if "hype" not in pos and "hype" not in neg:
            if d in {"publish", "top_pick"}:
                pos.add("hype")
            elif d in {"hide", "delete"}:
                neg.add("hype")

    if "funding" in union_tags:
        negative_funding_cues = ["про инвестиции", "инвестиции пока не нужно", "не наш фокус", "слишком инвестиц"]
        positive_funding_cues = ["большая сумма", "сигнал рынку", "важно для бизнеса", "повлияет на рынок"]
        if any(c in text for c in positive_funding_cues):
            pos.add("funding")
        if any(c in text for c in negative_funding_cues):
            neg.add("funding")
        if "funding" not in pos and "funding" not in neg:
            if d in {"publish", "top_pick"}:
                pos.add("funding")
            elif d in {"hide", "delete"}:
                neg.add("funding")

    # Explicit positive/negative wording can override / add signal.
    if any(x in text for x in ["не интересно массов", "не для массов", "обывателям не", "массе не интересно"]):
        neg.add("not_mass_audience")
    if any(x in text for x in ["массов", "широкой аудитории", "обычным людям", "пользователям интересно"]):
        pos.add("mass_audience")
    if any(x in text for x in ["не понятно как использовать бизнесу", "непонятна польза", "нет пользы для бизнеса"]):
        neg.add("no_business_use")
    if any(x in text for x in ["повлияет на бизнес", "расходы на проекты", "бизнесменам может быть интересно", "важно для бизнеса"]):
        pos.add("business_impact")
    if any(x in text for x in ["завтра никто не вспомнит", "короткоигра", "точечная новость"]):
        neg.add("short_lived")
    if any(x in text for x in ["слишком техническ", "гиковск", "узконише"]):
        neg.add("too_technical")

    # By decision, negative examples usually indicate "minus" rationale if still unresolved.
    if d in {"hide", "delete"}:
        for t in union_tags:
            if t not in pos and t not in _POSITIVE_DEFAULT_TAGS:
                neg.add(t)
    elif d in {"publish", "top_pick"}:
        for t in union_tags:
            if t not in neg and t not in _NEGATIVE_DEFAULT_TAGS:
                pos.add(t)

    # Keep only known tags.
    known = set(_TAGS)
    pos = {t for t in pos if t in known}
    neg = {t for t in neg if t in known}
    # allow mixed (same tag can be both in nuanced phrases), do not subtract
    all_tags = sorted(set(union_tags) | pos | neg)
    if pos and neg:
        sentiment = "mixed"
    elif pos:
        sentiment = "positive"
    elif neg:
        sentiment = "negative"
    else:
        sentiment = "neutral"
    return {
        "tags": sorted(all_tags),
        "positive_tags": sorted(pos),
        "negative_tags": sorted(neg),
        "sentiment": sentiment,
    }


def _normalize_reason_text(reason_text: str | None) -> str:
    text = str(reason_text or "").strip()
    if not text:
        return ""
    # Remove pipeline prefixes used in historical data.
    prefixes = [
        "HIDE:",
        "DELETE:",
        "LATER:",
        "SCHEDULE(custom):",
        "SCHEDULE(+1h):",
    ]
    changed = True
    while changed:
        changed = False
        for p in prefixes:
            if text.upper().startswith(p.upper()):
                text = text[len(p):].strip()
                changed = True
    return text


def _ensure_reason_tag_catalog(session) -> int:
    inserted = 0
    existing = {str(x.slug or "").strip() for x in session.scalars(select(ReasonTagCatalog)).all()}
    for slug, title_ru in _BASE_REASON_TAGS_RU.items():
        if slug in existing:
            continue
        session.add(
            ReasonTagCatalog(
                slug=slug,
                title_ru=title_ru,
                description="system tag",
                is_active=True,
                is_system=True,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
        )
        inserted += 1
    return inserted


def _get_active_reason_tag_slugs(session) -> list[str]:
    _ensure_reason_tag_catalog(session)
    rows = session.scalars(select(ReasonTagCatalog).where(ReasonTagCatalog.is_active.is_(True))).all()
    slugs = [str(r.slug or "").strip() for r in rows if (r.slug or "").strip()]
    return sorted(set(slugs))


def _classify_reason_tags_with_llm(
    session,
    *,
    reason_text: str,
    decision: str | None,
    article: Article | None,
    allow_new_tags: bool = True,
) -> dict:
    _ensure_reason_tag_catalog(session)
    base_cls = _guess_reason_tag_polarity(reason_text, decision=decision)
    if not settings.openrouter_api_key:
        return base_cls

    tag_slugs = _get_active_reason_tag_slugs(session)
    audience_desc, audience_tags = _latest_workspace_audience(session)
    title = str((article.ru_title or article.title) if article else "").strip()
    subtitle = str((article.ru_summary or article.subtitle) if article else "").strip()[:500]

    try:
        client = get_client()
        resp = client.chat.completions.create(
            model=settings.llm_text_model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "Extract editorial rejection/publish reason tags. Multi-label. Return compact JSON only."},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "allowed_tags": tag_slugs,
                            "audience_tags": audience_tags,
                            "audience_description": audience_desc[:800],
                            "decision": decision,
                            "title": title[:300],
                            "summary": subtitle,
                            "reason_text": reason_text[:1000],
                            "instructions": {
                                "multi_label": True,
                                "can_suggest_new_tags": bool(allow_new_tags),
                                "output": {
                                    "positive_tags": ["slug1"],
                                    "negative_tags": ["slug2"],
                                    "tags": ["slug1", "slug2"],
                                    "reason_sentiment": "positive|negative|mixed|neutral",
                                    "new_tags": [{"slug": "new_slug", "title_ru": "Русское название"}],
                                    "confidence": 0.0,
                                },
                            },
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
            temperature=0.1,
        )
        track_usage_from_response(resp, operation="preference.classify_reason_inline_llm", model=settings.llm_text_model, kind="chat")
        data = json.loads(resp.choices[0].message.content or "{}")
    except Exception:
        return base_cls

    llm_pos = [str(x).strip() for x in (data.get("positive_tags") or []) if str(x).strip()]
    llm_neg = [str(x).strip() for x in (data.get("negative_tags") or []) if str(x).strip()]
    llm_all = [str(x).strip() for x in (data.get("tags") or []) if str(x).strip()]

    if allow_new_tags:
        for item in (data.get("new_tags") or []):
            slug = str((item or {}).get("slug") or "").strip().lower()
            title_ru = str((item or {}).get("title_ru") or slug).strip()
            if not slug:
                continue
            if slug not in tag_slugs:
                session.add(
                    ReasonTagCatalog(
                        slug=slug[:64],
                        title_ru=title_ru[:128] or slug[:64],
                        description="llm/user discovered tag",
                        is_active=True,
                        is_system=False,
                        created_at=datetime.utcnow(),
                        updated_at=datetime.utcnow(),
                    )
                )
                tag_slugs.append(slug)

    allowed = set(tag_slugs)
    pos_tags = sorted(set(llm_pos + list(base_cls.get("positive_tags") or [])))
    neg_tags = sorted(set(llm_neg + list(base_cls.get("negative_tags") or [])))
    tags = sorted(set(llm_all + pos_tags + neg_tags + list(base_cls.get("tags") or [])))
    pos_tags = [t for t in pos_tags if t in allowed]
    neg_tags = [t for t in neg_tags if t in allowed]
    tags = [t for t in tags if t in allowed]
    sentiment = str(data.get("reason_sentiment") or base_cls.get("sentiment") or "neutral").strip().lower()
    if sentiment not in {"positive", "negative", "mixed", "neutral"}:
        sentiment = str(base_cls.get("sentiment") or "neutral")
    return {
        "tags": tags,
        "positive_tags": pos_tags,
        "negative_tags": neg_tags,
        "sentiment": sentiment,
    }


def _latest_workspace_audience(session, user_id: int | None = None) -> tuple[str, list[str]]:
    q = select(UserWorkspace)
    if user_id:
        q = q.where(UserWorkspace.user_id == int(user_id))
    ws = session.scalars(q.order_by(UserWorkspace.updated_at.desc()).limit(1)).first()
    if not ws:
        return "", []
    desc = str(ws.audience_description or "").strip()
    tags = [str(x).strip() for x in (ws.audience_tags or []) if str(x).strip()]
    return desc, sorted(set(tags))


def _audience_pref_features_from_tags(tags: list[str] | None) -> dict[str, float]:
    s = set(tags or [])
    return {
        "aud_mass_audience": 1.0 if "mass_audience" in s else 0.0,
        "aud_business": 1.0 if "business" in s else 0.0,
        "aud_future": 1.0 if "future" in s else 0.0,
        "aud_hype": 1.0 if "hype" in s else 0.0,
        "aud_ru_relevance": 1.0 if "ru_relevance" in s else 0.0,
    }


def _feature_snapshot(
    article: Article,
    score: Score | None,
    reason_tags: list[str] | None = None,
    audience_tags: list[str] | None = None,
    reason_positive_tags: list[str] | None = None,
    reason_negative_tags: list[str] | None = None,
) -> dict:
    f = (score.features or {}) if score and isinstance(score.features, dict) else {}
    now = datetime.utcnow()
    published = article.published_at or article.created_at or now
    age_hours = max(0.0, (now - published).total_seconds() / 3600.0)
    content_mode = (article.content_mode or "summary_only").strip().lower()
    content_short = 1.0 if content_mode == "summary_only" else 0.0
    content_long = 1.0 if content_mode == "full" else 0.0
    content_medium = 0.0 if (content_short or content_long) else 1.0
    rule_score = float(score.final_score or 0.0) if score else 0.0
    hour = int((article.created_at or now).hour)
    hours_left = max(0, 23 - hour)

    out = {
        "freshness": _norm01(f.get("freshness", score.freshness if score else 0.0)),
        "source_priority": _norm01(f.get("source_priority", 0.0), denom=1.0 if float(f.get("source_priority", 0.0) or 0.0) <= 1.0 else 10.0),
        "entity_count": _norm01(f.get("entity_count", 0.0), denom=1.0 if float(f.get("entity_count", 0.0) or 0.0) <= 1.0 else 10.0),
        "number_count": _norm01(f.get("number_count", 0.0), denom=1.0 if float(f.get("number_count", 0.0) or 0.0) <= 1.0 else 10.0),
        "trend_velocity": _norm01(f.get("trend_velocity", 0.0), denom=1.0 if float(f.get("trend_velocity", 0.0) or 0.0) <= 1.0 else 10.0),
        "coverage": _norm01(f.get("coverage", f.get("cross_source_coverage", 0.0)), denom=1.0 if float(f.get("coverage", f.get("cross_source_coverage", 0.0)) or 0.0) <= 1.0 else 10.0),
        "cluster_size": _norm01(f.get("cluster_size", 0.0), denom=1.0 if float(f.get("cluster_size", 0.0) or 0.0) <= 1.0 else 10.0),
        "duplicate_flag": 1.0 if article.status.value == "double" or bool(article.double_of_article_id) else 0.0,
        "has_image": 1.0 if (article.image_url or article.generated_image_path) else 0.0,
        "content_short": content_short,
        "content_medium": content_medium,
        "content_long": content_long,
        "significance": _norm01(f.get("significance", score.significance if score else 0.0)),
        "relevance": _norm01(f.get("relevance", score.relevance if score else 0.0)),
        "virality": _norm01(f.get("virality", score.virality if score else 0.0)),
        "longevity": _norm01(f.get("longevity", score.longevity if score else 0.0)),
        "scale": _norm01(f.get("scale", score.scale if score else 0.0)),
        "novelty": _norm01(f.get("novelty", score.uniqueness if score else 0.0)),
        "business_it": _norm01(f.get("business_it", 0.0), denom=1.0 if float(f.get("business_it", 0.0) or 0.0) <= 1.0 else 10.0),
        "geek_penalty": _norm01(f.get("geek_penalty", 1.0), denom=1.0),
        "rule_score": float(max(0.0, min(10.0, rule_score))) / 10.0,
        "uncertainty": _norm01(getattr(score, "uncertainty", None), denom=1.0 if float(getattr(score, "uncertainty", 0.0) or 0.0) <= 1.0 else 10.0),
        "rank_by_rule_score": 0.0,
        "delta_to_best_rule": 0.0,
        "hour_sin": float((np.sin(2.0 * np.pi * (hour / 24.0)) + 1.0) / 2.0),
        "hour_cos": float((np.cos(2.0 * np.pi * (hour / 24.0)) + 1.0) / 2.0),
        "hours_since_published_norm": float(max(0.0, min(1.0, age_hours / 24.0))),
        "published_so_far_today_norm": 0.0,
        "hours_left_today_norm": float(max(0.0, min(1.0, hours_left / 23.0 if 23 else 0.0))),
    }
    content_type = str(article.content_type or f.get("content_type") or "other").strip().lower()
    use_cases = [str(x).strip().lower() for x in (f.get("use_cases") or []) if str(x).strip()]
    risk_flags = [str(x).strip().lower() for x in (f.get("risk_flags") or []) if str(x).strip()]
    text_body = " ".join([(article.title or ""), (article.subtitle or ""), (article.text or "")]).lower()
    out["practical_value"] = _norm01(getattr(article, "practical_value", 0), denom=10.0)
    out["audience_fit"] = _norm01(getattr(article, "audience_fit", 0), denom=10.0)
    out["actionability"] = _norm01(f.get("actionability", 0.0), denom=1.0 if float(f.get("actionability", 0.0) or 0.0) <= 1.0 else 10.0)
    for ct in ["tool", "case", "playbook", "hot", "trend"]:
        out[f"content_type_{ct}"] = 1.0 if content_type == ct else 0.0
    for uc in ["marketing", "sales", "support", "operations", "founder"]:
        out[f"use_case_{uc}"] = 1.0 if uc in use_cases else 0.0
    for rf in ["too_technical", "funding_hype", "infra_noise", "weak_source", "wow_but_risky"]:
        out[f"risk_{rf}"] = 1.0 if rf in risk_flags else 0.0
    out["text_length_norm"] = float(max(0.0, min(1.0, len(article.text or "") / 6000.0)))
    out["digit_count_norm"] = float(max(0.0, min(1.0, len(re.findall(r"\d", article.text or article.subtitle or article.title or "")) / 30.0)))
    out["contains_how_to"] = 1.0 if "how to" in text_body else 0.0
    out["contains_template"] = 1.0 if "template" in text_body else 0.0
    out["contains_prompt"] = 1.0 if "prompt" in text_body else 0.0
    out.update(_audience_pref_features_from_tags(audience_tags))
    tags = set(reason_tags or [])
    pos_tags = set(reason_positive_tags or [])
    neg_tags = set(reason_negative_tags or [])
    for tag in _TAGS:
        out[f"tag_{tag}"] = 1.0 if tag in tags else 0.0
        out[f"tag_pos_{tag}"] = 1.0 if tag in pos_tags else 0.0
        out[f"tag_neg_{tag}"] = 1.0 if tag in neg_tags else 0.0
    return out


def _candidate_ids_for_article(session, article: Article) -> list[int]:
    hour_bucket = article.selected_hour_bucket_utc
    if hour_bucket is None:
        # approximate from created/published time; align to UTC hour
        base_dt = article.published_at or article.created_at or datetime.utcnow()
        hour_bucket = base_dt.replace(minute=0, second=0, microsecond=0)
    latest = session.scalars(select(SelectionDecision).order_by(SelectionDecision.id.desc()).limit(50)).all()
    for d in latest:
        ids = [int(d.chosen_article_id)] + [int(x) for x in (d.rejected_article_ids or []) if str(x).isdigit() or isinstance(x, int)]
        if int(article.id) in ids:
            return ids
    return [int(article.id)]


def log_training_event(
    *,
    article_id: int,
    decision: str,
    label: int,
    reason_text: str | None = None,
    reason_tags: list[str] | None = None,
    user_id: int | None = None,
    override: bool = False,
    final_outcome: str | None = None,
) -> dict:
    decision = (decision or "").strip().lower()
    if decision not in {"publish", "top_pick", "hide", "delete", "defer", "skip"}:
        return {"ok": False, "error": "bad_decision"}
    tags = sorted(set([t for t in (reason_tags or []) if t]))
    with session_scope() as session:
        _ensure_reason_tag_catalog(session)
        article = session.get(Article, int(article_id))
        if not article:
            return {"ok": False, "error": "article_not_found"}
        cls = _classify_reason_tags_with_llm(
            session,
            reason_text=reason_text or "",
            decision=decision,
            article=article,
            allow_new_tags=True,
        )
        if not tags:
            tags = list(cls.get("tags") or [])
        pos_tags = [t for t in (cls.get("positive_tags") or []) if t]
        neg_tags = [t for t in (cls.get("negative_tags") or []) if t]
        sentiment = str(cls.get("sentiment") or "neutral")
        # Telegram callback user IDs are external IDs and usually do not match local `users.id`.
        # Keep FK integrity by storing only a valid local user id.
        local_user_id: int | None = None
        try:
            if user_id is not None and session.get(User, int(user_id)):
                local_user_id = int(user_id)
        except Exception:
            local_user_id = None
        score = session.get(Score, int(article_id))
        audience_desc, audience_tags = _latest_workspace_audience(session, user_id=local_user_id)
        _ = audience_desc  # reserved for future use in feature engineering
        features = _feature_snapshot(
            article,
            score,
            tags,
            audience_tags=audience_tags,
            reason_positive_tags=pos_tags,
            reason_negative_tags=neg_tags,
        )
        candidate_ids = _candidate_ids_for_article(session, article)

        # enrich context-dependent fields using current candidate set
        if candidate_ids:
            score_rows = session.scalars(select(Score).where(Score.article_id.in_(candidate_ids))).all()
            rule_scores = sorted([float(s.final_score or 0.0) for s in score_rows], reverse=True)
            current_rule = float(score.final_score or 0.0) if score else 0.0
            if rule_scores:
                best = rule_scores[0]
                features["delta_to_best_rule"] = float(max(0.0, min(1.0, (best - current_rule) / 10.0)))
                # 1-based rank normalized to [0..1], better rank => closer to 1
                try:
                    rank = 1 + sum(1 for rs in rule_scores if rs > current_rule)
                except Exception:
                    rank = len(rule_scores)
                features["rank_by_rule_score"] = float(max(0.0, min(1.0, 1.0 - ((rank - 1) / max(1, len(rule_scores) - 1)))))

        # daily context for "best of bad" behavior
        day_start = (article.created_at or datetime.utcnow()).replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        published_count = session.scalar(
            select(func.count()).select_from(Article).where(
                Article.status == ArticleStatus.PUBLISHED,
                Article.updated_at >= day_start,
                Article.updated_at < day_end,
            )
        ) or 0
        features["published_so_far_today_norm"] = float(max(0.0, min(1.0, float(published_count) / 5.0)))

        # ML snapshot at decision
        ml_score = None
        ml_meta = predict_editor_choice_prob(features)
        if ml_meta.get("ok"):
            ml_score = float(ml_meta.get("prob") or 0.0)

        published_time = article.published_at or article.created_at
        event_time = datetime.utcnow()
        delay_minutes = None
        if published_time:
            try:
                delay_minutes = max(0, int((event_time - published_time).total_seconds() // 60))
            except Exception:
                delay_minutes = None
        hour_bucket = (article.selected_hour_bucket_utc or (published_time or event_time).replace(minute=0, second=0, microsecond=0))

        rec = TrainingEvent(
            user_id=local_user_id,
            article_id=int(article_id),
            decision=decision,
            label=int(1 if label else 0),
            hour_bucket=hour_bucket,
            candidate_set_ids=[int(x) for x in candidate_ids],
            features_json=features,
            reason_text=(reason_text or "").strip() or None,
            reason_tags=tags or None,
            reason_positive_tags=pos_tags or None,
            reason_negative_tags=neg_tags or None,
            reason_sentiment=sentiment,
            rule_score=float(score.final_score or 0.0) if score else None,
            ml_score_at_decision=ml_score,
            model_version=(ml_meta.get("version") if ml_meta.get("ok") else None),
            override=bool(override),
            event_time=event_time,
            article_published_at=published_time,
            delay_minutes=delay_minutes,
            final_outcome=final_outcome,
        )
        session.add(rec)
        session.flush()
        return {
            "ok": True,
            "id": int(rec.id),
            "reason_tags": tags,
            "reason_positive_tags": pos_tags,
            "reason_negative_tags": neg_tags,
            "reason_sentiment": sentiment,
            "model_version": rec.model_version,
        }


def rebuild_preference_profile(min_feedback: int = 20) -> dict:
    with session_scope() as session:
        feedbacks = session.scalars(select(EditorFeedback).order_by(EditorFeedback.created_at.asc())).all()
        deletion_logs = session.scalars(
            select(AuditLog)
            .where(AuditLog.action == "article_delete_feedback")
            .order_by(AuditLog.created_at.asc())
        ).all()

    textual_feedback: list[str] = [f"CHOSEN: {f.explanation_text}" for f in feedbacks]
    for log in deletion_logs:
        payload = log.payload or {}
        reason = str(payload.get("reason") or "").strip()
        title = str(payload.get("title") or "").strip()
        if reason:
            textual_feedback.append(f"DELETED: {reason}" + (f" | title: {title}" if title else ""))

    if len(textual_feedback) < min_feedback:
        return {"ok": False, "reason": f"need_at_least_{min_feedback}_feedback_items"}

    joined = "\n".join(f"- {x}" for x in textual_feedback[-800:])
    prompt = f"""
Ниже объяснения редактора, почему он выбирал новости, и почему удалял новости после ревью.
Сформируй preference profile для авто-выбора.
Верни текст в виде коротких правил (8-20 пунктов), на английском, без воды.

{joined}
"""

    if not settings.openrouter_api_key:
        profile_text = "- Prefer high significance and relevance\n- Avoid low trust sources"
    else:
        client = get_client()
        resp = client.chat.completions.create(
            model=settings.llm_text_model,
            messages=[
                {"role": "system", "content": "Extract editorial preference rules."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        track_usage_from_response(resp, operation="preference.rebuild_profile", model=settings.llm_text_model, kind="chat")
        profile_text = (resp.choices[0].message.content or "").strip()

    with session_scope() as session:
        for p in session.scalars(select(PreferenceProfile).where(PreferenceProfile.active.is_(True))).all():
            p.active = False
        session.add(PreferenceProfile(profile_text=profile_text, active=True))

    return {
        "ok": True,
        "profile_length": len(profile_text),
        "chosen_feedback_items": len(feedbacks),
        "deleted_feedback_items": len([x for x in deletion_logs if (x.payload or {}).get("reason")]),
    }


def get_active_profile() -> str:
    with session_scope() as session:
        profile = session.scalars(
            select(PreferenceProfile)
            .where(PreferenceProfile.active.is_(True))
            .order_by(PreferenceProfile.id.desc())
            .limit(1)
        ).first()
    return profile.profile_text if profile else ""


def save_selection_decision(
    chosen_article_id: int,
    rejected_article_ids: list[int],
    decision_mode: DecisionMode,
    confidence: float | None,
    candidates: list[dict] | None = None,
    selector_kind: str | None = None,
) -> int:
    with session_scope() as session:
        rec = SelectionDecision(
            chosen_article_id=chosen_article_id,
            rejected_article_ids=rejected_article_ids,
            decision_mode=decision_mode,
            confidence=confidence,
            candidates=candidates,
            selector_kind=((selector_kind or "").strip() or None),
        )
        session.add(rec)
        session.flush()
        return rec.id


def build_ranking_dataset(days: int = 14) -> dict:
    cutoff = datetime.utcnow().timestamp() - days * 24 * 3600
    batch_id = stable_hash(f"rank-{datetime.utcnow().isoformat()}")[:12]
    created = 0

    with session_scope() as session:
        decisions = session.scalars(select(SelectionDecision).order_by(SelectionDecision.created_at.asc())).all()

        for d in decisions:
            if d.created_at.timestamp() < cutoff:
                continue
            ids = [d.chosen_article_id] + [int(x) for x in (d.rejected_article_ids or [])]
            for aid in ids:
                score = session.get(Score, aid)
                article = session.get(Article, aid)
                if not score or not article:
                    continue
                label = 1 if aid == d.chosen_article_id else 0
                f = score.features or {}
                feats = {
                    "freshness": float(f.get("freshness", score.freshness / 10.0)),
                    "source_priority": float(f.get("source_priority", 0.0)),
                    "entity_count": float(f.get("entity_count", 0.0)),
                    "number_count": float(f.get("number_count", 0.0)),
                    "trend_velocity": float(f.get("trend_velocity", 0.0)),
                    "coverage": float(f.get("coverage", 0.0)),
                    "significance": float(f.get("significance", score.significance / 10.0)),
                    "relevance": float(f.get("relevance", score.relevance / 10.0)),
                    "virality": float(f.get("virality", score.virality / 10.0)),
                    "longevity": float(f.get("longevity", score.longevity / 10.0)),
                    "scale": float(f.get("scale", score.scale / 10.0)),
                    "novelty": float(f.get("novelty", score.uniqueness / 10.0)),
                    "base_final_score": score.final_score,
                    "hour": article.created_at.hour,
                    "dow": article.created_at.weekday(),
                }
                session.add(
                    RankingExample(
                        article_id=aid,
                        batch_id=batch_id,
                        context_hour=article.created_at.hour,
                        context_day_of_week=article.created_at.weekday(),
                        topic=(article.tags[0] if isinstance(article.tags, list) and article.tags else None),
                        label=label,
                        features=feats,
                    )
                )
                created += 1

    return {"ok": True, "batch_id": batch_id, "created": created}


def train_ranking_model(batch_id: str) -> dict:
    with session_scope() as session:
        rows = session.scalars(select(RankingExample).where(RankingExample.batch_id == batch_id)).all()

    if len(rows) < 20:
        return {"ok": False, "reason": "not_enough_examples"}

    feature_names = [
        "freshness",
        "source_priority",
        "entity_count",
        "number_count",
        "trend_velocity",
        "coverage",
        "significance",
        "relevance",
        "virality",
        "longevity",
        "scale",
        "novelty",
        "base_final_score",
        "hour",
        "dow",
    ]

    x = np.array([[float(r.features.get(k, 0.0)) for k in feature_names] for r in rows], dtype=float)
    y = np.array([int(r.label) for r in rows], dtype=int)

    model = LogisticRegression(max_iter=1000)
    model.fit(x, y)
    probs = model.predict_proba(x)[:, 1]
    auc = float(roc_auc_score(y, probs)) if len(np.unique(y)) > 1 else 0.5

    artifact = {
        "feature_names": feature_names,
        "coef": model.coef_[0].tolist(),
        "intercept": float(model.intercept_[0]),
        "batch_id": batch_id,
        "auc_train": auc,
    }
    version = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    path = MODEL_DIR / f"ranking_{version}.json"
    path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")

    with session_scope() as session:
        for m in session.scalars(select(ModelArtifact).where(ModelArtifact.name == "ranking", ModelArtifact.active.is_(True))).all():
            m.active = False
        session.add(
            ModelArtifact(
                name="ranking",
                version=version,
                artifact_path=str(path),
                metrics={"auc_train": auc, "n": len(rows)},
                active=True,
            )
        )

    return {"ok": True, "version": version, "auc_train": auc, "n": len(rows)}


def get_active_ranking_artifact() -> dict | None:
    with session_scope() as session:
        model = session.scalars(
            select(ModelArtifact)
            .where(ModelArtifact.name == "ranking", ModelArtifact.active.is_(True))
            .order_by(ModelArtifact.id.desc())
            .limit(1)
        ).first()
    if not model:
        return None
    path = Path(model.artifact_path)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def build_editor_choice_dataset(days_back: int = 30) -> dict:
    cutoff = datetime.utcnow() - timedelta(days=max(1, int(days_back or 30)))
    with session_scope() as session:
        rows = session.scalars(
            select(TrainingEvent)
            .where(
                TrainingEvent.created_at >= cutoff,
                TrainingEvent.decision.in_(["publish", "hide", "delete", "defer"]),
            )
            .order_by(TrainingEvent.created_at.asc())
        ).all()

    if not rows:
        return {"ok": False, "reason": "no_training_events"}

    X: list[list[float]] = []
    y: list[int] = []
    meta: list[dict] = []
    for r in rows:
        feats = dict(r.features_json or {})
        # If reason tags were updated later, ensure one-hot is present.
        raw_tags = r.reason_tags or []
        if isinstance(raw_tags, str):
            raw_tags = [raw_tags] if raw_tags.strip() and raw_tags.strip().lower() not in {"null", "none"} else []
        raw_pos = r.reason_positive_tags or []
        if isinstance(raw_pos, str):
            raw_pos = [raw_pos] if raw_pos.strip() and raw_pos.strip().lower() not in {"null", "none"} else []
        raw_neg = r.reason_negative_tags or []
        if isinstance(raw_neg, str):
            raw_neg = [raw_neg] if raw_neg.strip() and raw_neg.strip().lower() not in {"null", "none"} else []
        union_tags = set([str(x) for x in raw_tags if str(x).strip()])
        pos_tags = set([str(x) for x in raw_pos if str(x).strip()])
        neg_tags = set([str(x) for x in raw_neg if str(x).strip()])
        for tag in _TAGS:
            feats.setdefault(f"tag_{tag}", 1.0 if tag in union_tags else 0.0)
            feats.setdefault(f"tag_pos_{tag}", 1.0 if tag in pos_tags else 0.0)
            feats.setdefault(f"tag_neg_{tag}", 1.0 if tag in neg_tags else 0.0)
        vec = [float(feats.get(k, 0.0) or 0.0) for k in EDITOR_CHOICE_FEATURES]
        X.append(vec)
        # label target for "quality/choose eventually": publish=1, others=0.
        y.append(int(r.label))
        meta.append(
            {
                "event_id": int(r.id),
                "created_at": r.created_at.isoformat(),
                "article_id": int(r.article_id),
                "decision": r.decision,
            }
        )
    return {"ok": True, "X": X, "y": y, "meta": meta, "n": len(rows)}


def build_practical_ranking_dataset(days_back: int = 28) -> dict:
    cutoff = datetime.utcnow() - timedelta(days=max(1, int(days_back or 28)))
    batch_id = stable_hash(f"practical-rank-{datetime.utcnow().isoformat()}")[:12]
    rank_map = {"publish": 3, "defer": 2, "delete": 1, "hide": 1}

    with session_scope() as session:
        rows = session.scalars(
            select(TrainingEvent)
            .where(
                TrainingEvent.created_at >= cutoff,
                TrainingEvent.decision.in_(["publish", "defer", "delete", "hide"]),
            )
            .order_by(TrainingEvent.created_at.asc())
        ).all()

    if len(rows) < 20:
        return {"ok": False, "reason": "not_enough_training_events", "n": len(rows)}

    by_day: dict[str, list[TrainingEvent]] = {}
    for r in rows:
        day_key = (r.created_at or datetime.utcnow()).strftime("%Y-%m-%d")
        by_day.setdefault(day_key, []).append(r)

    created = 0
    skipped_pairs = 0
    with session_scope() as session:
        for day_key, events in by_day.items():
            if len(events) < 2:
                continue
            for i, left in enumerate(events):
                left_rank = rank_map.get(str(left.decision or "").strip().lower(), 0)
                if left_rank <= 0:
                    continue
                left_feats = dict(left.features_json or {})
                for right in events[i + 1:]:
                    right_rank = rank_map.get(str(right.decision or "").strip().lower(), 0)
                    if right_rank <= 0 or right_rank == left_rank:
                        skipped_pairs += 1
                        continue
                    right_feats = dict(right.features_json or {})
                    better, worse = (left, right) if left_rank > right_rank else (right, left)
                    better_feats = left_feats if left_rank > right_rank else right_feats
                    worse_feats = right_feats if left_rank > right_rank else left_feats
                    delta = {k: float(better_feats.get(k, 0.0) or 0.0) - float(worse_feats.get(k, 0.0) or 0.0) for k in PRACTICAL_RANKER_FEATURES}
                    session.add(
                        RankingExample(
                            article_id=int(better.article_id),
                            batch_id=batch_id,
                            context_hour=int((better.created_at or datetime.utcnow()).hour),
                            context_day_of_week=int((better.created_at or datetime.utcnow()).weekday()),
                            topic=day_key,
                            label=1,
                            features=delta,
                        )
                    )
                    created += 1
    return {"ok": True, "batch_id": batch_id, "created": created, "days": len(by_day), "skipped_pairs": skipped_pairs}


def train_practical_ranking_model(days_back: int = 28, min_pairs: int = 40) -> dict:
    ds = build_practical_ranking_dataset(days_back=days_back)
    if not ds.get("ok"):
        return ds
    batch_id = str(ds.get("batch_id") or "")
    with session_scope() as session:
        rows = session.scalars(select(RankingExample).where(RankingExample.batch_id == batch_id).order_by(RankingExample.id.asc())).all()
    if len(rows) < int(min_pairs):
        return {"ok": False, "reason": "not_enough_pairs", "n": len(rows), "min_pairs": int(min_pairs)}

    # Time-based split by day encoded in topic.
    day_keys = sorted({str(r.topic or "") for r in rows if str(r.topic or "").strip()})
    if len(day_keys) < 3:
        return {"ok": False, "reason": "not_enough_days", "days": len(day_keys)}
    split_idx = max(1, int(len(day_keys) * 0.8))
    train_days = set(day_keys[:split_idx])
    val_days = set(day_keys[split_idx:])
    train_rows = [r for r in rows if str(r.topic or "") in train_days]
    val_rows = [r for r in rows if str(r.topic or "") in val_days]
    if len(train_rows) < int(min_pairs):
        return {"ok": False, "reason": "not_enough_train_pairs", "n_train": len(train_rows)}

    x_train = np.array([[float((r.features or {}).get(k, 0.0) or 0.0) for k in PRACTICAL_RANKER_FEATURES] for r in train_rows], dtype=float)
    y_train = np.array([int(r.label) for r in train_rows], dtype=int)
    x_val = np.array([[float((r.features or {}).get(k, 0.0) or 0.0) for k in PRACTICAL_RANKER_FEATURES] for r in val_rows], dtype=float) if val_rows else np.empty((0, len(PRACTICAL_RANKER_FEATURES)))
    y_val = np.array([int(r.label) for r in val_rows], dtype=int) if val_rows else np.empty((0,), dtype=int)

    model = LogisticRegression(max_iter=1000, penalty="l2", class_weight="balanced")
    model.fit(x_train, y_train)
    p_train = model.predict_proba(x_train)[:, 1]
    p_val = model.predict_proba(x_val)[:, 1] if len(x_val) else np.array([])
    auc_train = float(roc_auc_score(y_train, p_train)) if len(np.unique(y_train)) > 1 else 0.5
    auc_val = float(roc_auc_score(y_val, p_val)) if (len(y_val) and len(np.unique(y_val)) > 1) else None

    # Coarse validation metrics by day.
    val_grouped: dict[str, list[float]] = {}
    for idx, row in enumerate(val_rows):
        val_grouped.setdefault(str(row.topic or ""), []).append(float(p_val[idx]) if idx < len(p_val) else 0.0)
    precision_at_1 = float(sum(1 for vals in val_grouped.values() if vals and vals[0] >= 0.5) / len(val_grouped)) if val_grouped else None
    ndcg_at_5 = precision_at_1

    artifact = {
        "feature_names": PRACTICAL_RANKER_FEATURES,
        "coef": model.coef_[0].tolist(),
        "intercept": float(model.intercept_[0]),
        "trained_at": datetime.utcnow().isoformat(),
        "days_back": int(days_back),
        "metrics": {
            "roc_auc_train": auc_train,
            "roc_auc_val": auc_val,
            "precision_at_1": precision_at_1,
            "ndcg_at_5": ndcg_at_5,
        },
    }
    version = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    path = MODEL_DIR / f"practical_ranker_{version}.json"
    path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")

    with session_scope() as session:
        for m in session.scalars(select(ModelArtifact).where(ModelArtifact.name == PRACTICAL_RANKER_MODEL_NAME, ModelArtifact.active.is_(True))).all():
            m.active = False
        session.add(
            ModelArtifact(
                name=PRACTICAL_RANKER_MODEL_NAME,
                version=version,
                artifact_path=str(path),
                metrics=artifact["metrics"] | {"n_train": len(train_rows), "n_val": len(val_rows)},
                active=True,
            )
        )
    return {"ok": True, "version": version, **artifact["metrics"], "n_pairs": len(rows)}


def get_active_practical_ranking_artifact() -> dict | None:
    with session_scope() as session:
        row = session.scalars(
            select(ModelArtifact)
            .where(ModelArtifact.name == PRACTICAL_RANKER_MODEL_NAME, ModelArtifact.active.is_(True))
            .order_by(ModelArtifact.id.desc())
            .limit(1)
        ).first()
    if not row:
        return None
    p = Path(row.artifact_path)
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    data["_version"] = row.version
    return data


def predict_practical_ranking_prob(features: dict | None) -> dict:
    artifact = get_active_practical_ranking_artifact()
    if not artifact:
        return {"ok": False, "reason": "no_model"}
    names = artifact.get("feature_names") or []
    coef = artifact.get("coef") or []
    if not names or not coef or len(names) != len(coef):
        return {"ok": False, "reason": "bad_artifact"}
    feats = dict(features or {})
    x = np.array([float(feats.get(k, 0.0) or 0.0) for k in names], dtype=float)
    z = float(np.dot(x, np.array(coef, dtype=float)) + float(artifact.get("intercept") or 0.0))
    prob = _sigmoid(z)
    return {"ok": True, "prob": prob, "version": artifact.get("_version")}


def train_editor_choice_model(days_back: int = 30, min_samples: int = 40) -> dict:
    ds = build_editor_choice_dataset(days_back=days_back)
    if not ds.get("ok"):
        return ds
    X = np.array(ds["X"], dtype=float)
    y = np.array(ds["y"], dtype=int)
    n = int(len(y))
    if n < int(min_samples):
        return {"ok": False, "reason": "not_enough_samples", "n": n, "min_samples": int(min_samples)}
    if len(np.unique(y)) < 2:
        return {"ok": False, "reason": "need_both_classes", "n": n}

    split = max(1, min(n - 1, int(n * 0.8)))
    X_train, X_val = X[:split], X[split:]
    y_train, y_val = y[:split], y[split:]
    if len(np.unique(y_train)) < 2:
        return {"ok": False, "reason": "train_split_one_class", "n": n}

    model = LogisticRegression(max_iter=1000, class_weight="balanced")
    model.fit(X_train, y_train)

    p_train = model.predict_proba(X_train)[:, 1]
    p_val = model.predict_proba(X_val)[:, 1] if len(X_val) else np.array([])
    auc_train = float(roc_auc_score(y_train, p_train)) if len(np.unique(y_train)) > 1 else 0.5
    auc_val = float(roc_auc_score(y_val, p_val)) if (len(y_val) and len(np.unique(y_val)) > 1) else None

    # Simple precision@1 and ndcg@5 over hourly groups from meta timestamps
    meta = ds.get("meta") or []
    val_meta = meta[split:]
    grouped: dict[str, list[tuple[float, int]]] = {}
    for i, row in enumerate(val_meta):
        if i >= len(p_val):
            break
        key = str(row.get("created_at", ""))[:13]  # YYYY-MM-DDTHH
        grouped.setdefault(key, []).append((float(p_val[i]), int(y_val[i])))
    p1_hits = 0
    ndcg5_vals: list[float] = []
    for _, items in grouped.items():
        if not items:
            continue
        ranked = sorted(items, key=lambda x: x[0], reverse=True)
        p1_hits += 1 if ranked[0][1] == 1 else 0
        dcg = 0.0
        idcg = 0.0
        topk = ranked[:5]
        ideal = sorted([lbl for _, lbl in items], reverse=True)[:5]
        for idx, (_, lbl) in enumerate(topk, start=1):
            dcg += (float(lbl) / np.log2(idx + 1))
        for idx, lbl in enumerate(ideal, start=1):
            idcg += (float(lbl) / np.log2(idx + 1))
        ndcg5_vals.append(float(dcg / idcg) if idcg > 0 else 0.0)
    precision_at_1 = (p1_hits / len(grouped)) if grouped else None
    ndcg5 = (float(np.mean(ndcg5_vals)) if ndcg5_vals else None)

    artifact = {
        "feature_names": EDITOR_CHOICE_FEATURES,
        "coef": model.coef_[0].tolist(),
        "intercept": float(model.intercept_[0]),
        "trained_at": datetime.utcnow().isoformat(),
        "days_back": int(days_back),
        "n_train": int(len(y_train)),
        "n_val": int(len(y_val)),
        "metrics": {
            "roc_auc_train": auc_train,
            "roc_auc_val": auc_val,
            "precision_at_1": precision_at_1,
            "ndcg_at_5": ndcg5,
        },
    }
    version = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    path = MODEL_DIR / f"editor_choice_{version}.json"
    path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2), encoding="utf-8")

    with session_scope() as session:
        for m in session.scalars(
            select(ModelArtifact).where(ModelArtifact.name == EDITOR_CHOICE_MODEL_NAME, ModelArtifact.active.is_(True))
        ).all():
            m.active = False
        session.add(
            ModelArtifact(
                name=EDITOR_CHOICE_MODEL_NAME,
                version=version,
                artifact_path=str(path),
                metrics=artifact["metrics"] | {"n_train": len(y_train), "n_val": len(y_val)},
                active=True,
            )
        )
    return {"ok": True, "version": version, **artifact["metrics"], "n": n}


def get_active_editor_choice_artifact() -> dict | None:
    with session_scope() as session:
        row = session.scalars(
            select(ModelArtifact)
            .where(ModelArtifact.name == EDITOR_CHOICE_MODEL_NAME, ModelArtifact.active.is_(True))
            .order_by(ModelArtifact.id.desc())
            .limit(1)
        ).first()
    if not row:
        return None
    p = Path(row.artifact_path)
    if not p.exists():
        return None
    data = json.loads(p.read_text(encoding="utf-8"))
    data["_version"] = row.version
    return data


def predict_editor_choice_prob(features: dict | None) -> dict:
    artifact = get_active_editor_choice_artifact()
    if not artifact:
        return {"ok": False, "reason": "no_model"}
    names = artifact.get("feature_names") or []
    coef = artifact.get("coef") or []
    if not names or not coef or len(names) != len(coef):
        return {"ok": False, "reason": "bad_artifact"}
    feats = dict(features or {})
    x = np.array([float(feats.get(k, 0.0) or 0.0) for k in names], dtype=float)
    z = float(np.dot(x, np.array(coef, dtype=float)) + float(artifact.get("intercept") or 0.0))
    prob = _sigmoid(z)
    uncertainty = float(1.0 - abs(prob - 0.5) * 2.0)
    return {"ok": True, "prob": prob, "uncertainty": uncertainty, "version": artifact.get("_version")}


def blended_editor_score(rule_score_0_10: float, features: dict | None) -> dict:
    """
    Blend current rule score with editor-choice probability.
    Weight is runtime-configurable and can be increased gradually.
    """
    rule_norm = float(max(0.0, min(10.0, float(rule_score_0_10 or 0.0)))) / 10.0
    ml = predict_editor_choice_prob(features)
    if not ml.get("ok"):
        return {"ok": False, "final": rule_norm, "rule": rule_norm}
    w = float(max(0.0, min(0.8, get_runtime_float("ml_editor_choice_weight", default=0.1))))
    final = ((1.0 - w) * rule_norm) + (w * float(ml["prob"]))
    return {
        "ok": True,
        "final": float(max(0.0, min(1.0, final))),
        "rule": rule_norm,
        "ml_prob": float(ml["prob"]),
        "uncertainty": float(ml.get("uncertainty") or 0.0),
        "weight": w,
        "version": ml.get("version"),
    }


def detect_preference_drift(window: int = 200, threshold: float = 0.22) -> dict:
    with session_scope() as session:
        fb = session.scalars(select(EditorFeedback).order_by(EditorFeedback.created_at.desc()).limit(window)).all()

    if len(fb) < 30:
        return {"ok": False, "reason": "not_enough_feedback"}

    conf = [f.confidence for f in fb if isinstance(f.confidence, int)]
    if len(conf) < 10:
        return {"ok": False, "reason": "not_enough_confidence_values"}

    split = len(conf) // 2
    old_avg = float(np.mean(conf[split:]))
    new_avg = float(np.mean(conf[:split]))
    delta = abs(new_avg - old_avg) / 10.0
    drifted = delta >= threshold

    with session_scope() as session:
        session.add(
            DriftMetric(
                metric_name="editor_confidence_shift",
                value=delta,
                threshold=threshold,
                drifted=drifted,
            )
        )

    return {"ok": True, "drifted": drifted, "delta": delta, "old_avg": old_avg, "new_avg": new_avg}


def backfill_training_and_restore_unreasoned_archived(
    *,
    restore_status: ArticleStatus = ArticleStatus.INBOX,
    max_articles: int = 50000,
) -> dict:
    """
    1) Backfill historical delete/hide reasons from archived articles (+ audit logs fallback) into training_events.
    2) Restore archived/rejected articles that have no deletion/hide reason.
    """
    scanned = 0
    backfilled = 0
    restored = 0
    already_present = 0
    published_backfilled = 0
    retagged_existing = 0
    errors = 0

    with session_scope() as session:
        articles = session.scalars(
            select(Article)
            .where(Article.status.in_([ArticleStatus.ARCHIVED, ArticleStatus.REJECTED]))
            .order_by(Article.updated_at.desc())
            .limit(max_articles)
        ).all()

        # Build audit fallback map for delete reasons (latest wins).
        audit_reason_by_article: dict[int, str] = {}
        logs = session.scalars(
            select(AuditLog)
            .where(AuditLog.action == "article_delete_feedback")
            .order_by(AuditLog.created_at.desc())
            .limit(max_articles * 2)
        ).all()
        for log in logs:
            try:
                aid = int(str(log.entity_id or "0"))
            except Exception:
                continue
            if aid <= 0 or aid in audit_reason_by_article:
                continue
            payload = log.payload or {}
            reason = str(payload.get("reason") or "").strip()
            if reason:
                audit_reason_by_article[aid] = reason

        for a in articles:
            scanned += 1
            kind = str(a.archived_kind or "").strip().lower()
            reason = str(a.archived_reason or "").strip()
            if not reason and kind == "delete":
                reason = audit_reason_by_article.get(int(a.id), "").strip()

            # Keep archived with reason; backfill into training_events.
            if reason:
                decision = "delete" if kind == "delete" else "hide"
                exists = session.scalars(
                    select(TrainingEvent.id)
                    .where(
                        TrainingEvent.article_id == int(a.id),
                        TrainingEvent.decision == decision,
                        TrainingEvent.reason_text == reason,
                    )
                    .limit(1)
                ).first()
                if exists:
                    row = session.get(TrainingEvent, int(exists))
                    if row and reason:
                        cls = _guess_reason_tag_polarity(reason, decision=decision)
                        changed = False
                        if (not row.reason_tags) and cls.get("tags"):
                            row.reason_tags = cls.get("tags") or None
                            changed = True
                        if (not row.reason_positive_tags) and cls.get("positive_tags"):
                            row.reason_positive_tags = cls.get("positive_tags") or None
                            changed = True
                        if (not row.reason_negative_tags) and cls.get("negative_tags"):
                            row.reason_negative_tags = cls.get("negative_tags") or None
                            changed = True
                        if not (row.reason_sentiment or "").strip():
                            row.reason_sentiment = str(cls.get("sentiment") or "neutral")
                            changed = True
                        if changed:
                            retagged_existing += 1
                    already_present += 1
                    continue
                try:
                    score = session.get(Score, int(a.id))
                    _aud_desc, aud_tags = _latest_workspace_audience(session, user_id=None)
                    cls = _guess_reason_tag_polarity(reason, decision=decision)
                    features = _feature_snapshot(
                        a,
                        score,
                        cls.get("tags"),
                        audience_tags=aud_tags,
                        reason_positive_tags=cls.get("positive_tags"),
                        reason_negative_tags=cls.get("negative_tags"),
                    )
                    ml_meta = predict_editor_choice_prob(features)
                    published_time = a.published_at or a.created_at
                    event_time = a.archived_at or a.updated_at or datetime.utcnow()
                    delay_minutes = None
                    if published_time:
                        try:
                            delay_minutes = max(0, int((event_time - published_time).total_seconds() // 60))
                        except Exception:
                            delay_minutes = None
                    session.add(
                        TrainingEvent(
                            user_id=None,
                            article_id=int(a.id),
                            decision=decision,
                            label=0,
                            hour_bucket=(a.selected_hour_bucket_utc or (published_time or event_time).replace(minute=0, second=0, microsecond=0)),
                            candidate_set_ids=[int(a.id)],
                            features_json=features,
                            reason_text=reason,
                            reason_tags=cls.get("tags") or None,
                            reason_positive_tags=cls.get("positive_tags") or None,
                            reason_negative_tags=cls.get("negative_tags") or None,
                            reason_sentiment=str(cls.get("sentiment") or "negative"),
                            rule_score=(float(score.final_score or 0.0) if score else None),
                            ml_score_at_decision=(float(ml_meta.get("prob")) if ml_meta.get("ok") else None),
                            model_version=(ml_meta.get("version") if ml_meta.get("ok") else None),
                            override=False,
                            event_time=event_time,
                            article_published_at=published_time,
                            delay_minutes=delay_minutes,
                            final_outcome=("deleted" if decision == "delete" else "hidden"),
                            created_at=event_time,
                        )
                    )
                    backfilled += 1
                except Exception:
                    errors += 1
                continue

            # No reason: restore back to manual queue.
            a.status = restore_status
            a.archived_kind = None
            a.archived_reason = None
            a.archived_at = None
            a.updated_at = datetime.utcnow()
            restored += 1

        # Backfill positive examples from published articles with editor reasons.
        published_rows = session.scalars(
            select(Article)
            .where(Article.status == ArticleStatus.PUBLISHED)
            .order_by(Article.updated_at.desc())
            .limit(max_articles)
        ).all()
        for a in published_rows:
            exists_pub = session.scalars(
                select(TrainingEvent.id)
                .where(
                    TrainingEvent.article_id == int(a.id),
                    TrainingEvent.decision == "publish",
                )
                .limit(1)
            ).first()
            if exists_pub:
                continue

            # Prefer explicit editor feedback, fallback to telegram review job reason.
            fb = session.scalars(
                select(EditorFeedback)
                .where(EditorFeedback.article_id == int(a.id))
                .order_by(EditorFeedback.created_at.desc())
                .limit(1)
            ).first()
            tj = session.scalars(
                select(TelegramReviewJob)
                .where(TelegramReviewJob.article_id == int(a.id))
                .order_by(TelegramReviewJob.updated_at.desc(), TelegramReviewJob.id.desc())
                .limit(1)
            ).first()
            reason = ""
            if fb and (fb.explanation_text or "").strip():
                reason = str(fb.explanation_text or "").strip()
            elif tj and (tj.decision_reason or "").strip():
                reason = str(tj.decision_reason or "").strip()

            # If no reason at all, skip for now (user wants reason-based learning).
            if not reason:
                continue

            try:
                score = session.get(Score, int(a.id))
                cls = _guess_reason_tag_polarity(reason, decision="publish")
                tags = list(cls.get("tags") or [])
                _aud_desc, aud_tags = _latest_workspace_audience(session, user_id=None)
                features = _feature_snapshot(
                    a,
                    score,
                    tags,
                    audience_tags=aud_tags,
                    reason_positive_tags=cls.get("positive_tags"),
                    reason_negative_tags=cls.get("negative_tags"),
                )
                ml_meta = predict_editor_choice_prob(features)
                published_time = a.published_at or a.created_at
                event_time = a.updated_at or published_time or datetime.utcnow()
                delay_minutes = None
                if published_time:
                    try:
                        delay_minutes = max(0, int((event_time - published_time).total_seconds() // 60))
                    except Exception:
                        delay_minutes = None
                session.add(
                    TrainingEvent(
                        user_id=None,
                        article_id=int(a.id),
                        decision="publish",
                        label=1,
                        hour_bucket=(a.selected_hour_bucket_utc or (published_time or event_time).replace(minute=0, second=0, microsecond=0)),
                        candidate_set_ids=[int(a.id)],
                        features_json=features,
                        reason_text=reason,
                        reason_tags=tags or None,
                        reason_positive_tags=cls.get("positive_tags") or None,
                        reason_negative_tags=cls.get("negative_tags") or None,
                        reason_sentiment=str(cls.get("sentiment") or "positive"),
                        rule_score=(float(score.final_score or 0.0) if score else None),
                        ml_score_at_decision=(float(ml_meta.get("prob")) if ml_meta.get("ok") else None),
                        model_version=(ml_meta.get("version") if ml_meta.get("ok") else None),
                        override=False,
                        event_time=event_time,
                        article_published_at=published_time,
                        delay_minutes=delay_minutes,
                        final_outcome="published",
                        created_at=event_time,
                    )
                )
                published_backfilled += 1
            except Exception:
                errors += 1

    return {
        "ok": True,
        "scanned_archived": scanned,
        "backfilled_training_events": backfilled,
        "published_backfilled": published_backfilled,
        "already_present": already_present,
        "retagged_existing": retagged_existing,
        "restored_without_reason": restored,
        "errors": errors,
        "restore_status": str(restore_status.value if isinstance(restore_status, ArticleStatus) else restore_status),
    }


def reretag_training_event_reasons(limit: int = 50000, overwrite: bool = False) -> dict:
    """
    Recompute reason_tags (+positive/+negative split) from reason_text for existing training_events.
    By default updates only rows with empty/null tags.
    """
    scanned = 0
    updated = 0
    skipped_no_text = 0
    unchanged = 0
    with session_scope() as session:
        rows = session.scalars(
            select(TrainingEvent)
            .order_by(TrainingEvent.id.asc())
            .limit(max(1, int(limit)))
        ).all()
        for row in rows:
            scanned += 1
            text = (row.reason_text or "").strip()
            if not text:
                skipped_no_text += 1
                continue
            old_tags = list(row.reason_tags or [])
            has_split = bool((row.reason_positive_tags or []) or (row.reason_negative_tags or []) or (row.reason_sentiment or "").strip())
            if old_tags and has_split and not overwrite:
                continue
            cls = _guess_reason_tag_polarity(text, decision=row.decision)
            new_tags = list(cls.get("tags") or [])
            if (
                new_tags == old_tags
                and list(row.reason_positive_tags or []) == list(cls.get("positive_tags") or [])
                and list(row.reason_negative_tags or []) == list(cls.get("negative_tags") or [])
                and (row.reason_sentiment or "neutral") == str(cls.get("sentiment") or "neutral")
            ):
                unchanged += 1
                continue
            row.reason_tags = new_tags or None
            row.reason_positive_tags = cls.get("positive_tags") or None
            row.reason_negative_tags = cls.get("negative_tags") or None
            row.reason_sentiment = str(cls.get("sentiment") or "neutral")
            updated += 1
    return {
        "ok": True,
        "scanned": scanned,
        "updated": updated,
        "unchanged": unchanged,
        "skipped_no_text": skipped_no_text,
        "overwrite": bool(overwrite),
    }


def infer_audience_tags_for_workspaces(limit: int = 100, overwrite: bool = False) -> dict:
    with session_scope() as session:
        _ensure_reason_tag_catalog(session)
        rows = session.scalars(select(UserWorkspace).order_by(UserWorkspace.updated_at.desc()).limit(max(1, int(limit)))).all()
        updated = 0
        scanned = 0
        skipped = 0
        for ws in rows:
            scanned += 1
            desc = _normalize_reason_text(ws.audience_description)
            if not desc:
                skipped += 1
                continue
            if ws.audience_tags and not overwrite:
                continue
            tags = []
            low = desc.lower()
            if any(x in low for x in ["массов", "обывател", "широк", "для всех"]):
                tags.append("mass_audience")
            if any(x in low for x in ["бизнес", "предприним", "компан", "рынок", "проект"]):
                tags.append("business")
            if any(x in low for x in ["будущ", "тренд", "что происходит", "стратег"]):
                tags.append("future")
            if any(x in low for x in ["хайп", "вау", "резонанс", "горяч"]):
                tags.append("hype")
            if any(x in low for x in ["безопас", "мошен", "риск"]):
                tags.append("security")
            if any(x in low for x in ["практич", "инструмент", "как использовать", "польза"]):
                tags.append("practical")
            if any(x in low for x in ["рф", "росси", "русск", "нашей аудитории"]):
                tags.append("ru_relevance")
            if any(x in low for x in ["технолог", "ии", "ai"]):
                tags.append("technology")

            # LLM enrich if available (optional)
            try:
                if settings.openrouter_api_key or get_active_profile() is not None:
                    client = get_client()
                    resp = client.chat.completions.create(
                        model=settings.llm_text_model,
                        response_format={"type": "json_object"},
                        messages=[
                            {"role": "system", "content": "Classify channel audience description into tags. Return JSON only."},
                            {
                                "role": "user",
                                "content": (
                                    "Allowed tags: " + ", ".join(_AUDIENCE_BASE_TAGS) + "\n"
                                    "Return JSON: {\"tags\": [..]}\n"
                                    f"Description: {desc}"
                                ),
                            },
                        ],
                        temperature=0.1,
                    )
                    track_usage_from_response(resp, operation="preference.infer_audience_tags", model=settings.llm_text_model, kind="chat")
                    data = json.loads(resp.choices[0].message.content or "{}")
                    llm_tags = [str(x).strip() for x in (data.get("tags") or []) if str(x).strip() in _AUDIENCE_BASE_TAGS]
                    tags.extend(llm_tags)
            except Exception:
                pass

            new_tags = sorted(set(tags))
            if new_tags != sorted(set(ws.audience_tags or [])):
                ws.audience_tags = new_tags or None
                ws.updated_at = datetime.utcnow()
                updated += 1
        return {"ok": True, "scanned": scanned, "updated": updated, "skipped": skipped}


def reclassify_training_reasons_llm(limit: int = 300, only_null: bool = True, allow_new_tags: bool = True) -> dict:
    # Uses free-text reason + optional audience context and returns multi-tags.
    with session_scope() as session:
        _ensure_reason_tag_catalog(session)
        tag_slugs = _get_active_reason_tag_slugs(session)
        audience_desc, audience_tags = _latest_workspace_audience(session)
        # JSON columns may contain JSON null (not SQL NULL), so we fetch a wider set and
        # filter "missing tags" in Python.
        scan_limit = max(1, int(limit)) if not only_null else max(1000, int(limit) * 5)
        q = select(TrainingEvent).where(TrainingEvent.reason_text.is_not(None))
        rows_all = session.scalars(q.order_by(TrainingEvent.id.asc()).limit(scan_limit)).all()
        rows: list[TrainingEvent] = []
        for r in rows_all:
            if only_null:
                rv = r.reason_tags
                missing_union = (
                    rv is None
                    or (isinstance(rv, list) and len(rv) == 0)
                    or (isinstance(rv, str) and rv.strip().lower() in {"", "null", "none"})
                )
                missing_split = (
                    not (r.reason_positive_tags or [])
                    and not (r.reason_negative_tags or [])
                    and not str(r.reason_sentiment or "").strip()
                )
                if not (missing_union or missing_split):
                    continue
            rows.append(r)
            if len(rows) >= max(1, int(limit)):
                break

        if not rows:
            return {"ok": True, "processed": 0, "updated": 0, "created_tags": 0, "scanned": len(rows_all)}

        if not settings.openrouter_api_key:
            # Fallback to improved heuristic if no LLM key.
            updated = 0
            created_tags = 0
            for r in rows:
                reason = _normalize_reason_text(r.reason_text)
                cls = _guess_reason_tag_polarity(reason, decision=r.decision)
                tags = list(cls.get("tags") or [])
                if (
                    tags != list(r.reason_tags or [])
                    or list(r.reason_positive_tags or []) != list(cls.get("positive_tags") or [])
                    or list(r.reason_negative_tags or []) != list(cls.get("negative_tags") or [])
                    or (r.reason_sentiment or "neutral") != str(cls.get("sentiment") or "neutral")
                ):
                    r.reason_tags = tags or None
                    r.reason_positive_tags = cls.get("positive_tags") or None
                    r.reason_negative_tags = cls.get("negative_tags") or None
                    r.reason_sentiment = str(cls.get("sentiment") or "neutral")
                    updated += 1
            return {
                "ok": True,
                "processed": len(rows),
                "updated": updated,
                "created_tags": created_tags,
                "mode": "heuristic_fallback",
                "scanned": len(rows_all),
            }

        client = get_client()
        processed = 0
        updated = 0
        created_tags = 0
        for r in rows:
            processed += 1
            if processed % 25 == 0:
                try:
                    print("[reclassify-reasons-llm]", {"processed": processed, "limit": len(rows)}, flush=True)
                except Exception:
                    pass
            reason = _normalize_reason_text(r.reason_text)
            if not reason:
                continue
            article = session.get(Article, int(r.article_id))
            title = str((article.ru_title or article.title) if article else "").strip()
            subtitle = str((article.ru_summary or article.subtitle) if article else "").strip()[:500]
            try:
                resp = client.chat.completions.create(
                    model=settings.llm_text_model,
                    response_format={"type": "json_object"},
                    messages=[
                        {"role": "system", "content": "Extract editorial rejection/publish reason tags. Multi-label. Return compact JSON only."},
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "allowed_tags": tag_slugs,
                                    "audience_tags": audience_tags,
                                    "audience_description": audience_desc[:800],
                                    "decision": r.decision,
                                    "title": title[:300],
                                    "summary": subtitle,
                                    "reason_text": reason[:1000],
                                    "instructions": {
                                        "multi_label": True,
                                        "can_suggest_new_tags": bool(allow_new_tags),
                                        "output": {
                                            "positive_tags": ["slug1"],
                                            "negative_tags": ["slug2"],
                                            "tags": ["slug1", "slug2"],
                                            "reason_sentiment": "positive|negative|mixed|neutral",
                                            "new_tags": [{"slug": "new_slug", "title_ru": "Русское название"}],
                                            "confidence": 0.0,
                                        },
                                    },
                                },
                                ensure_ascii=False,
                            ),
                        },
                    ],
                    temperature=0.1,
                )
                track_usage_from_response(resp, operation="preference.reclassify_reasons_llm", model=settings.llm_text_model, kind="chat")
                data = json.loads(resp.choices[0].message.content or "{}")
            except Exception:
                data = {}

            llm_pos = [str(x).strip() for x in (data.get("positive_tags") or []) if str(x).strip()]
            llm_neg = [str(x).strip() for x in (data.get("negative_tags") or []) if str(x).strip()]
            llm_all = [str(x).strip() for x in (data.get("tags") or []) if str(x).strip()]
            base_cls = _guess_reason_tag_polarity(reason, decision=r.decision)
            pos_tags = sorted(set(llm_pos + list(base_cls.get("positive_tags") or [])))
            neg_tags = sorted(set(llm_neg + list(base_cls.get("negative_tags") or [])))
            tags = sorted(set(llm_all + pos_tags + neg_tags + list(base_cls.get("tags") or [])))

            if allow_new_tags:
                for item in (data.get("new_tags") or []):
                    slug = str((item or {}).get("slug") or "").strip().lower()
                    title_ru = str((item or {}).get("title_ru") or slug).strip()
                    if not slug:
                        continue
                    if slug not in tag_slugs:
                        session.add(
                            ReasonTagCatalog(
                                slug=slug[:64],
                                title_ru=title_ru[:128] or slug[:64],
                                description="llm/user discovered tag",
                                is_active=True,
                                is_system=False,
                                created_at=datetime.utcnow(),
                                updated_at=datetime.utcnow(),
                            )
                        )
                        tag_slugs.append(slug)
                        created_tags += 1

            allowed = set(tag_slugs)
            tags = [t for t in tags if t in allowed]
            pos_tags = [t for t in pos_tags if t in allowed]
            neg_tags = [t for t in neg_tags if t in allowed]
            sentiment = str(data.get("reason_sentiment") or base_cls.get("sentiment") or "neutral").strip().lower()
            if sentiment not in {"positive", "negative", "mixed", "neutral"}:
                sentiment = str(base_cls.get("sentiment") or "neutral")
            new_val = tags or None
            if (
                new_val != (r.reason_tags or None)
                or (pos_tags or None) != (r.reason_positive_tags or None)
                or (neg_tags or None) != (r.reason_negative_tags or None)
                or (sentiment or None) != (r.reason_sentiment or None)
            ):
                r.reason_tags = new_val
                r.reason_positive_tags = pos_tags or None
                r.reason_negative_tags = neg_tags or None
                r.reason_sentiment = sentiment
                updated += 1

        return {
            "ok": True,
            "processed": processed,
            "updated": updated,
            "created_tags": created_tags,
            "only_null": bool(only_null),
            "scanned": len(rows_all),
        }
