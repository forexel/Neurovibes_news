from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select, text
from sqlalchemy.exc import SQLAlchemyError

from app.db import session_scope
from app.models import RuntimeSetting

Scope = Literal["global", "topic"]


RUNTIME_DEFAULTS: dict[str, str] = {
    "max_summary_chars": "1400",
    "max_title_chars": "130",
    "max_overview_chars": "260",
    "ru_preview_enabled": "true",
    "ru_preview_days_back": "14",
    "ru_preview_fill_limit": "120",
    "banned_phrases_csv": "шок!,ты не поверишь",
    "ai_prefilter_enabled": "true",
    "ai_prefilter_keywords_csv": (
        "ai,artificial intelligence,llm,large language model,generative,ai agent,ai agents,"
        "openai,anthropic,deepmind,chatgpt,gemini,claude,transformer,fine-tuning,model inference,"
        "neural,chips,compute,gpu,nvidia,robotics"
    ),
    "min_relevance_for_content": "7",
    "technical_filter_enabled": "true",
    "technical_filter_business_it_max": "7.4",
    "technical_filter_significance_max": "8.8",
    "deep_technical_filter_enabled": "true",
    "deep_technical_filter_business_it_max": "8.5",
    "deep_technical_filter_significance_max": "9.2",
    "deep_technical_filter_relevance_max": "9.4",
    "deep_technical_keywords_csv": (
        "benchmark,parameterized logical,sat,petri net,theorem,proof,ablation,architecture search,"
        "diffusion equation,formal verification,retrosynthesis"
    ),
    "investing_filter_enabled": "true",
    "investing_filter_business_it_max": "8.0",
    "investing_filter_significance_max": "8.9",
    "investing_filter_relevance_max": "9.0",
    "mass_audience_filter_enabled": "true",
    "mass_audience_significance_max": "9.1",
    "mass_audience_relevance_max": "9.2",
    "mass_audience_business_it_max": "8.6",
    "mass_audience_wow_keywords_csv": (
        "new version,release,launched,rollout,available now,security,privacy,data breach,safety,"
        "major update,first in world,breakthrough,wow"
    ),
    "browser_fetch_enabled": "true",
    "browser_fetch_domains_csv": "forbes.com,wired.com,bloomberg.com,ft.com,businessinsider.com,openai.com",
    "browser_cookies_json": "",
    "openai_hourly_enrich_enabled": "true",
    "openai_hourly_enrich_limit": "25",
    "openai_hourly_enrich_days_back": "7",
    # Telegram (fallback defaults; primary config is per-user workspace)
    "telegram_review_chat_id": os.getenv("TELEGRAM_REVIEW_CHAT_ID", "").strip(),
    "telegram_channel_id": os.getenv("TELEGRAM_CHANNEL_ID", "").strip(),
    "telegram_signature": os.getenv("TELEGRAM_SIGNATURE", "@neuro_vibes_future").strip() or "@neuro_vibes_future",
    "timezone_name": os.getenv("TIMEZONE_NAME", "Europe/Moscow").strip() or "Europe/Moscow",
    # ML ranking blend (0..0.8). Start low and increase gradually as dataset grows.
    "ml_editor_choice_weight": "0.10",
    "ml_review_every_n_hours": "2",
    "ml_review_min_confidence": "0.72",
    "ml_recommend_publish_threshold": "0.72",
    "ml_recommend_delete_threshold": "0.28",
    # User-vector retrieval ranking (pos - alpha * neg)
    "user_vector_alpha": "0.60",
    "user_vector_window_days": "30",
    "user_vector_half_life_days": "7",
    "user_vector_max_events": "500",
    "user_vector_manual_weight": "4",
    "user_vector_positive_statuses_csv": "published,selected_hourly",
    "user_vector_negative_statuses_csv": "deleted,archived,rejected",
    "user_vector_split_by_content_type": "true",
    # Auto disable low-value sources in Sources page
    "source_auto_disable_enabled": "true",
    "source_auto_disable_days": "14",
    "source_auto_disable_min_attempts": "12",
    # Review queue hygiene
    "unsorted_recent_days": "3",
    "daily_cleanup_enabled": "true",
    "daily_cleanup_archive_summary_only": "true",
    "daily_cleanup_hide_old_unsorted_days": "3",
    # Hour-end article selection strategy.
    # Examples: "09:script,10:ml,11:off"
    "hourly_slot_strategy_csv": os.getenv("HOURLY_SLOT_STRATEGY_CSV", "").strip(),
    "hourly_default_selection_strategy": os.getenv("HOURLY_DEFAULT_SELECTION_STRATEGY", "ml").strip() or "ml",
    # Editorial candidate bias: penalize repetitive/pop topics, boost usable product news.
    "editorial_penalty_investment_keywords_csv": (
        "funding,investment,investor,valuation,raises,raised,raise,$,billion,million,round,series a,series b,"
        "series c,seed round,acquire,acquires,acquisition,stake,merger,deal,private credit,public company,"
        "market cap,shares,stock jumps,profit hits record,revenue surge,revenue jumps"
    ),
    "editorial_penalty_chip_keywords_csv": (
        "chip,chips,gpu,gpus,compute,data center,datacenter,semiconductor,server,servers,nvidia,amd,blackwell,"
        "rack,cluster,ai factory,processor,processors,infrastructure surge,ai infrastructure,compute capacity,"
        "training cluster"
    ),
    "editorial_penalty_layoff_keywords_csv": (
        "layoff,layoffs,cut jobs,cuts jobs,job cuts,slash jobs,slashes jobs,workforce reduction,firing,firings,"
        "staff cuts,headcount,laid off,job losses,restructuring"
    ),
    "editorial_penalty_too_technical_keywords_csv": (
        "benchmark,latency,throughput,token context,context window,lora,hypernetwork,embedding,quantization,"
        "kernel,weights,inference stack,training stack,attention,architecture,ablation,ranker,eval,leaderboard,"
        "parameters,parameter count,distillation,zero-shot,fine-tuning,app store ranking,retrieval"
    ),
    "editorial_bonus_new_tool_keywords_csv": (
        "launches,launched,releases,released,introduces,introduced,rolls out,new tool,new tools,new app,new agent,"
        "assistant,copilot,plugin,plugins,api,sdk,feature available,now available,ships,shipping today,"
        "available to users,available in app"
    ),
    "editorial_bonus_new_usage_keywords_csv": (
        "use case,workflow,for teams,for business,for users,lets users,lets developers,used to,helps teams,"
        "can now,integrates with,automation,automate,practical,real-world,real world,adoption,using ai in work,"
        "productivity,save time"
    ),
    "editorial_penalty_investment_weight": "0.24",
    "editorial_penalty_chip_weight": "0.22",
    "editorial_penalty_layoff_weight": "0.18",
    "editorial_penalty_too_technical_weight": "0.28",
    "editorial_bonus_new_tool_weight": "0.16",
    "editorial_bonus_new_usage_weight": "0.14",
    "editorial_min_multiplier": "0.45",
    "editorial_max_multiplier": "1.25",
}


