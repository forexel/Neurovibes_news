from __future__ import annotations

import json
import math
import re
from collections import Counter
from datetime import datetime, timedelta

from sqlalchemy import func, select

from app.core.config import settings
from app.db import session_scope
from app.models import Article, ArticleStatus, AuditLog, DailySelection, Score, ScoreParameter, Source
from app.services.llm import get_client, track_usage_from_response
from app.services.runtime_settings import get_runtime_bool, get_runtime_csv_list, get_runtime_float, get_runtime_int
from app.services.topic_filter import passes_ai_topic_filter

CHANNEL_THEME = (
    "Artificial Intelligence, AI tools, AI regulation, LLM models, AGI research, "
    "robotics + AI, AI in business, major AI breakthroughs, AI startups funding, "
    "AI infrastructure (chips, compute, training), AI safety, AI policy."
)

FEATURE_WEIGHTS = {
    # Weights sum to 1.0 (keeps final_score in a stable ~0..1 range).
    "significance": 0.16,
    "relevance": 0.17,
    "novelty": 0.13,
    "trend_velocity": 0.09,
    "coverage": 0.07,
    "virality": 0.07,
    "longevity": 0.06,
    "scale": 0.05,
    "freshness": 0.05,
    "entity_count": 0.03,
    "number_count": 0.02,
    "source_priority": 0.02,
    # Channel preference: business IT is prioritized over automotive/industrial/investing topics.
    "business_it": 0.06,
    # Learned signal from editor's accepted/rejected history.
    "editor_style": 0.02,
}


def _get_feature_weights(session) -> dict[str, float]:
    rows = session.scalars(
        select(ScoreParameter).where(ScoreParameter.is_active.is_(True)).order_by(ScoreParameter.id.asc())
    ).all()
    if not rows:
        return FEATURE_WEIGHTS.copy()

    out: dict[str, float] = {}
    for row in rows:
        key = (row.key or "").strip()
        if not key:
            continue
        try:
            out[key] = max(0.0, float(row.weight))
        except Exception:
            continue

    if not out:
        return FEATURE_WEIGHTS.copy()

    total = float(sum(out.values()))
    if total <= 0:
        return FEATURE_WEIGHTS.copy()

    # Normalize so final score stays in 0..1 range.
    return {k: (v / total) for k, v in out.items()}

EVENT_NOVELTY_BANDS = {
    "incremental_update": (0.2, 0.4),
    "product_iteration": (0.4, 0.6),
    "funding_round": (0.5, 0.7),
    "research_breakthrough": (0.7, 0.9),
    "regulatory_shift": (0.8, 0.95),
    "market_structure_change": (0.8, 0.95),
    "paradigm_shift": (0.95, 1.0),
}

RESEARCH_HEAVY_SOURCES = {
    "papers with code (latest)",
    "arxiv cs.cl",
    "arxiv cs.lg",
    "arxiv cs.ai",
}

BLOOMBERG_HYPE_TITLE_KEYWORDS = {
    "launch",
    "release",
    "rollout",
    "unveils",
    "introduces",
    "debuts",
    "breakthrough",
    "major",
    "record",
    "first",
    "ai",
    "gpt",
    "llm",
    "chatgpt",
    "gemini",
    "claude",
    "openai",
    "anthropic",
    "deepmind",
    "meta",
    "nvidia",
    "security",
    "breach",
    "ban",
    "lawsuit",
    "regulation",
    "funding",
}

GEEK_HEAVY_KEYWORDS = {
    "ablation",
    "theorem",
    "proof",
    "sample complexity",
    "convergence",
    "gradient",
    "bayesian",
    "laplace",
    "cuda kernel",
    "kv cache",
    "quantization",
    "attention head",
    "embedding space",
    "benchmark suite",
    "architecture search",
    "formal verification",
    "petri net",
    "sat solver",
    "tokenization",
    "loss function",
    "hyperparameter",
}

PERSONNEL_MOVE_KEYWORDS = {
    "i’m joining",
    "i'm joining",
    "joins ",
    "joining ",
    "hired ",
    "hires ",
    "appointed",
    "appointment",
    "steps down",
    "resigns",
    "departure",
    "leaves ",
    "new ceo",
    "new cto",
    "new chief",
}

LOW_LOCAL_SIGNAL_KEYWORDS = {
    "india",
    "euro ncap",
    "davos",
    "wall street",
    "u.s.",
    "us ",
    "uk ",
}

STYLE_STOPWORDS = {
    "the",
    "and",
    "that",
    "with",
    "from",
    "this",
    "will",
    "into",
    "about",
    "after",
    "what",
    "when",
    "your",
    "their",
    "they",
    "been",
    "have",
    "over",
    "как",
    "что",
    "для",
    "про",
    "или",
    "это",
    "эти",
    "этой",
    "также",
    "после",
    "среди",
}


def _clip01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _clip10(value: float) -> float:
    return max(0.0, min(10.0, float(value)))


def _tokenize_style(text: str) -> list[str]:
    if not text:
        return []
    out = re.findall(r"[a-zA-Zа-яА-ЯёЁ][a-zA-Zа-яА-ЯёЁ0-9_-]{2,}", text.lower())
    return [t for t in out if len(t) >= 4 and t not in STYLE_STOPWORDS]


