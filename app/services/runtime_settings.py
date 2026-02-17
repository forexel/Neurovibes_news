from __future__ import annotations

import os
import threading
import time
from datetime import datetime
from typing import Any, Literal

from sqlalchemy import select

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
    # Telegram (fallback defaults; primary config is per-user workspace)
    "telegram_review_chat_id": os.getenv("TELEGRAM_REVIEW_CHAT_ID", "").strip(),
    "telegram_channel_id": os.getenv("TELEGRAM_CHANNEL_ID", "").strip(),
    "telegram_signature": os.getenv("TELEGRAM_SIGNATURE", "@neuro_vibes_future").strip() or "@neuro_vibes_future",
    "timezone_name": os.getenv("TIMEZONE_NAME", "Europe/Moscow").strip() or "Europe/Moscow",
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
    "TELEGRAM_REVIEW_CHAT_ID": "telegram_review_chat_id",
    "TELEGRAM_CHANNEL_ID": "telegram_channel_id",
    "TELEGRAM_SIGNATURE": "telegram_signature",
    "TIMEZONE_NAME": "timezone_name",
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
        with session_scope() as session:
            rows = session.scalars(select(RuntimeSetting)).all()
            for row in rows:
                key = (row.key or "").strip().lower()
                if not key:
                    continue
                scope = (row.scope or "global").strip().lower()
                topic = _normalize_topic_key(row.topic_key)
                new_map[(scope, topic, key)] = row.value or ""
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
        q = select(RuntimeSetting)
        if scope:
            q = q.where(RuntimeSetting.scope == str(scope))
        if topic_key is not None:
            q = q.where(RuntimeSetting.topic_key == _normalize_topic_key(topic_key))
        rows = session.scalars(q.order_by(RuntimeSetting.scope.asc(), RuntimeSetting.topic_key.asc(), RuntimeSetting.key.asc())).all()
        return [
            {
                "id": int(r.id),
                "scope": r.scope,
                "topic_key": r.topic_key,
                "key": r.key,
                "value": r.value,
                "updated_at": r.updated_at.isoformat() if r.updated_at else None,
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
        row = session.scalars(
            select(RuntimeSetting).where(
                RuntimeSetting.scope == scope_n,
                RuntimeSetting.topic_key == topic_n,
                RuntimeSetting.key == key_n,
            )
        ).first()
        if row is None:
            row = RuntimeSetting(scope=scope_n, topic_key=topic_n, key=key_n, value=str(value or ""))
            session.add(row)
            session.flush()
        else:
            row.value = str(value or "")
            row.updated_at = datetime.utcnow()
        out = {
            "id": int(row.id),
            "scope": row.scope,
            "topic_key": row.topic_key,
            "key": row.key,
            "value": row.value,
        }
    clear_runtime_settings_cache()
    return out


def delete_runtime_setting(setting_id: int) -> bool:
    with session_scope() as session:
        row = session.get(RuntimeSetting, setting_id)
        if row is None:
            return False
        session.delete(row)
    clear_runtime_settings_cache()
    return True