ENV_TO_RUNTIME: dict[str, str] = {
    "MAX_SUMMARY_CHARS": "max_summary_chars",
    "MAX_TITLE_CHARS": "max_title_chars",
    "MAX_OVERVIEW_CHARS": "max_overview_chars",
    "BANNED_PHRASES": "banned_phrases_csv",
    "AI_PREFILTER_ENABLED": "ai_prefilter_enabled",
    "AI_PREFILTER_KEYWORDS": "ai_prefilter_keywords_csv",
    "MIN_RELEVANCE_FOR_CONTENT": "min_relevance_for_content",
    "TECHNICAL_FILTER_ENABLED": "technical_filter_enabled",
    "TECHNICAL_FILTER_BUSINESS_IT_MAX": "technical_filter_business_it_max",
    "TECHNICAL_FILTER_SIGNIFICANCE_MAX": "technical_filter_significance_max",
    "DEEP_TECHNICAL_FILTER_ENABLED": "deep_technical_filter_enabled",
    "DEEP_TECHNICAL_FILTER_BUSINESS_IT_MAX": "deep_technical_filter_business_it_max",
    "DEEP_TECHNICAL_FILTER_SIGNIFICANCE_MAX": "deep_technical_filter_significance_max",
    "DEEP_TECHNICAL_FILTER_RELEVANCE_MAX": "deep_technical_filter_relevance_max",
    "DEEP_TECHNICAL_KEYWORDS": "deep_technical_keywords_csv",
    "INVESTING_FILTER_ENABLED": "investing_filter_enabled",
    "INVESTING_FILTER_BUSINESS_IT_MAX": "investing_filter_business_it_max",
    "INVESTING_FILTER_SIGNIFICANCE_MAX": "investing_filter_significance_max",
    "INVESTING_FILTER_RELEVANCE_MAX": "investing_filter_relevance_max",
    "MASS_AUDIENCE_FILTER_ENABLED": "mass_audience_filter_enabled",
    "MASS_AUDIENCE_SIGNIFICANCE_MAX": "mass_audience_significance_max",
    "MASS_AUDIENCE_RELEVANCE_MAX": "mass_audience_relevance_max",
    "MASS_AUDIENCE_BUSINESS_IT_MAX": "mass_audience_business_it_max",
    "MASS_AUDIENCE_WOW_KEYWORDS": "mass_audience_wow_keywords_csv",
    "BROWSER_FETCH_ENABLED": "browser_fetch_enabled",
    "BROWSER_FETCH_DOMAINS": "browser_fetch_domains_csv",
    "BROWSER_COOKIES_JSON": "browser_cookies_json",
    "OPENAI_HOURLY_ENRICH_ENABLED": "openai_hourly_enrich_enabled",
    "OPENAI_HOURLY_ENRICH_LIMIT": "openai_hourly_enrich_limit",
    "OPENAI_HOURLY_ENRICH_DAYS_BACK": "openai_hourly_enrich_days_back",
    "TELEGRAM_REVIEW_CHAT_ID": "telegram_review_chat_id",
    "TELEGRAM_CHANNEL_ID": "telegram_channel_id",
    "TELEGRAM_SIGNATURE": "telegram_signature",
    "TIMEZONE_NAME": "timezone_name",
    "UNSORTED_RECENT_DAYS": "unsorted_recent_days",
    "DAILY_CLEANUP_ENABLED": "daily_cleanup_enabled",
    "DAILY_CLEANUP_ARCHIVE_SUMMARY_ONLY": "daily_cleanup_archive_summary_only",
    "DAILY_CLEANUP_HIDE_OLD_UNSORTED_DAYS": "daily_cleanup_hide_old_unsorted_days",
    "HOURLY_SLOT_STRATEGY_CSV": "hourly_slot_strategy_csv",
    "HOURLY_DEFAULT_SELECTION_STRATEGY": "hourly_default_selection_strategy",
    "EDITORIAL_PENALTY_INVESTMENT_KEYWORDS": "editorial_penalty_investment_keywords_csv",
    "EDITORIAL_PENALTY_CHIP_KEYWORDS": "editorial_penalty_chip_keywords_csv",
    "EDITORIAL_PENALTY_LAYOFF_KEYWORDS": "editorial_penalty_layoff_keywords_csv",
    "EDITORIAL_PENALTY_TOO_TECHNICAL_KEYWORDS": "editorial_penalty_too_technical_keywords_csv",
    "EDITORIAL_BONUS_NEW_TOOL_KEYWORDS": "editorial_bonus_new_tool_keywords_csv",
    "EDITORIAL_BONUS_NEW_USAGE_KEYWORDS": "editorial_bonus_new_usage_keywords_csv",
    "EDITORIAL_PENALTY_INVESTMENT_WEIGHT": "editorial_penalty_investment_weight",
    "EDITORIAL_PENALTY_CHIP_WEIGHT": "editorial_penalty_chip_weight",
    "EDITORIAL_PENALTY_LAYOFF_WEIGHT": "editorial_penalty_layoff_weight",
    "EDITORIAL_PENALTY_TOO_TECHNICAL_WEIGHT": "editorial_penalty_too_technical_weight",
    "EDITORIAL_BONUS_NEW_TOOL_WEIGHT": "editorial_bonus_new_tool_weight",
    "EDITORIAL_BONUS_NEW_USAGE_WEIGHT": "editorial_bonus_new_usage_weight",
    "EDITORIAL_MIN_MULTIPLIER": "editorial_min_multiplier",
    "EDITORIAL_MAX_MULTIPLIER": "editorial_max_multiplier",
}