def _build_editor_style_profile(session) -> dict:
    positive_ids = set(
        session.scalars(
            select(Article.id).where(Article.status.in_([ArticleStatus.PUBLISHED, ArticleStatus.SELECTED_HOURLY]))
        ).all()
    )
    positive_ids.update(
        int(x)
        for x in session.scalars(select(DailySelection.article_id).where(DailySelection.active.is_(True))).all()
    )

    neg_ids = set(session.scalars(select(Article.id).where(Article.status == ArticleStatus.REJECTED)).all())
    deleted_logs = session.scalars(
        select(AuditLog).where(AuditLog.action == "article_delete_feedback", AuditLog.entity_type == "article")
    ).all()
    deleted_titles: list[str] = []
    deleted_reasons: list[str] = []
    for log in deleted_logs:
        payload = log.payload or {}
        t = str(payload.get("title") or "").strip()
        r = str(payload.get("reason") or "").strip()
        eid = str(log.entity_id or "").strip()
        if eid.isdigit():
            neg_ids.add(int(eid))
        if t:
            deleted_titles.append(t)
        if r:
            deleted_reasons.append(r)

    if len(positive_ids) < 8 or len(neg_ids) < 12:
        return {"enabled": False, "reason": "not_enough_examples"}

    pos_counter: Counter[str] = Counter()
    neg_counter: Counter[str] = Counter()
    pos_total = 0
    neg_total = 0

    if positive_ids:
        pos_rows = session.execute(select(Article.title, Article.subtitle).where(Article.id.in_(positive_ids))).all()
        for title, subtitle in pos_rows:
            toks = _tokenize_style(f"{title or ''} {subtitle or ''}")
            pos_counter.update(toks)
            pos_total += len(toks)

    if neg_ids:
        neg_rows = session.execute(select(Article.title, Article.subtitle).where(Article.id.in_(neg_ids))).all()
        for title, subtitle in neg_rows:
            toks = _tokenize_style(f"{title or ''} {subtitle or ''}")
            neg_counter.update(toks)
            neg_total += len(toks)

    for t in deleted_titles:
        toks = _tokenize_style(t)
        neg_counter.update(toks)
        neg_total += len(toks)
    for r in deleted_reasons:
        toks = _tokenize_style(r)
        neg_counter.update(toks)
        neg_total += len(toks)

    if pos_total < 50 or neg_total < 80:
        return {"enabled": False, "reason": "too_few_tokens"}

    vocab = set(pos_counter.keys()) | set(neg_counter.keys())
    weights: dict[str, float] = {}
    for token in vocab:
        p = (pos_counter.get(token, 0) + 1.0) / (pos_total + 2.0)
        n = (neg_counter.get(token, 0) + 1.0) / (neg_total + 2.0)
        w = math.log(p / n)
        if abs(w) >= 0.35:
            weights[token] = float(w)

    if not weights:
        return {"enabled": False, "reason": "empty_weights"}

    return {
        "enabled": True,
        "weights": weights,
        "positive_ids": len(positive_ids),
        "negative_ids": len(neg_ids),
    }


def _editor_style_score(profile: dict | None, article: Article) -> tuple[float, int]:
    if not profile or not profile.get("enabled"):
        return 0.0, 0
    weights = profile.get("weights") or {}
    if not isinstance(weights, dict) or not weights:
        return 0.0, 0
    tokens = list(dict.fromkeys(_tokenize_style(f"{article.title or ''} {article.subtitle or ''}")))
    vals = [float(weights[t]) for t in tokens if t in weights]
    if not vals:
        return 0.0, 0
    return float(sum(vals) / len(vals)), len(vals)


def _fails_editor_style_gate(style_score: float, style_hits: int, semantic: dict) -> bool:
    if style_hits < 2:
        return False
    relevance = float(semantic.get("relevance", 0.0))
    business_it = float(semantic.get("business_it", 0.0))
    significance = float(semantic.get("significance", 0.0))
    # Conservative gate:
    # archive only if article strongly looks like past rejected/deleted style
    # and it is not a clearly important/highly relevant business-AI story.
    return style_score <= -0.22 and relevance < 8.2 and business_it < 8.0 and significance < 8.3


def _freshness(published_at: datetime | None) -> float:
    if not published_at:
        return 0.5
    hours = max(0.0, (datetime.utcnow() - published_at).total_seconds() / 3600.0)
    return _clip01((10.0 - (hours / 3.0)) / 10.0)


def _source_priority(rank: int, max_rank: int) -> float:
    if max_rank <= 1:
        return 1.0
    return _clip01(1.0 - ((rank - 1) / max_rank))


def _tier(rank: int) -> int:
    if rank <= 10:
        return 1
    if rank <= 18:
        return 2
    return 3


def _entity_count_feature(article: Article) -> float:
    text = f"{article.title} {article.subtitle}"[:1200]
    entities = set(re.findall(r"\b[A-Z][a-zA-Z]{2,}\b|\b[A-Z]{2,}\b", text))
    return _clip01(len(entities) / 15.0)


def _number_count_feature(article: Article) -> float:
    text = f"{article.title} {article.subtitle} {article.text[:1000]}"
    numbers = re.findall(r"\b\d+[\d,.%$]*\b", text)
    return _clip01(len(numbers) / 10.0)


def _cluster_stats(session, article: Article) -> tuple[float, float, float, bool]:
    """Returns trend_velocity, coverage, cluster_age_hours, has_high_tier_peer.

    coverage: unique sources in cluster over 24h, normalized.
    trend_velocity: momentum in last 3h vs previous 3h.
    """
    cluster_key = article.cluster_key
    if not cluster_key:
        return 0.0, 0.0, 999.0, False

    now = datetime.utcnow()
    h24 = now - timedelta(hours=24)
    h3 = now - timedelta(hours=3)
    h6 = now - timedelta(hours=6)

    rows24 = session.execute(
        select(Article.id, Article.source_id, Article.created_at, Source.priority_rank)
        .join(Source, Source.id == Article.source_id)
        .where(Article.cluster_key == cluster_key, Article.created_at >= h24)
    ).all()

    if not rows24:
        return 0.0, 0.0, 999.0, False

    unique_sources = {r[1] for r in rows24}
    coverage = _clip01(len(unique_sources) / 8.0)

    count_last3 = sum(1 for _, _, created_at, _ in rows24 if created_at >= h3)
    count_prev3 = sum(1 for _, _, created_at, _ in rows24 if h6 <= created_at < h3)
    trend_velocity = _clip01((count_last3 / 4.0) + max(0.0, (count_last3 - count_prev3) / 6.0))

    cluster_age_hours = min((now - min(r[2] for r in rows24)).total_seconds() / 3600.0, 999.0)

    has_high_tier_peer = any((_tier(int(r[3])) <= 2) for r in rows24 if r[0] != article.id)
    return trend_velocity, coverage, cluster_age_hours, has_high_tier_peer


