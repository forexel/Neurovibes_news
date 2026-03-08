from contextvars import ContextVar
from datetime import datetime, timedelta

from openai import OpenAI

from sqlalchemy import func, select

from app.core.config import settings
from app.db import session_scope
from app.models import LLMUsageLog, UserWorkspace
from app.services.user_secrets import decrypt_secret


_user_api_key_var: ContextVar[str | None] = ContextVar("nv_user_openrouter_api_key", default=None)


def set_user_api_key(api_key: str | None) -> None:
    v = (api_key or "").strip()
    _user_api_key_var.set(v or None)


def get_workspace_api_key(user_id: int) -> str | None:
    if not user_id:
        return None
    with session_scope() as session:
        ws = session.scalars(select(UserWorkspace).where(UserWorkspace.user_id == int(user_id))).first()
        if ws is None or not (ws.openrouter_api_key_enc or "").strip():
            return None
        return decrypt_secret(ws.openrouter_api_key_enc) or None


def get_client() -> OpenAI:
    # Prefer per-user key (set by request context) to avoid spending platform owner's credits.
    api_key = _user_api_key_var.get() or settings.openrouter_api_key
    return OpenAI(
        api_key=api_key,
        base_url=settings.openrouter_base_url,
        timeout=settings.openai_timeout_seconds,
        max_retries=settings.openai_max_retries,
    )


def llm_budget_allows(operation: str, *, feature: str = "content") -> bool:
    now = datetime.utcnow()
    day_from = now - timedelta(hours=24)
    five_m_from = now - timedelta(minutes=5)
    feature_prefix = f"{feature}."
    with session_scope() as session:
        total_24h = float(
            session.scalar(
                select(func.coalesce(func.sum(LLMUsageLog.estimated_cost_usd), 0.0)).where(LLMUsageLog.created_at >= day_from)
            )
            or 0.0
        )
        feature_24h = float(
            session.scalar(
                select(func.coalesce(func.sum(LLMUsageLog.estimated_cost_usd), 0.0)).where(
                    LLMUsageLog.created_at >= day_from,
                    LLMUsageLog.operation.like(f"{feature_prefix}%"),
                )
            )
            or 0.0
        )
        spike_5m = float(
            session.scalar(
                select(func.coalesce(func.sum(LLMUsageLog.estimated_cost_usd), 0.0)).where(LLMUsageLog.created_at >= five_m_from)
            )
            or 0.0
        )

    if settings.llm_daily_quota_usd > 0 and total_24h >= float(settings.llm_daily_quota_usd):
        return False
    if settings.llm_feature_quota_usd > 0 and feature_24h >= float(settings.llm_feature_quota_usd):
        return False
    if settings.llm_spike_5m_quota_usd > 0 and spike_5m >= float(settings.llm_spike_5m_quota_usd):
        return False
    return True


def track_usage_from_response(resp, operation: str, model: str | None = None, kind: str = "chat") -> None:
    usage = getattr(resp, "usage", None)
    if usage is None:
        return
    try:
        prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        total_tokens = int(getattr(usage, "total_tokens", prompt_tokens + completion_tokens) or 0)
    except Exception:
        return

    m = (model or getattr(resp, "model", "") or "unknown").strip()
    if kind == "embedding":
        est = (prompt_tokens / 1_000_000.0) * float(settings.llm_embedding_input_cost_per_mtok)
    else:
        est = (
            (prompt_tokens / 1_000_000.0) * float(settings.llm_chat_input_cost_per_mtok)
            + (completion_tokens / 1_000_000.0) * float(settings.llm_chat_output_cost_per_mtok)
        )
    with session_scope() as session:
        session.add(
            LLMUsageLog(
                operation=(operation or "unknown")[:128],
                model=m[:128],
                kind=(kind or "chat")[:32],
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                estimated_cost_usd=float(round(est, 8)),
            )
        )