_CACHE_TTL_SECONDS = 10.0
_cache_lock = threading.Lock()
_cache_loaded_at = 0.0
_cache_map: dict[tuple[str, str | None, str], str] = {}


def _normalize_topic_key(topic_key: str | None) -> str | None:
    v = (topic_key or "").strip().lower()
    return v or None


def _cache_refresh(force: bool = False) -> None:
    global _cache_loaded_at
    global _cache_map
    now = time.time()
    if not force and (now - _cache_loaded_at) < _CACHE_TTL_SECONDS:
        return
    with _cache_lock:
        now = time.time()
        if not force and (now - _cache_loaded_at) < _CACHE_TTL_SECONDS:
            return
        new_map: dict[tuple[str, str | None, str], str] = {}
        try:
            with session_scope() as session:
                rows = session.scalars(select(RuntimeSetting)).all()
                for row in rows:
                    key = (row.key or "").strip().lower()
                    if not key:
                        continue
                    scope = (row.scope or "global").strip().lower()
                    topic = _normalize_topic_key(row.topic_key)
                    new_map[(scope, topic, key)] = row.value or ""
        except SQLAlchemyError:
            new_map = {}
        _cache_map = new_map
        _cache_loaded_at = now


def clear_runtime_settings_cache() -> None:
    _cache_refresh(force=True)


