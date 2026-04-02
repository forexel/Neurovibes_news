from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import joinedload

from app.core.config import settings
from app.db import session_scope
from app.models import Article, ArticleStatus, DecisionMode, Score
from app.services.content_generation import generate_image_card, generate_ru_summary
from app.services.embedding_dedup import process_embeddings_and_dedup
from app.services.ingestion import enrich_openai_summary_only_articles, enrich_summary_only_articles, run_ingestion_fast
from app.services.llm import get_client, track_usage_from_response
from app.services.preference import (
    get_active_profile,
    log_training_event,
    predict_editor_choice_prob,
    save_selection_decision,
)
from app.services.scoring import article_is_selection_eligible, run_scoring
from app.services.telegram_context import telegram_timezone_name
from app.services.runtime_settings import get_runtime_str
from app.services.runtime_settings import get_runtime_csv_list, get_runtime_float
from app.services.topic_filter import _normalize_text


def _clip_multiplier(value: float) -> float:
    min_mult = get_runtime_float("editorial_min_multiplier", default=0.55)
    max_mult = get_runtime_float("editorial_max_multiplier", default=1.25)
    return float(max(min_mult, min(max_mult, value)))


def _unified_score_10(score: Score) -> float:
    # Single score scale for UI and ranking: 0..10 from model final_score (0..1).
    return float(max(0.0, min(10.0, float(score.final_score or 0.0) * 10.0)))


def _editorial_score_multiplier(article: Article, score: Score) -> tuple[float, list[str]]:
    features = score.features if isinstance(score.features, dict) else {}
    normalized_text = " ".join(
        [
            _normalize_text(article.title or ""),
            _normalize_text(article.subtitle or ""),
            _normalize_text(article.ru_title or ""),
            _normalize_text(article.short_hook or ""),
            _normalize_text(article.text[:800] if article.text else ""),
        ]
    ).strip()
    domain = str(features.get("domain") or "").strip().lower()
    event_type = str(features.get("event_type") or "").strip().lower()
    business_it = float(features.get("business_it") or 0.0)
    practical_product_signal = any(
        token in normalized_text
        for token in (
            "stitch",
            "browser",
            "ios",
            "android",
            "app store",
            "video",
            "image",
            "voice",
            "podcast",
            "creator",
            "content",
            "design",
            "prototype",
            "workflow",
            "assistant",
            "copilot",
            "workspace",
            "docs",
            "sheets",
            "slides",
            "real-time",
            "realtime",
        )
    )

    def has_any(key: str) -> bool:
        return any(token in normalized_text for token in get_runtime_csv_list(key))

    multiplier = 1.0
    reasons: list[str] = []

    if has_any("editorial_penalty_investment_keywords_csv") or event_type == "funding_round":
        multiplier -= get_runtime_float("editorial_penalty_investment_weight", default=0.18)
        reasons.append("penalty:investment")
    if domain == "finance_investing":
        multiplier -= 0.08
        reasons.append("penalty:finance_domain")
    if has_any("editorial_penalty_chip_keywords_csv") or domain in {"research", "industrial"}:
        multiplier -= get_runtime_float("editorial_penalty_chip_weight", default=0.16)
        reasons.append("penalty:chips")
    if has_any("editorial_penalty_layoff_keywords_csv"):
        multiplier -= get_runtime_float("editorial_penalty_layoff_weight", default=0.14)
        reasons.append("penalty:layoffs")

    too_technical = has_any("editorial_penalty_too_technical_keywords_csv")
    if features.get("technical_gate") == "failed" or features.get("deep_technical_gate") == "failed":
        too_technical = True
    if domain == "research" and business_it < 0.82 and not practical_product_signal:
        too_technical = True
    if event_type == "incremental_update" and business_it < 0.80 and not practical_product_signal:
        too_technical = True
    if too_technical:
        multiplier -= get_runtime_float("editorial_penalty_too_technical_weight", default=0.20)
        reasons.append("penalty:too_technical")

    if has_any("editorial_bonus_new_tool_keywords_csv"):
        multiplier += get_runtime_float("editorial_bonus_new_tool_weight", default=0.12)
        reasons.append("bonus:new_tool")
    elif event_type in {"paradigm_shift", "market_structure_change", "regulatory_shift"}:
        multiplier += 0.08
        reasons.append("bonus:event_type")
    if has_any("editorial_bonus_new_usage_keywords_csv") or business_it >= 0.86:
        multiplier += get_runtime_float("editorial_bonus_new_usage_weight", default=0.10)
        reasons.append("bonus:new_usage")

    return _clip_multiplier(multiplier), reasons


