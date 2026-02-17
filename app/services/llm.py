from openai import OpenAI

from app.core.config import settings
from app.db import session_scope
from app.models import LLMUsageLog


def get_client() -> OpenAI:
    return OpenAI(
        api_key=settings.openrouter_api_key,
        base_url=settings.openrouter_base_url,
        timeout=settings.openai_timeout_seconds,
        max_retries=settings.openai_max_retries,
    )


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