def seed_runtime_settings() -> int:
    inserted = 0
    with session_scope() as session:
        existing_rows = session.scalars(select(RuntimeSetting).where(RuntimeSetting.scope == "global")).all()
        existing_keys = {(r.key or "").strip().lower() for r in existing_rows}

        env_values: dict[str, str] = {}
        for env_key, runtime_key in ENV_TO_RUNTIME.items():
            raw = os.getenv(env_key)
            if raw is not None and str(raw).strip() != "":
                env_values[runtime_key] = str(raw).strip()

        for key, default_value in RUNTIME_DEFAULTS.items():
            if key in existing_keys:
                continue
            session.add(
                RuntimeSetting(
                    scope="global",
                    topic_key=None,
                    key=key,
                    value=env_values.get(key, default_value),
                )
            )
            inserted += 1
    clear_runtime_settings_cache()
    return inserted


def _resolve_raw_value(key: str, topic_key: str | None = None, default: str | None = None) -> str:
    key_n = (key or "").strip().lower()
    if not key_n:
        return default or ""
    _cache_refresh(force=False)
    topic_n = _normalize_topic_key(topic_key)
    if topic_n is not None:
        hit = _cache_map.get(("topic", topic_n, key_n))
        if hit is not None:
            return hit
    hit = _cache_map.get(("global", None, key_n))
    if hit is not None:
        return hit
    if default is not None:
        return str(default)
    return str(RUNTIME_DEFAULTS.get(key_n, ""))


def get_runtime_str(key: str, topic_key: str | None = None, default: str | None = None) -> str:
    return _resolve_raw_value(key=key, topic_key=topic_key, default=default)


def get_runtime_bool(key: str, topic_key: str | None = None, default: bool | None = None) -> bool:
    fallback = None if default is None else ("true" if default else "false")
    raw = _resolve_raw_value(key=key, topic_key=topic_key, default=fallback).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def get_runtime_int(key: str, topic_key: str | None = None, default: int | None = None) -> int:
    fallback = None if default is None else str(default)
    raw = _resolve_raw_value(key=key, topic_key=topic_key, default=fallback).strip()
    try:
        return int(float(raw))
    except Exception:
        try:
            return int(float(RUNTIME_DEFAULTS.get(key, "0")))
        except Exception:
            return int(default or 0)