def _rule_adjusted_score(article: Article, score: Score) -> float:
    _ = article
    return _unified_score_10(score)


def _audience_adjusted_score(article: Article, score: Score) -> float:
    _ = article
    return _unified_score_10(score)


def _get_tz_name() -> str:
    return (telegram_timezone_name() or get_runtime_str("timezone_name") or "Europe/Moscow").strip()


def _hour_bucket_utc(dt_utc: datetime, tz_name: str) -> datetime:
    """
    Compute "hour bucket start" in UTC (naive) aligned to the user's timezone hour.
    dt_utc must be naive UTC datetime.
    """
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Moscow")
    local = dt_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    local_bucket = local.replace(minute=0, second=0, microsecond=0)
    bucket_utc = local_bucket.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    return bucket_utc


def _previous_completed_hour_bucket_utc(now_utc: datetime, tz_name: str) -> datetime:
    # Align to user's timezone and always return the previous completed hour bucket.
    return _hour_bucket_utc(now_utc - timedelta(hours=1), tz_name)


def _current_local_hour(now_utc: datetime | None = None) -> int:
    now_utc = now_utc or datetime.utcnow()
    tz_name = _get_tz_name()
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Europe/Moscow")
    local = now_utc.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    return int(local.hour)


def _resolve_hourly_selection_strategy(now_utc: datetime | None = None) -> str:
    mapping_raw = (get_runtime_str("hourly_slot_strategy_csv", default="") or "").strip()
    default_strategy = (get_runtime_str("hourly_default_selection_strategy", default="ml") or "ml").strip().lower()
    hour = _current_local_hour(now_utc)
    mapping: dict[int, str] = {}
    for chunk in mapping_raw.split(","):
        item = chunk.strip()
        if not item or ":" not in item:
            continue
        hh, strategy = item.split(":", 1)
        try:
            hour_key = int(hh.strip())
        except ValueError:
            continue
        strategy_key = strategy.strip().lower()
        if 0 <= hour_key <= 23 and strategy_key in {"script", "ml", "off"}:
            mapping[hour_key] = strategy_key
    resolved = mapping.get(hour, default_strategy if default_strategy in {"script", "ml", "off"} else "ml")
    if resolved == "ml":
        interval_hours = int(max(1, round(get_runtime_float("ml_review_every_n_hours", default=2.0))))
        if (int(hour) % interval_hours) != 0:
            return "off"
    return resolved


def _ml_candidate_score(article: Article, score: Score) -> tuple[float, dict]:
    features = score.features if isinstance(score.features, dict) else {}
    ml_meta = predict_editor_choice_prob(features)
    if not ml_meta.get("ok"):
        # Strict ML mode: if model is not ready, candidate is not eligible.
        return 0.0, {
            "mode": "ml_unavailable",
            "confidence": None,
            "ml_meta": ml_meta,
            "eligible": False,
        }
    ml_prob = float(ml_meta.get("prob") or 0.0)
    return ml_prob, {
        "mode": "ml_strict",
        "confidence": ml_prob,
        "ml_meta": ml_meta,
        "eligible": True,
    }