def _llm_semantic_features(article: Article, source_name: str) -> dict:
    fallback = {
        "significance": 5,
        "relevance": 5,
        "virality": 5,
        "longevity": 5,
        "scale": 5,
        "business_it": 5,
        "domain": "other",
        "event_type": "incremental_update",
        "novelty_score": 0.35,
        "novelty_reason": "Fallback semantic estimate",
    }

    if not settings.openrouter_api_key:
        return fallback
    client = get_client()
    prompt = f"""
You score AI-channel news. Return JSON only:
{{
  "significance": 0-10,
  "relevance": 0-10,
  "virality": 0-10,
  "longevity": 0-10,
  "scale": 0-10,
  "business_it": 0-10,
  "domain": "business_it|automotive|industrial|finance_investing|consumer|research|policy|other",
  "event_type": "incremental_update|product_iteration|funding_round|research_breakthrough|regulatory_shift|market_structure_change|paradigm_shift",
  "novelty_score": 0-1,
  "novelty_reason": "Why is this novel compared to existing AI news?"
}}

Relevance scoring guide:
0-3 weak AI link, 4-6 AI mentioned but secondary, 7-8 important AI news, 9-10 core AI event.

business_it guide (channel preference):
0-3 mostly automotive/industrial/investing/general tech
4-6 mixed
7-10 business IT / enterprise software / developer tooling / cloud / AI infrastructure / AI adoption in companies

Channel theme:
{CHANNEL_THEME}

Source: {source_name}
Title: {article.title}
Subtitle: {article.subtitle}
Text: {article.text[:7000]}
"""
    try:
        resp = client.chat.completions.create(
            model="openai/gpt-4o-mini",
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "Return strictly valid JSON with numeric fields."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
        )
        track_usage_from_response(resp, operation="scoring.semantic_features", model="openai/gpt-4o-mini", kind="chat")
        data = json.loads(resp.choices[0].message.content or "{}")
        event_type = str(data.get("event_type") or "incremental_update")
        band = EVENT_NOVELTY_BANDS.get(event_type)
        novelty_llm = float(data.get("novelty_score", fallback["novelty_score"]))
        if band:
            novelty_llm = _clip01(max(band[0], min(band[1], novelty_llm)))

        return {
            "significance": _clip10(float(data.get("significance", fallback["significance"]))),
            "relevance": _clip10(float(data.get("relevance", fallback["relevance"]))),
            "virality": _clip10(float(data.get("virality", fallback["virality"]))),
            "longevity": _clip10(float(data.get("longevity", fallback["longevity"]))),
            "scale": _clip10(float(data.get("scale", fallback["scale"]))),
            "business_it": _clip10(float(data.get("business_it", fallback["business_it"]))),
            "domain": str(data.get("domain") or fallback["domain"]),
            "event_type": event_type,
            "novelty_score": novelty_llm,
            "novelty_reason": str(data.get("novelty_reason") or fallback["novelty_reason"]),
        }
    except Exception:
        return fallback


def _novelty(base_cluster_novelty: float, semantic: dict) -> float:
    return _clip01(max(base_cluster_novelty, float(semantic.get("novelty_score", 0.35))))


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _title_hype_score(title: str) -> float:
    t = (title or "").strip().lower()
    if not t:
        return 0.0
    kw_hits = sum(1 for k in BLOOMBERG_HYPE_TITLE_KEYWORDS if k in t)
    punct = 1 if ("!" in t or ":" in t) else 0
    return _clip01((kw_hits / 3.0) + (0.15 * punct))


def _is_bloomberg_low_hype(article: Article, semantic: dict, source_name: str | None) -> bool:
    source_low = (source_name or "").strip().lower()
    if "bloomberg" not in source_low:
        return False

    # User rule: for Bloomberg, if title isn't hype and full text is missing -> hide.
    if (article.content_mode or "summary_only") == "full":
        return False

    title_hype = _title_hype_score(article.title or "")
    virality = float(semantic.get("virality") or 0.0)
    significance = float(semantic.get("significance") or 0.0)
    relevance = float(semantic.get("relevance") or 0.0)

    if title_hype < 0.45 and virality < 8.2 and significance < 8.8 and relevance < 9.0:
        return True
    return False


def _is_too_geek_for_mass(article: Article, semantic: dict, source_name: str | None) -> bool:
    text = f"{article.title or ''} {article.subtitle or ''} {(article.text or '')[:3000]}".lower()
    source_low = (source_name or "").strip().lower()
    domain = str(semantic.get("domain") or "").strip().lower()
    relevance = float(semantic.get("relevance") or 0.0)
    significance = float(semantic.get("significance") or 0.0)
    business_it = float(semantic.get("business_it") or 0.0)
    virality = float(semantic.get("virality") or 0.0)

    kw_hits = sum(1 for k in GEEK_HEAVY_KEYWORDS if k in text)
    research_like = domain == "research" or source_low in RESEARCH_HEAVY_SOURCES
    code_like = ("```" in text) or ("pip install" in text) or ("github.com/" in text and "paper" in text)

    # For mass audience policy, research-like sources/domains are considered geek by default.
    geek_signal = kw_hits >= 1 or research_like or code_like
    if not geek_signal:
        return False

    # Keep only if it's truly exceptional and broad-impact.
    if relevance >= 9.5 and significance >= 9.4 and business_it >= 9.2 and virality >= 8.8:
        return False
    return True


def _is_personnel_move_low_value(article: Article, semantic: dict) -> bool:
    title = (article.title or "").lower()
    subtitle = (article.subtitle or "").lower()
    text = f"{title} {subtitle}"
    if not any(k in text for k in PERSONNEL_MOVE_KEYWORDS):
        return False

    significance = float(semantic.get("significance") or 0.0)
    relevance = float(semantic.get("relevance") or 0.0)
    virality = float(semantic.get("virality") or 0.0)
    business_it = float(semantic.get("business_it") or 0.0)

    # Keep only truly major personnel stories.
    if significance >= 9.4 and relevance >= 9.2 and (virality >= 8.8 or business_it >= 9.0):
        return False
    return True


def _is_low_local_practical_value(article: Article, semantic: dict) -> bool:
    text = f"{article.title or ''} {article.subtitle or ''} {(article.text or '')[:1500]}".lower()
    if not any(k in text for k in LOW_LOCAL_SIGNAL_KEYWORDS):
        return False

    relevance = float(semantic.get("relevance") or 0.0)
    significance = float(semantic.get("significance") or 0.0)
    business_it = float(semantic.get("business_it") or 0.0)
    virality = float(semantic.get("virality") or 0.0)

    # If no clear practical/wow impact for broad audience, hide.
    # Slightly stricter to filter "far-away but not actionable" stories.
    if relevance <= 9.0 and significance < 9.3 and business_it < 8.9 and virality < 8.8:
        return True
    return False


def _is_summary_and_boring(article: Article, semantic: dict) -> bool:
    if (article.content_mode or "summary_only") == "full":
        return False
    title_hype = _title_hype_score(article.title or "")
    relevance = float(semantic.get("relevance") or 0.0)
    significance = float(semantic.get("significance") or 0.0)
    virality = float(semantic.get("virality") or 0.0)
    if title_hype < 0.40 and relevance < 9.0 and significance < 9.1 and virality < 8.7:
        return True
    return False