def get_runtime_float(key: str, topic_key: str | None = None, default: float | None = None) -> float:
    fallback = None if default is None else str(default)
    raw = _resolve_raw_value(key=key, topic_key=topic_key, default=fallback).strip()
    try:
        return float(raw)
    except Exception:
        try:
            return float(RUNTIME_DEFAULTS.get(key, "0"))
        except Exception:
            return float(default or 0.0)


def get_runtime_csv_list(key: str, topic_key: str | None = None, default: list[str] | None = None) -> list[str]:
    fallback = ",".join(default or [])
    raw = _resolve_raw_value(key=key, topic_key=topic_key, default=fallback)
    return [x.strip() for x in str(raw).split(",") if x.strip()]


def list_runtime_settings(scope: Scope | None = None, topic_key: str | None = None) -> list[dict[str, Any]]:
    with session_scope() as session:
        where = []
        params: dict[str, Any] = {}
        if scope:
            where.append("scope = :scope")
            params["scope"] = str(scope)
        if topic_key is not None:
            where.append("topic_key = :topic_key")
            params["topic_key"] = _normalize_topic_key(topic_key)
        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        rows = session.execute(
            text(
                "SELECT id, scope, topic_key, key, value, updated_at "
                f"FROM public.runtime_settings {where_sql} "
                "ORDER BY scope ASC, topic_key ASC NULLS FIRST, key ASC"
            ),
            params,
        ).mappings().all()
        return [
            {
                "id": int(r["id"]),
                "scope": r["scope"],
                "topic_key": r["topic_key"],
                "key": r["key"],
                "value": r["value"],
                "updated_at": r["updated_at"].isoformat() if r["updated_at"] else None,
            }
            for r in rows
        ]


def upsert_runtime_setting(key: str, value: str, scope: Scope = "global", topic_key: str | None = None) -> dict[str, Any]:
    key_n = (key or "").strip().lower()
    if not key_n:
        raise ValueError("key_required")
    scope_n = (scope or "global").strip().lower()
    topic_n = _normalize_topic_key(topic_key) if scope_n == "topic" else None
    if scope_n == "topic" and not topic_n:
        raise ValueError("topic_key_required_for_topic_scope")

    with session_scope() as session:
        row = session.execute(
            text(
                "SELECT id FROM public.runtime_settings "
                "WHERE scope = :scope AND key = :key "
                "AND (topic_key IS NOT DISTINCT FROM :topic_key) "
                "LIMIT 1"
            ),
            {"scope": scope_n, "key": key_n, "topic_key": topic_n},
        ).first()
        if row is None:
            created = session.execute(
                text(
                    "INSERT INTO public.runtime_settings (scope, topic_key, key, value, updated_at) "
                    "VALUES (:scope, :topic_key, :key, :value, :updated_at) RETURNING id"
                ),
                {
                    "scope": scope_n,
                    "topic_key": topic_n,
                    "key": key_n,
                    "value": str(value or ""),
                    "updated_at": datetime.utcnow(),
                },
            ).first()
            row_id = int(created[0])
        else:
            row_id = int(row[0])
            session.execute(
                text(
                    "UPDATE public.runtime_settings "
                    "SET value = :value, updated_at = :updated_at "
                    "WHERE id = :id"
                ),
                {"id": row_id, "value": str(value or ""), "updated_at": datetime.utcnow()},
            )
        out = {
            "id": row_id,
            "scope": scope_n,
            "topic_key": topic_n,
            "key": key_n,
            "value": str(value or ""),
        }
    clear_runtime_settings_cache()
    return out


def delete_runtime_setting(setting_id: int) -> bool:
    with session_scope() as session:
        row = session.execute(
            text("DELETE FROM public.runtime_settings WHERE id = :id RETURNING id"),
            {"id": int(setting_id)},
        ).first()
        if row is None:
            return False
    clear_runtime_settings_cache()
    return True