def _title_fallback_key(article: Article) -> str:
    src = ""
    try:
        src = str((article.source.name if article.source else "") or "").lower().strip()
    except Exception:
        src = ""
    t = _normalize_text(article.title or "")
    # Keep first tokens to collapse near-identical wire duplicates
    # ("Mistral CEO says X..." vs "... according to Bloomberg")
    tokens = [x for x in t.split(" ") if x][:12]
    return f"{src}|{' '.join(tokens)}"


def _is_incomplete_candidate(article: Article, score: Score | None = None, *, mode: str = "auto") -> bool:
    content_mode = str(article.content_mode or "").strip().lower()
    if content_mode == "summary_only":
        return True
    text = str(article.text or "").strip()
    subtitle = str(article.subtitle or "").strip()
    source_name = ""
    try:
        source_name = str(article.source.name or "").strip().lower()
    except Exception:
        source_name = ""
    if not text and not subtitle:
        return True
    low = text.lower()
    if low.startswith("article url:") and ("comments url:" in low) and len(text) < 500:
        return True
    if "hacker news" in source_name:
        meta_only_re = re.compile(
            r"^\s*author:\s*.+?\|\s*points:\s*\d+\s*\|\s*tags:\s*.+$",
            re.IGNORECASE | re.DOTALL,
        )
        text_meta_only = bool(meta_only_re.match(text))
        subtitle_meta_only = bool(meta_only_re.match(subtitle))
        if (text_meta_only or not text) and (subtitle_meta_only or not subtitle):
            return True
    if len(text) < 220 and len(subtitle) < 60:
        return True
    if score is not None:
        eligible, _ = article_is_selection_eligible(article, score, mode=mode, source_name=source_name)
        if not eligible:
            return True
    return False


def _hourly_candidates(limit: int = 50, hours_window: int = 1) -> list[tuple[Article, Score]]:
    now = datetime.utcnow()
    tz_name = _get_tz_name()
    bucket_start = _previous_completed_hour_bucket_utc(now, tz_name)
    bucket_end = bucket_start + timedelta(hours=1)
    window_hours = max(1, int(hours_window or 1))
    primary_start = bucket_end - timedelta(hours=window_hours)
    day_ago = now - timedelta(hours=24)
    two_days_ago = now - timedelta(hours=48)

    with session_scope() as session:
        selected_clusters = set(
            row[0]
            for row in session.execute(
                select(Article.cluster_key).where(
                    or_(
                        Article.status == ArticleStatus.PUBLISHED,
                        Article.selected_hour_bucket_utc >= day_ago,
                    ),
                    Article.cluster_key.is_not(None),
                )
            ).all()
        )
        recent_selected_or_published = session.scalars(
            select(Article)
            .where(
                or_(
                    Article.status == ArticleStatus.PUBLISHED,
                    Article.selected_hour_bucket_utc >= day_ago,
                ),
            )
            .options(joinedload(Article.source))
            .limit(1000)
        ).all()
        selected_title_keys = {_title_fallback_key(a) for a in recent_selected_or_published}

        base = (
            select(Article, Score)
            .join(Score, Score.article_id == Article.id)
            .where(
                Article.status.in_([ArticleStatus.SCORED, ArticleStatus.REVIEW, ArticleStatus.READY]),
                Article.status != ArticleStatus.DOUBLE,
            )
            .options(joinedload(Article.source))
            .order_by(Score.final_score.desc())
            .limit(limit)
        )
        def _filter_rows(rows: list[tuple[Article, Score]]) -> list[tuple[Article, Score]]:
            filtered: list[tuple[Article, Score]] = []
            for a, s in rows:
                if _is_incomplete_candidate(a, s, mode="auto"):
                    continue
                if a.cluster_key and a.cluster_key in selected_clusters:
                    continue
                if (not a.cluster_key) and (_title_fallback_key(a) in selected_title_keys):
                    continue
                filtered.append((a, s))
            filtered.sort(key=lambda x: _audience_adjusted_score(x[0], x[1]), reverse=True)
            return filtered

        window_filters = [
            base.where(
                ((Article.created_at >= primary_start) & (Article.created_at < bucket_end))
                | ((Article.published_at >= primary_start) & (Article.published_at < bucket_end))
            ),
            base.where((Article.created_at >= day_ago) | (Article.published_at >= day_ago)),
            base.where((Article.created_at >= two_days_ago) | (Article.published_at >= two_days_ago)),
        ]

        for query in window_filters:
            rows = session.execute(query).all()
            filtered = _filter_rows(rows)
            if filtered:
                return filtered

    return []