def _geek_penalty_factor(article: Article, semantic: dict, source_name: str | None) -> float:
    """Returns multiplicative penalty in [0.7..1.0] for geek-heavy content."""
    text = f"{article.title or ''} {article.subtitle or ''} {(article.text or '')[:2500]}".lower()
    source_low = (source_name or "").strip().lower()
    domain = str(semantic.get("domain") or "").strip().lower()
    business_it = float(semantic.get("business_it") or 0.0)
    relevance = float(semantic.get("relevance") or 0.0)
    significance = float(semantic.get("significance") or 0.0)

    kw_hits = sum(1 for k in GEEK_HEAVY_KEYWORDS if k in text)
    research_like = domain == "research" or source_low in RESEARCH_HEAVY_SOURCES

    # No penalty for clearly mass-impact stories.
    if relevance >= 9.2 and significance >= 9.0 and business_it >= 8.8:
        return 1.0

    if _is_personnel_move_low_value(article, semantic):
        return 0.68
    if _is_low_local_practical_value(article, semantic):
        return 0.74
    if _is_summary_and_boring(article, semantic):
        return 0.78
    if research_like:
        return 0.72 if business_it < 8.5 else 0.82
    if kw_hits >= 3:
        return 0.80
    if kw_hits >= 1 and business_it < 8.0:
        return 0.88
    return 1.0


def _is_too_technical(semantic: dict, source_name: str | None) -> bool:
    if not get_runtime_bool("technical_filter_enabled", default=True):
        return False
    domain = str(semantic.get("domain") or "").strip().lower()
    business_it = float(semantic.get("business_it") or 0.0)
    significance = float(semantic.get("significance") or 0.0)
    source_low = (source_name or "").strip().lower()

    low_business = business_it < get_runtime_float("technical_filter_business_it_max", default=7.4)
    low_significance = significance < get_runtime_float("technical_filter_significance_max", default=8.8)
    if not (low_business and low_significance):
        return False

    if domain == "research":
        return True
    if source_low in RESEARCH_HEAVY_SOURCES:
        return True
    return False


def _is_too_investing(semantic: dict) -> bool:
    if not get_runtime_bool("investing_filter_enabled", default=True):
        return False
    domain = str(semantic.get("domain") or "").strip().lower()
    if domain != "finance_investing":
        return False

    business_it = float(semantic.get("business_it") or 0.0)
    significance = float(semantic.get("significance") or 0.0)
    relevance = float(semantic.get("relevance") or 0.0)

    return (
        business_it < get_runtime_float("investing_filter_business_it_max", default=8.0)
        and significance < get_runtime_float("investing_filter_significance_max", default=8.9)
        and relevance < get_runtime_float("investing_filter_relevance_max", default=9.0)
    )


def _is_too_deep_technical(article: Article, semantic: dict, source_name: str | None) -> bool:
    if not get_runtime_bool("deep_technical_filter_enabled", default=True):
        return False
    text = f"{article.title or ''} {article.subtitle or ''} {(article.text or '')[:1800]}".lower()
    domain = str(semantic.get("domain") or "").strip().lower()
    business_it = float(semantic.get("business_it") or 0.0)
    significance = float(semantic.get("significance") or 0.0)
    relevance = float(semantic.get("relevance") or 0.0)

    keywords = [k.strip().lower() for k in get_runtime_csv_list("deep_technical_keywords_csv")]
    kw_hits = sum(1 for k in keywords if k in text)
    # Strict mode for research-heavy sources (arXiv, PapersWithCode) + deep-tech keywords.
    research_like = domain == "research" or (source_name or "").strip().lower() in RESEARCH_HEAVY_SOURCES or kw_hits >= 2

    if not research_like:
        return False

    return (
        business_it < get_runtime_float("deep_technical_filter_business_it_max", default=8.5)
        and significance < get_runtime_float("deep_technical_filter_significance_max", default=9.2)
        and relevance < get_runtime_float("deep_technical_filter_relevance_max", default=9.4)
    )


def _has_mass_audience_override(article: Article) -> bool:
    text = f"{article.title or ''} {article.subtitle or ''} {(article.text or '')[:2500]}".lower()
    keywords = [k.strip().lower() for k in get_runtime_csv_list("mass_audience_wow_keywords_csv")]
    return any(k in text for k in keywords)


def _is_low_mass_audience(semantic: dict, source_name: str | None, article: Article) -> bool:
    if not get_runtime_bool("mass_audience_filter_enabled", default=True):
        return False
    if _has_mass_audience_override(article):
        return False

    domain = str(semantic.get("domain") or "").strip().lower()
    source_low = (source_name or "").strip().lower()
    business_it = float(semantic.get("business_it") or 0.0)
    significance = float(semantic.get("significance") or 0.0)
    relevance = float(semantic.get("relevance") or 0.0)

    low_triple = (
        business_it < get_runtime_float("mass_audience_business_it_max", default=8.6)
        and significance < get_runtime_float("mass_audience_significance_max", default=9.1)
        and relevance < get_runtime_float("mass_audience_relevance_max", default=9.2)
    )
    if not low_triple:
        return False

    if source_low in RESEARCH_HEAVY_SOURCES:
        return True
    if domain in {"research", "automotive", "industrial", "finance_investing"}:
        return True
    return False