def _choose_with_profile(candidates: list[tuple[Article, Score]], top_n: int = 3) -> tuple[int, dict]:
    ranked = candidates[:top_n]
    if not ranked:
        return 0, {"mode": "none", "reason": "no_candidates"}

    profile = get_active_profile()
    if not profile or not settings.openrouter_api_key:
        return ranked[0][0].id, {
            "mode": "fallback_by_score",
            "top_drivers": ranked[0][1].features.get("top_drivers", []) if ranked[0][1].features else [],
        }

    payload = []
    for a, s in ranked:
        payload.append(
            {
                "article_id": a.id,
                "title": a.title,
                "source": a.source.name if a.source else "Unknown",
                "score": s.final_score,
                "features": s.features,
            }
        )

    prompt = f"""
Choose 1 article for this hour.
Preference profile:
{profile}

Candidates:
{json.dumps(payload, ensure_ascii=False)}

Return JSON only:
{{"article_id": int, "confidence": 0-1, "reason": "short"}}
"""
    try:
        client = get_client()
        resp = client.chat.completions.create(
            model=settings.llm_text_model,
            response_format={"type": "json_object"},
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
        )
        track_usage_from_response(resp, operation="pipeline.choose_with_profile", model=settings.llm_text_model, kind="chat")
        data = json.loads(resp.choices[0].message.content or "{}")
        picked = int(data.get("article_id") or ranked[0][0].id)
        confidence = float(data.get("confidence", 0.5))
        if picked not in {x[0].id for x in ranked}:
            picked = ranked[0][0].id
        return picked, {"mode": "profile", "confidence": confidence, "reason": data.get("reason", "")}
    except Exception:
        return ranked[0][0].id, {
            "mode": "fallback_by_score_on_error",
            "top_drivers": ranked[0][1].features.get("top_drivers", []) if ranked[0][1].features else [],
        }