def score_article_in_session(session, article: Article, max_rank: int, editor_style_profile: dict | None = None) -> dict:
    """
    Score a single article inside an existing DB session/transaction.

    Used for:
    - auto-score immediately after ingestion
    - scoring jobs
    - admin "Score" button (via score_article_by_id)
    """
    if article.status in {ArticleStatus.ARCHIVED, ArticleStatus.DOUBLE}:
        return {"ok": False, "error": f"article_not_scorable: status={article.status}"}

    # Hard topical gate: if article is not about AI, archive it immediately.
    if not passes_ai_topic_filter(
        title=article.title or "",
        subtitle=article.subtitle or "",
        text=article.text or "",
        tags=article.tags or [],
    ):
        score = session.get(Score, article.id)
        if score is None:
            score = Score(article_id=article.id)
            session.add(score)
        score.significance = 0.0
        score.freshness = round(_freshness(article.published_at) * 10.0, 4)
        score.relevance = 0.0
        score.virality = 0.0
        score.uniqueness = 0.0
        score.source_trust = 0.0
        score.longevity = 0.0
        score.scale = 0.0
        score.final_score = 0.0
        score.reasoning = "Non-AI article by topical gate"
        score.features = {"topical_gate": "failed"}
        score.uncertainty = 0.0
        article.status = ArticleStatus.ARCHIVED
        article.archived_kind = "filter"
        article.archived_reason = "non_ai"
        article.archived_at = datetime.utcnow()
        article.updated_at = datetime.utcnow()
        return {"ok": True, "article_id": article.id, "archived": True, "reason": "non_ai"}

    source = session.get(Source, article.source_id)
    rank = int(source.priority_rank if source else max_rank)

    freshness = _freshness(article.published_at)
    source_priority = _source_priority(rank, max_rank)
    entity_count = _entity_count_feature(article)
    number_count = _number_count_feature(article)
    trend_velocity, coverage, cluster_age_hours, has_high_tier_peer = _cluster_stats(session, article)

    if _tier(rank) == 3 and has_high_tier_peer:
        source_priority *= 0.6

    semantic = _llm_semantic_features(article, source.name if source else "Unknown")
    style_score, style_hits = _editor_style_score(editor_style_profile, article)
    significance = _clip01(float(semantic["significance"]) / 10.0)
    relevance = _clip01(float(semantic["relevance"]) / 10.0)
    virality = _clip01(float(semantic["virality"]) / 10.0)
    longevity = _clip01(float(semantic["longevity"]) / 10.0)
    scale = _clip01(float(semantic["scale"]) / 10.0)
    business_it = _clip01(float(semantic.get("business_it", 5)) / 10.0)

    base_novelty = 0.3 if (cluster_age_hours < 12.0 and coverage > 0.375) else 0.0
    novelty = _novelty(base_novelty, semantic)

    features = {
        "freshness": freshness,
        "source_priority": source_priority,
        "entity_count": entity_count,
        "number_count": number_count,
        "trend_velocity": trend_velocity,
        "coverage": coverage,
        "significance": significance,
        "relevance": relevance,
        "virality": virality,
        "longevity": longevity,
        "scale": scale,
        "novelty": novelty,
        "business_it": business_it,
        "editor_style": _clip01((style_score + 1.0) / 2.0),
    }

    weights = _get_feature_weights(session)
    contributions = {k: round(v * float(weights.get(k, 0.0)), 6) for k, v in features.items()}
    final_linear = float(sum(contributions.values()))
    geek_penalty = _geek_penalty_factor(article, semantic, source.name if source else None)
    final_linear *= geek_penalty
    final_linear = _clip01(final_linear)
    p = _sigmoid(6.0 * (final_linear - 0.5))
    uncertainty = 1.0 - abs(p - 0.5) * 2.0

    sorted_drivers = sorted(contributions.items(), key=lambda x: x[1], reverse=True)
    top_drivers = [f"{k}: {round(features[k], 3)}" for k, _ in sorted_drivers[:3]]

    score = session.get(Score, article.id)
    if score is None:
        score = Score(article_id=article.id)
        session.add(score)

    score.significance = semantic["significance"]
    score.freshness = round(freshness * 10.0, 4)
    score.relevance = semantic["relevance"]
    score.virality = semantic["virality"]
    score.uniqueness = round(novelty * 10.0, 4)
    score.source_trust = 0.0
    score.longevity = semantic["longevity"]
    score.scale = semantic["scale"]
    score.final_score = round(final_linear, 6)
    score.reasoning = f"{semantic['novelty_reason']} | top_drivers={'; '.join(top_drivers)}"
    score.features = {
        **features,
        "editor_style_raw": style_score,
        "editor_style_hits": style_hits,
        "geek_penalty": geek_penalty,
        "domain": semantic.get("domain"),
        "event_type": semantic["event_type"],
        "novelty_reason": semantic["novelty_reason"],
        "feature_contributions": contributions,
        "feature_weights": weights,
        "probability": p,
        "top_drivers": top_drivers,
    }
    score.uncertainty = round(uncertainty, 6)

    if float(semantic["relevance"]) < get_runtime_float("min_relevance_for_content", default=7.0):
        article.status = ArticleStatus.ARCHIVED
        article.archived_kind = "filter"
        article.archived_reason = "low_relevance"
        article.archived_at = datetime.utcnow()
    elif _is_personnel_move_low_value(article, semantic):
        article.status = ArticleStatus.ARCHIVED
        article.archived_kind = "filter"
        article.archived_reason = "personnel_move_gate"
        article.archived_at = datetime.utcnow()
        score.reasoning = f"{score.reasoning} | personnel_move_gate=failed"
        score.features = {**(score.features or {}), "personnel_move_gate": "failed"}
    elif _is_low_local_practical_value(article, semantic):
        article.status = ArticleStatus.ARCHIVED
        article.archived_kind = "filter"
        article.archived_reason = "local_practical_gate"
        article.archived_at = datetime.utcnow()
        score.reasoning = f"{score.reasoning} | local_practical_gate=failed"
        score.features = {**(score.features or {}), "local_practical_gate": "failed"}
    elif _is_summary_and_boring(article, semantic):
        article.status = ArticleStatus.ARCHIVED
        article.archived_kind = "filter"
        article.archived_reason = "summary_boring_gate"
        article.archived_at = datetime.utcnow()
        score.reasoning = f"{score.reasoning} | summary_boring_gate=failed"
        score.features = {**(score.features or {}), "summary_boring_gate": "failed"}
    elif _is_bloomberg_low_hype(article, semantic, source.name if source else None):
        article.status = ArticleStatus.ARCHIVED
        article.archived_kind = "filter"
        article.archived_reason = "bloomberg_hype_gate"
        article.archived_at = datetime.utcnow()
        score.reasoning = f"{score.reasoning} | bloomberg_hype_gate=failed"
        score.features = {**(score.features or {}), "bloomberg_hype_gate": "failed"}
    elif _is_too_technical(semantic, source.name if source else None):
        article.status = ArticleStatus.ARCHIVED
        article.archived_kind = "filter"
        article.archived_reason = "technical_gate"
        article.archived_at = datetime.utcnow()
        score.reasoning = f"{score.reasoning} | technical_gate=failed"
        score.features = {**(score.features or {}), "technical_gate": "failed"}
    elif _is_too_deep_technical(article, semantic, source.name if source else None):
        article.status = ArticleStatus.ARCHIVED
        article.archived_kind = "filter"
        article.archived_reason = "deep_technical_gate"
        article.archived_at = datetime.utcnow()
        score.reasoning = f"{score.reasoning} | deep_technical_gate=failed"
        score.features = {**(score.features or {}), "deep_technical_gate": "failed"}
    elif _is_too_geek_for_mass(article, semantic, source.name if source else None):
        article.status = ArticleStatus.ARCHIVED
        article.archived_kind = "filter"
        article.archived_reason = "geek_gate"
        article.archived_at = datetime.utcnow()
        score.reasoning = f"{score.reasoning} | geek_gate=failed"
        score.features = {**(score.features or {}), "geek_gate": "failed"}
    elif _is_too_investing(semantic):
        article.status = ArticleStatus.ARCHIVED
        article.archived_kind = "filter"
        article.archived_reason = "investing_gate"
        article.archived_at = datetime.utcnow()
        score.reasoning = f"{score.reasoning} | investing_gate=failed"
        score.features = {**(score.features or {}), "investing_gate": "failed"}
    elif _is_low_mass_audience(semantic, source.name if source else None, article):
        article.status = ArticleStatus.ARCHIVED
        article.archived_kind = "filter"
        article.archived_reason = "mass_audience_gate"
        article.archived_at = datetime.utcnow()
        score.reasoning = f"{score.reasoning} | mass_audience_gate=failed"
        score.features = {**(score.features or {}), "mass_audience_gate": "failed"}
    elif _fails_editor_style_gate(style_score, style_hits, semantic):
        article.status = ArticleStatus.ARCHIVED
        article.archived_kind = "filter"
        article.archived_reason = "editor_style_gate"
        article.archived_at = datetime.utcnow()
        score.reasoning = f"{score.reasoning} | editor_style_gate=failed"
        score.features = {**(score.features or {}), "editor_style_gate": "failed"}
    else:
        article.status = ArticleStatus.REVIEW if (0.45 < p < 0.60) else ArticleStatus.SCORED
    article.updated_at = datetime.utcnow()

    # Fast RU preview (title + one-line overview) for the article list.
    # This is intentionally cheaper than Generate Post and helps browsing.
    if article.status not in {ArticleStatus.ARCHIVED, ArticleStatus.DOUBLE}:
        _ensure_ru_preview(session, article)

    return {
        "ok": True,
        "article_id": article.id,
        "status": article.status,
        "final_score": score.final_score,
        "relevance": score.relevance,
        "reasoning": score.reasoning,
    }