def pick_hourly_top(strategy: str | None = None) -> int | None:
    resolved_strategy = (strategy or _resolve_hourly_selection_strategy()).strip().lower()
    if resolved_strategy == "off":
        return None
    interval_hours = int(max(1, round(get_runtime_float("ml_review_every_n_hours", default=2.0))))
    candidates = _hourly_candidates(limit=50, hours_window=(interval_hours if resolved_strategy == "ml" else 1))
    if not candidates:
        return None

    if resolved_strategy == "script":
        ranked = sorted(candidates, key=lambda x: _rule_adjusted_score(x[0], x[1]), reverse=True)
        top3 = ranked[:3]
        if not top3:
            return None
        selected_id = int(top3[0][0].id)
        explain = {
            "mode": "script",
            "confidence": None,
            "selector_kind": "script",
        }
    elif resolved_strategy == "ml":
        min_confidence = float(get_runtime_float("ml_review_min_confidence", default=0.72))
        scored_candidates: list[tuple[Article, Score, float, dict]] = []
        for article, score in candidates:
            if str(article.content_mode or "summary_only").strip().lower() == "summary_only":
                continue
            ml_score, ml_explain = _ml_candidate_score(article, score)
            if not bool(ml_explain.get("eligible", True)):
                continue
            scored_candidates.append((article, score, ml_score, ml_explain))
        scored_candidates.sort(key=lambda x: x[2], reverse=True)
        gated_candidates = [
            (article, score, ml_score, ml_explain)
            for article, score, ml_score, ml_explain in scored_candidates
            if float(ml_explain.get("confidence") or 0.0) >= min_confidence
        ]
        if gated_candidates:
            top3 = [(article, score) for article, score, _, _ in gated_candidates[:3]]
            selected_id = int(top3[0][0].id)
            top_ml_meta = gated_candidates[0][3]
            explain = {
                "mode": "ml",
                "confidence": top_ml_meta.get("confidence"),
                "selector_kind": "ml",
                "model_version": ((top_ml_meta.get("ml_meta") or {}).get("version")),
            }
        else:
            if not scored_candidates:
                return None
            # Do not leave the slot empty when we already found the best candidate
            # across 2h -> 24h -> 48h windows. In low-news periods we accept the top
            # ML-ranked article even below the strict threshold and annotate the choice.
            top3 = [(article, score) for article, score, _, _ in scored_candidates[:3]]
            selected_id = int(top3[0][0].id)
            fallback_ml_meta = scored_candidates[0][3]
            explain = {
                "mode": "ml_fallback_low_confidence",
                "confidence": fallback_ml_meta.get("confidence"),
                "selector_kind": "ml",
                "model_version": ((fallback_ml_meta.get("ml_meta") or {}).get("version")),
                "min_confidence": min_confidence,
            }
    else:
        top3 = candidates[:3]
        selected_id, explain = _choose_with_profile(top3, top_n=3)
        explain["selector_kind"] = "profile" if explain.get("mode") == "profile" else "script"

    with session_scope() as session:
        article = session.get(Article, selected_id)
        if not article:
            return None
        if article.status not in {ArticleStatus.REVIEW, ArticleStatus.SCORED, ArticleStatus.READY}:
            article.status = ArticleStatus.READY
        # Save the hour-bucket so we can backfill 24h later and avoid duplicates per hour.
        tz_name = _get_tz_name()
        article.selected_hour_bucket_utc = _previous_completed_hour_bucket_utc(datetime.utcnow(), tz_name)
        article.updated_at = datetime.utcnow()

    rejected = [a.id for a, _ in top3 if a.id != selected_id]
    candidate_payload = []
    for a, s in top3:
        editorial_mult, editorial_reasons = _editorial_score_multiplier(a, s)
        candidate_payload.append(
            {
                "article_id": a.id,
                "score": s.final_score,
                "editorial_multiplier": editorial_mult,
                "editorial_reasons": editorial_reasons,
                "top_drivers": (s.features or {}).get("top_drivers", []),
                "novelty_reason": (s.features or {}).get("novelty_reason", ""),
            }
        )

    save_selection_decision(
        chosen_article_id=selected_id,
        rejected_article_ids=rejected,
        decision_mode=DecisionMode.AUTO if explain.get("selector_kind") == "ml" else DecisionMode.MANUAL,
        confidence=explain.get("confidence"),
        candidates=candidate_payload,
        selector_kind=explain.get("selector_kind"),
    )
    try:
        log_training_event(
            article_id=selected_id,
            decision="top_pick",
            label=1,
            reason_text=None,
            reason_tags=None,
            override=False,
        )
    except Exception:
        pass

    return selected_id


def pick_hourly_backfill(hours_back: int = 24, per_hour: int = 1) -> dict:
    """
    Backfill Selected Hour for the last N hours: pick up to `per_hour` articles for each
    hour bucket (aligned to user's timezone), skipping already-filled buckets.

    Returns: {"ok": True, "hours": int, "selected": int, "filled_buckets": int, "selected_ids": [...]}
    """
    hours = max(1, min(int(hours_back or 24), 168))  # cap at 7 days
    per_hour_n = max(1, min(int(per_hour or 1), 3))
    tz_name = _get_tz_name()
    now_utc = datetime.utcnow()
    current_bucket = _hour_bucket_utc(now_utc, tz_name)

    selected_ids: list[int] = []
    filled_buckets = 0
    missing_buckets: list[datetime] = []

    # Start from the oldest hour so review chat receives messages in chronological order.
    buckets = [current_bucket - timedelta(hours=i) for i in range(hours - 1, -1, -1)]

    # Backward-compat: older rows may have status=selected_hourly but bucket is NULL.
    # Assign a bucket from updated_at so they can participate in backlog sending and dedup.
    cutoff = now_utc - timedelta(hours=hours)
    with session_scope() as session:
        old_rows = session.scalars(
            select(Article).where(
                Article.status == ArticleStatus.SELECTED_HOURLY,
                Article.selected_hour_bucket_utc.is_(None),
                Article.updated_at >= cutoff,
            )
        ).all()
        for a in old_rows:
            a.selected_hour_bucket_utc = _hour_bucket_utc(a.updated_at or now_utc, tz_name)
            a.status = ArticleStatus.READY
            a.updated_at = datetime.utcnow()

    # Track clusters already selected/published in last 24h + during this run.
    day_ago = now_utc - timedelta(hours=24)
    selected_clusters: set[str] = set()
    with session_scope() as session:
        selected_clusters.update(
            row[0]
            for row in session.execute(
                select(Article.cluster_key).where(
                    or_(
                        Article.status == ArticleStatus.PUBLISHED,
                        Article.selected_hour_bucket_utc >= day_ago,
                    ),
                    Article.cluster_key.is_not(None),
                )
            ).all()
        )

    for bucket_start in buckets:
        bucket_end = bucket_start + timedelta(hours=1)
        with session_scope() as session:
            exists = session.scalars(
                select(Article.id).where(
                    Article.selected_hour_bucket_utc == bucket_start,
                    Article.status != ArticleStatus.PUBLISHED,
                    Article.status != ArticleStatus.ARCHIVED,
                    Article.status != ArticleStatus.DOUBLE,
                    Article.status != ArticleStatus.REJECTED,
                )
            ).first()
            if exists:
                filled_buckets += 1
                continue

            base = (
                select(Article, Score)
                .join(Score, Score.article_id == Article.id)
                .where(
                    Article.status.in_([ArticleStatus.SCORED, ArticleStatus.REVIEW, ArticleStatus.READY]),
                    Article.status != ArticleStatus.DOUBLE,
                    Article.status != ArticleStatus.ARCHIVED,
                    Article.status != ArticleStatus.REJECTED,
                    Article.status != ArticleStatus.PUBLISHED,
                    Article.selected_hour_bucket_utc.is_(None),
                    ((Article.created_at >= bucket_start) & (Article.created_at < bucket_end))
                    | ((Article.published_at >= bucket_start) & (Article.published_at < bucket_end)),
                )
                .options(joinedload(Article.source))
                .order_by(Score.final_score.desc())
                .limit(200)
            )
            rows = session.execute(base).all()

            # If that hour has no candidates, just skip (no forced fallback across hours).
            if not rows:
                missing_buckets.append(bucket_start)
                continue

        # Filter by cluster_key dedup against existing selections/publishes.
        filtered = [
            (a, s)
            for a, s in rows
            if (a.cluster_key not in selected_clusters) and not _is_incomplete_candidate(a, s, mode="auto")
        ]
        if not filtered:
            missing_buckets.append(bucket_start)
            continue
        filtered.sort(key=lambda x: _audience_adjusted_score(x[0], x[1]), reverse=True)

        picked_batch = filtered[:per_hour_n]
        for a, _s in picked_batch:
            with session_scope() as session:
                art = session.get(Article, int(a.id))
                if not art:
                    continue
                # Double-check bucket isn't filled by a parallel request.
                exists2 = session.scalars(
                    select(Article.id).where(
                        Article.selected_hour_bucket_utc == bucket_start,
                        Article.status != ArticleStatus.PUBLISHED,
                        Article.status != ArticleStatus.ARCHIVED,
                        Article.status != ArticleStatus.DOUBLE,
                        Article.status != ArticleStatus.REJECTED,
                    )
                ).first()
                if exists2:
                    break
                if art.status not in {ArticleStatus.REVIEW, ArticleStatus.SCORED, ArticleStatus.READY}:
                    art.status = ArticleStatus.READY
                art.selected_hour_bucket_utc = bucket_start
                art.updated_at = datetime.utcnow()
                selected_ids.append(int(art.id))
                if art.cluster_key:
                    selected_clusters.add(str(art.cluster_key))
                filled_buckets += 1
                # Only 1 per bucket by default; if per_hour>1 we'll fill more.
                if per_hour_n == 1:
                    break

    # If we couldn't fill some hour buckets (low-news hours), top up using best candidates
    # from the whole window so user can still receive ~N messages for N hours.
    if missing_buckets:
        window_start = now_utc - timedelta(hours=hours)
        with session_scope() as session:
            rows2 = session.execute(
                select(Article, Score)
                .join(Score, Score.article_id == Article.id)
                .where(
                    Article.status.in_([ArticleStatus.SCORED, ArticleStatus.REVIEW, ArticleStatus.READY]),
                    Article.status != ArticleStatus.DOUBLE,
                    Article.status != ArticleStatus.ARCHIVED,
                    Article.status != ArticleStatus.REJECTED,
                    Article.status != ArticleStatus.PUBLISHED,
                    Article.selected_hour_bucket_utc.is_(None),
                    (Article.created_at >= window_start) | (Article.published_at >= window_start),
                )
                .options(joinedload(Article.source))
                .order_by(Score.final_score.desc())
                .limit(500)
            ).all()
        pool = [
            (a, s)
            for a, s in rows2
            if (a.cluster_key not in selected_clusters) and not _is_incomplete_candidate(a, s, mode="auto")
        ]
        pool.sort(key=lambda x: _audience_adjusted_score(x[0], x[1]), reverse=True)

        # Fill oldest missing buckets first.
        missing_buckets.sort()
        for bucket_start in list(missing_buckets):
            if not pool:
                break
            a, _s = pool.pop(0)
            with session_scope() as session:
                exists2 = session.scalars(
                    select(Article.id).where(
                        Article.selected_hour_bucket_utc == bucket_start,
                        Article.status != ArticleStatus.PUBLISHED,
                        Article.status != ArticleStatus.ARCHIVED,
                        Article.status != ArticleStatus.DOUBLE,
                        Article.status != ArticleStatus.REJECTED,
                    )
                ).first()
                if exists2:
                    continue
                art = session.get(Article, int(a.id))
                if not art:
                    continue
                if art.status not in {ArticleStatus.REVIEW, ArticleStatus.SCORED, ArticleStatus.READY}:
                    art.status = ArticleStatus.READY
                art.selected_hour_bucket_utc = bucket_start
                art.updated_at = datetime.utcnow()
                selected_ids.append(int(art.id))
                filled_buckets += 1
                if art.cluster_key:
                    selected_clusters.add(str(art.cluster_key))

    return {
        "ok": True,
        "hours": hours,
        "per_hour": per_hour_n,
        "selected": len(selected_ids),
        "filled_buckets": filled_buckets,
        "missing_buckets": max(0, hours - filled_buckets),
        "selected_ids": selected_ids,
        "tz": tz_name,
    }