def _ensure_ru_preview(session, article: Article) -> None:
    """
    Fill `ru_title` and `short_hook` (used as RU overview in list UI) if missing.
    Uses the per-request OpenRouter key if available (see llm middleware).
    """
    # Don't overwrite editor-written RU text.
    if (article.ru_title or "").strip() and (article.short_hook or "").strip():
        return

    title = (article.title or "").strip()
    subtitle = (article.subtitle or "").strip()
    text = (article.text or "").strip()
    if not title:
        return

    # If title already looks Russian, use it as-is and derive overview from subtitle/text.
    if not (article.ru_title or "").strip() and re.search(r"[А-Яа-яЁё]", title):
        article.ru_title = title[: get_runtime_int("max_title_chars", default=130)]
    if not (article.short_hook or "").strip() and (subtitle or text):
        raw = (subtitle or text[:260]).strip()
        if raw:
            article.short_hook = raw[: get_runtime_int("max_overview_chars", default=260)]

    # If still missing, ask LLM for RU title + 1-sentence overview.
    if (article.ru_title or "").strip() and (article.short_hook or "").strip():
        return

    max_title = get_runtime_int("max_title_chars", default=130)
    max_overview = get_runtime_int("max_overview_chars", default=260)
    prompt = f"""
Ты — редактор русскоязычного AI-канала.
Нужно быстро дать русскую "превью-версию" статьи для списка.

Сделай:
1) RU заголовок (до {max_title} символов)
2) Короткий RU обзор (1 предложение, до {max_overview} символов), без воды, без эмоций, без домыслов.

Правила:
- Только факты из текста.
- Сохраняй имена компаний/моделей и цифры.
- Не делай кликбейт.
- Никаких эмодзи.

Верни JSON only:
{{"ru_title":"...", "ru_overview":"..."}}

EN title: {title}
EN subtitle: {subtitle}
EN excerpt: {(text[:1200] if text else "")}
URL: {article.canonical_url}
""".strip()

    try:
        client = get_client()
        resp = client.chat.completions.create(
            model=settings.llm_text_model,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": "Return strictly valid JSON."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        track_usage_from_response(resp, operation="content.ru_preview", model=settings.llm_text_model, kind="chat")
        data = json.loads(resp.choices[0].message.content or "{}")
        ru_title = (str(data.get("ru_title") or "").strip() or title)[:max_title]
        ru_overview = (str(data.get("ru_overview") or "").strip() or (subtitle or text[:max_overview]))[:max_overview]
        if not (article.ru_title or "").strip():
            article.ru_title = ru_title
        if not (article.short_hook or "").strip():
            article.short_hook = ru_overview
    except Exception:
        # Best-effort fallback.
        if not (article.ru_title or "").strip():
            article.ru_title = title[:max_title]
        if not (article.short_hook or "").strip() and (subtitle or text):
            article.short_hook = (subtitle or text[:max_overview]).strip()[:max_overview]


def score_article_by_id(article_id: int) -> dict:
    """
    Score a single article on-demand (for admin UI).

    This is used when an article exists in DB but has no Score row yet.
    """
    with session_scope() as session:
        max_rank = int(session.scalar(select(func.max(Source.priority_rank))) or 22)
        editor_style_profile = _build_editor_style_profile(session)
        article = session.get(Article, article_id)
        if not article:
            return {"ok": False, "error": "article_not_found"}
        return score_article_in_session(session, article, max_rank=max_rank, editor_style_profile=editor_style_profile)


def run_scoring(limit: int = 300, progress_cb=None) -> int:
    processed = 0
    with session_scope() as session:
        max_rank = int(session.scalar(select(func.max(Source.priority_rank))) or 22)
        editor_style_profile = _build_editor_style_profile(session)

        # Score all unscored articles, not only NEW/INBOX.
        # This covers cases when article was manually moved to another status
        # (e.g. SELECTED_HOURLY) before scoring.
        articles = session.scalars(
            select(Article)
            .join(Score, Score.article_id == Article.id, isouter=True)
            .where(
                Score.article_id.is_(None),
                Article.status != ArticleStatus.ARCHIVED,
                Article.status != ArticleStatus.DOUBLE,
            )
            .order_by(Article.created_at.asc())
            .limit(limit)
        ).all()
        total = len(articles)
        if progress_cb:
            progress_cb(0, total)

        for article in articles:
            if article.status == ArticleStatus.DOUBLE:
                if progress_cb:
                    progress_cb(processed, total)
                continue
            score_article_in_session(session, article, max_rank=max_rank, editor_style_profile=editor_style_profile)
            processed += 1
            if progress_cb:
                progress_cb(processed, total)

        # Backfill RU preview fields for already-scored items (helps list browsing).
        # This is cheaper than full Generate Post and only targets AI-relevant items.
        if get_runtime_bool("ru_preview_enabled", default=True):
            min_rel = get_runtime_float("min_relevance_for_content", default=7.0)
            days_back = get_runtime_int("ru_preview_days_back", default=14)
            fill_limit = get_runtime_int("ru_preview_fill_limit", default=120)
            cutoff = datetime.utcnow() - timedelta(days=days_back)
            targets = session.scalars(
                select(Article)
                .join(Score, Score.article_id == Article.id)
                .where(
                    Article.status.in_(
                        [
                            ArticleStatus.INBOX,
                            ArticleStatus.REVIEW,
                            ArticleStatus.SCORED,
                            ArticleStatus.READY,
                            ArticleStatus.SELECTED_HOURLY,
                        ]
                    ),
                    Article.status != ArticleStatus.ARCHIVED,
                    Article.status != ArticleStatus.DOUBLE,
                    (Article.ru_title.is_(None) | (Article.ru_title == "") | Article.short_hook.is_(None) | (Article.short_hook == "")),
                    Score.relevance >= min_rel,
                    (Article.published_at.is_(None) | (Article.published_at >= cutoff)),
                )
                .order_by(Article.updated_at.desc())
                .limit(fill_limit)
            ).all()
            for t in targets:
                _ensure_ru_preview(session, t)

    return processed


def prune_non_ai_articles(limit: int = 5000) -> int:
    """Soft-delete non-AI articles already stored in DB."""
    updated = 0
    with session_scope() as session:
        rows = session.scalars(
            select(Article)
            .where(Article.status != ArticleStatus.ARCHIVED, Article.status != ArticleStatus.DOUBLE)
            .order_by(Article.created_at.desc())
            .limit(limit)
        ).all()

        for article in rows:
            if passes_ai_topic_filter(
                title=article.title or "",
                subtitle=article.subtitle or "",
                text=article.text or "",
                tags=article.tags or [],
            ):
                continue
            article.status = ArticleStatus.ARCHIVED
            article.updated_at = datetime.utcnow()
            updated += 1
    return updated


def prune_bad_articles(limit: int = 50000, archive_summary_only: bool = True, archive_low_relevance: bool = True) -> dict:
    """
    Soft-hide articles that should not appear in admin by default.

    Rules:
    - Non-AI by topical gate -> archived
    - Low semantic relevance (if scored) -> archived
    - summary_only (couldn't scrape full text) -> archived (unless you later enrich and re-score)
    """
    counts = {
        "archived": 0,
        "non_ai": 0,
        "low_relevance": 0,
        "summary_only": 0,
        "too_technical": 0,
        "too_deep_technical": 0,
        "too_geek": 0,
        "bloomberg_low_hype": 0,
        "personnel_move_low_value": 0,
        "local_practical_low_value": 0,
        "summary_boring": 0,
        "too_investing": 0,
        "low_mass_audience": 0,
    }
    with session_scope() as session:
        rows = session.execute(
            select(Article, Score)
            .join(Score, Score.article_id == Article.id, isouter=True)
            .where(Article.status != ArticleStatus.ARCHIVED, Article.status != ArticleStatus.DOUBLE)
            .order_by(Article.created_at.desc())
            .limit(limit)
        ).all()

        for article, score in rows:
            # Do not auto-hide already published or hourly-selected content.
            if article.status in {ArticleStatus.PUBLISHED, ArticleStatus.SELECTED_HOURLY}:
                continue

            if archive_summary_only and (article.content_mode or "summary_only") == "summary_only":
                article.status = ArticleStatus.ARCHIVED
                article.updated_at = datetime.utcnow()
                counts["archived"] += 1
                counts["summary_only"] += 1
                continue

            if not passes_ai_topic_filter(
                title=article.title or "",
                subtitle=article.subtitle or "",
                text=article.text or "",
                tags=article.tags or [],
            ):
                article.status = ArticleStatus.ARCHIVED
                article.updated_at = datetime.utcnow()
                counts["archived"] += 1
                counts["non_ai"] += 1
                continue

            if (
                archive_low_relevance
                and score is not None
                and float(score.relevance or 0.0) < get_runtime_float("min_relevance_for_content", default=7.0)
            ):
                article.status = ArticleStatus.ARCHIVED
                article.updated_at = datetime.utcnow()
                counts["archived"] += 1
                counts["low_relevance"] += 1
                continue

            if score is not None and get_runtime_bool("technical_filter_enabled", default=True):
                semantic = {
                    "domain": ((score.features or {}).get("domain") if isinstance(score.features, dict) else None),
                    "business_it": (float((score.features or {}).get("business_it", 0)) * 10.0) if isinstance(score.features, dict) else 0.0,
                    "significance": float(score.significance or 0.0),
                    "relevance": float(score.relevance or 0.0),
                    "virality": float(score.virality or 0.0),
                }
                source = session.get(Source, article.source_id)
                if _is_personnel_move_low_value(article, semantic):
                    article.status = ArticleStatus.ARCHIVED
                    article.updated_at = datetime.utcnow()
                    counts["archived"] += 1
                    counts["personnel_move_low_value"] += 1
                    continue

                if _is_low_local_practical_value(article, semantic):
                    article.status = ArticleStatus.ARCHIVED
                    article.updated_at = datetime.utcnow()
                    counts["archived"] += 1
                    counts["local_practical_low_value"] += 1
                    continue

                if _is_summary_and_boring(article, semantic):
                    article.status = ArticleStatus.ARCHIVED
                    article.updated_at = datetime.utcnow()
                    counts["archived"] += 1
                    counts["summary_boring"] += 1
                    continue

                if _is_bloomberg_low_hype(article, semantic, source.name if source else None):
                    article.status = ArticleStatus.ARCHIVED
                    article.updated_at = datetime.utcnow()
                    counts["archived"] += 1
                    counts["bloomberg_low_hype"] += 1
                    continue

                if _is_too_technical(semantic, source.name if source else None):
                    article.status = ArticleStatus.ARCHIVED
                    article.updated_at = datetime.utcnow()
                    counts["archived"] += 1
                    counts["too_technical"] += 1
                    continue

                if _is_too_deep_technical(article, semantic, source.name if source else None):
                    article.status = ArticleStatus.ARCHIVED
                    article.updated_at = datetime.utcnow()
                    counts["archived"] += 1
                    counts["too_deep_technical"] += 1
                    continue

                if _is_too_geek_for_mass(article, semantic, source.name if source else None):
                    article.status = ArticleStatus.ARCHIVED
                    article.updated_at = datetime.utcnow()
                    counts["archived"] += 1
                    counts["too_geek"] += 1
                    continue

                if _is_too_investing(semantic):
                    article.status = ArticleStatus.ARCHIVED
                    article.updated_at = datetime.utcnow()
                    counts["archived"] += 1
                    counts["too_investing"] += 1
                    continue

                if _is_low_mass_audience(semantic, source.name if source else None, article):
                    article.status = ArticleStatus.ARCHIVED
                    article.updated_at = datetime.utcnow()
                    counts["archived"] += 1
                    counts["low_mass_audience"] += 1

    return counts


def rescore_all_articles(limit: int = 50000, include_archived: bool = True) -> dict:
    """
    Re-score all articles (optionally including archived) after filter/prompt changes.
    Useful when editorial policy changes and old decisions must be recalculated.
    """
    processed = 0
    reactivated = 0
    with session_scope() as session:
        max_rank = int(session.scalar(select(func.max(Source.priority_rank))) or 22)
        editor_style_profile = _build_editor_style_profile(session)
        q = select(Article.id).where(Article.status != ArticleStatus.DOUBLE, Article.status != ArticleStatus.PUBLISHED)
        if not include_archived:
            q = q.where(Article.status != ArticleStatus.ARCHIVED)
        ids = session.scalars(q.order_by(Article.created_at.asc()).limit(limit)).all()

        for article_id in ids:
            article = session.get(Article, article_id)
            if not article:
                continue
            if article.status == ArticleStatus.ARCHIVED and include_archived:
                article.status = ArticleStatus.INBOX
                reactivated += 1
            score_article_in_session(session, article, max_rank=max_rank, editor_style_profile=editor_style_profile)
            processed += 1

    return {"processed": processed, "reactivated_archived": reactivated}


def reclassify_all_articles(
    limit: int = 100000,
    include_archived: bool = True,
    days_back: int | None = None,
    exclude_deleted: bool = False,
) -> dict:
    """
    Re-apply gates to all articles using existing scores/features, without
    triggering expensive LLM rescoring for every record.
    """
    scanned = 0
    archived = 0
    restored = 0
    unchanged = 0
    scored_missing = 0

    with session_scope() as session:
        editor_style_profile = _build_editor_style_profile(session)
        # Articles deleted manually by editor should never auto-restore.
        deleted_by_editor_ids = {
            int(row[0])
            for row in session.execute(
                select(AuditLog.entity_id).where(
                    AuditLog.action == "article_delete_feedback",
                    AuditLog.entity_type == "article",
                )
            ).all()
            if str(row[0]).isdigit()
        }

        q = select(Article.id).where(Article.status != ArticleStatus.DOUBLE, Article.status != ArticleStatus.PUBLISHED)
        if not include_archived:
            q = q.where(Article.status != ArticleStatus.ARCHIVED)
        if exclude_deleted:
            q = q.where((Article.archived_kind.is_(None)) | (Article.archived_kind != "delete"))
        if days_back is not None:
            cutoff = datetime.utcnow() - timedelta(days=int(days_back))
            q = q.where((Article.created_at >= cutoff) | (Article.published_at >= cutoff))
        ids = session.scalars(q.order_by(Article.created_at.asc()).limit(limit)).all()

        for article_id in ids:
            article = session.get(Article, article_id)
            if not article:
                continue
            scanned += 1

            # Topical gate always applies.
            if not passes_ai_topic_filter(
                title=article.title or "",
                subtitle=article.subtitle or "",
                text=article.text or "",
                tags=article.tags or [],
            ):
                if article.status != ArticleStatus.ARCHIVED:
                    article.status = ArticleStatus.ARCHIVED
                    article.updated_at = datetime.utcnow()
                    archived += 1
                else:
                    unchanged += 1
                continue

            score = session.get(Score, article.id)
            if score is None:
                # No score -> keep active for later explicit scoring.
                if article.status == ArticleStatus.ARCHIVED and include_archived:
                    article.status = ArticleStatus.INBOX
                    article.updated_at = datetime.utcnow()
                    restored += 1
                else:
                    unchanged += 1
                scored_missing += 1
                continue

            semantic = {
                "domain": ((score.features or {}).get("domain") if isinstance(score.features, dict) else None),
                "business_it": (float((score.features or {}).get("business_it", 0)) * 10.0) if isinstance(score.features, dict) else 0.0,
                "significance": float(score.significance or 0.0),
                "relevance": float(score.relevance or 0.0),
                "virality": float(score.virality or 0.0),
            }
            style_score, style_hits = _editor_style_score(editor_style_profile, article)
            source = session.get(Source, article.source_id)
            should_archive = (
                float(score.relevance or 0.0) < get_runtime_float("min_relevance_for_content", default=7.0)
                or _is_personnel_move_low_value(article, semantic)
                or _is_low_local_practical_value(article, semantic)
                or _is_summary_and_boring(article, semantic)
                or _is_bloomberg_low_hype(article, semantic, source.name if source else None)
                or _is_too_technical(semantic, source.name if source else None)
                or _is_too_deep_technical(article, semantic, source.name if source else None)
                or _is_too_geek_for_mass(article, semantic, source.name if source else None)
                or _is_too_investing(semantic)
                or _is_low_mass_audience(semantic, source.name if source else None, article)
                or _fails_editor_style_gate(style_score, style_hits, semantic)
            )

            if should_archive:
                if article.status != ArticleStatus.ARCHIVED:
                    article.status = ArticleStatus.ARCHIVED
                    article.updated_at = datetime.utcnow()
                    archived += 1
                else:
                    unchanged += 1
            else:
                if article.status == ArticleStatus.ARCHIVED and include_archived and article.id not in deleted_by_editor_ids:
                    article.status = ArticleStatus.SCORED
                    article.updated_at = datetime.utcnow()
                    restored += 1
                else:
                    unchanged += 1

    return {
        "scanned": scanned,
        "archived": archived,
        "restored": restored,
        "unchanged": unchanged,
        "missing_score": scored_missing,
    }