def auto_select_by_profile(top_n: int = 5) -> dict:
    candidates = _hourly_candidates(limit=max(top_n, 5), hours_window=1)
    if not candidates:
        return {"ok": False, "reason": "no_candidates"}
    selected_id, explain = _choose_with_profile(candidates[:top_n], top_n=top_n)
    return {"ok": True, "article_id": selected_id, **explain}


def run_hourly_cycle(backfill_days: int = 1, select_hourly_top: bool = True) -> dict:
    # Fast ingestion: do not fetch full pages here.
    # Full text is handled by the explicit enrich step.
    def _stage(name: str):
        print("[cycle]", name, flush=True)

    try:
        _stage("ingestion_fast")
        ingest = run_ingestion_fast(days_back=backfill_days, max_entries=200)
    except Exception as exc:
        raise RuntimeError(f"cycle_stage_failed: ingestion_fast: {exc}") from exc

    try:
        _stage("enrich_openai_summary_only")
        openai_enrich = {"scanned": 0, "upgraded_to_full": 0, "still_summary_only": 0, "blocked_http": 0, "thin_or_paywalled": 0}
        if get_runtime_str("openai_hourly_enrich_enabled", default="true").strip().lower() in {"1", "true", "yes", "on"}:
            openai_limit = int(max(1, get_runtime_float("openai_hourly_enrich_limit", default=25.0)))
            openai_days_back = int(max(1, get_runtime_float("openai_hourly_enrich_days_back", default=7.0)))

            def _openai_progress(processed: int, total: int) -> None:
                if total and (processed == total or processed % 5 == 0):
                    print("[cycle] openai_enrich", {"processed": processed, "total": total}, flush=True)

            openai_enrich = enrich_openai_summary_only_articles(
                limit=openai_limit,
                days_back=openai_days_back,
                progress_cb=_openai_progress,
            )
    except Exception as exc:
        raise RuntimeError(f"cycle_stage_failed: enrich_openai_summary_only: {exc}") from exc

    try:
        _stage("enrich_summary_only")
        # Keep hourly cycle fast: do a small enrichment batch only.
        # Full enrichment is available via the UI "Get Full Text" action.
        def _enrich_progress(processed: int, total: int) -> None:
            if total and (processed == total or processed % 10 == 0):
                print("[cycle] enrich", {"processed": processed, "total": total}, flush=True)

        enrich = enrich_summary_only_articles(limit=40, days_back=3, progress_cb=_enrich_progress)
    except Exception as exc:
        raise RuntimeError(f"cycle_stage_failed: enrich_summary_only: {exc}") from exc

    embedded = 0
    embedded_error = None
    try:
        _stage("dedup_embeddings")
        embedded = process_embeddings_and_dedup(limit=250)
    except Exception as exc:
        # Non-fatal fallback: if embeddings API fails, continue the cycle so scoring + hourly review still work.
        embedded_error = str(exc)
        print("[cycle] dedup_embeddings_warning", {"error": embedded_error[:300]}, flush=True)

    try:
        _stage("scoring")
        def _score_progress(processed: int, total: int) -> None:
            if total and (processed == total or processed % 25 == 0):
                print("[cycle] scoring", {"processed": processed, "total": total}, flush=True)

        scored = run_scoring(limit=200, progress_cb=_score_progress)
    except Exception as exc:
        raise RuntimeError(f"cycle_stage_failed: scoring: {exc}") from exc

    top_id = None
    selection_strategy = _resolve_hourly_selection_strategy()
    if select_hourly_top:
        try:
            _stage("pick_hourly_top")
            if selection_strategy != "off":
                top_id = pick_hourly_top(strategy=selection_strategy)
        except Exception as exc:
            raise RuntimeError(f"cycle_stage_failed: pick_hourly_top: {exc}") from exc
    else:
        _stage("pick_hourly_top_skipped")

    if top_id:
        try:
            _stage("prepare_ru_summary")
            generate_ru_summary(top_id)
        except Exception as exc:
            raise RuntimeError(f"cycle_stage_failed: prepare_ru_summary: {exc}") from exc
        try:
            _stage("prepare_image_card")
            generate_image_card(top_id)
        except Exception as exc:
            raise RuntimeError(f"cycle_stage_failed: prepare_image_card: {exc}") from exc

    return {
        "ingestion": ingest,
        "enrich_openai_summary_only": openai_enrich,
        "enrich_summary_only": enrich,
        "embedded": embedded,
        "embedded_error": embedded_error,
        "scored": scored,
        "top_article_id": top_id,
        "selection_strategy": selection_strategy,
        "select_hourly_top": bool(select_hourly_top),
    }
