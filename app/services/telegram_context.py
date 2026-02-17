from __future__ import annotations

from contextvars import ContextVar

from app.core.config import settings
from sqlalchemy import select

from app.db import session_scope
from app.models import UserWorkspace
from app.services.runtime_settings import get_runtime_str
from app.services.user_secrets import decrypt_secret


_bot_token_var: ContextVar[str] = ContextVar("tg_bot_token", default="")
_review_chat_var: ContextVar[str] = ContextVar("tg_review_chat_id", default="")
_channel_id_var: ContextVar[str] = ContextVar("tg_channel_id", default="")
_signature_var: ContextVar[str] = ContextVar("tg_signature", default="")
_tz_var: ContextVar[str] = ContextVar("tg_timezone_name", default="")


def set_telegram_context(*, bot_token: str = "", review_chat_id: str = "", channel_id: str = "", signature: str = "", timezone_name: str = "") -> None:
    _bot_token_var.set((bot_token or "").strip())
    _review_chat_var.set((review_chat_id or "").strip())
    _channel_id_var.set((channel_id or "").strip())
    _signature_var.set((signature or "").strip())
    _tz_var.set((timezone_name or "").strip())


def load_workspace_telegram_context(user_id: int | None) -> None:
    # Best-effort. If no user or no workspace config, fall back to global runtime/env.
    bot_token = ""
    review_chat_id = ""
    channel_id = ""
    signature = ""
    timezone_name = ""

    if user_id:
        with session_scope() as session:
            ws = session.scalars(select(UserWorkspace).where(UserWorkspace.user_id == int(user_id))).first()
            if ws is not None:
                bot_token = decrypt_secret((ws.telegram_bot_token_enc or "").strip())
                review_chat_id = (ws.telegram_review_chat_id or "").strip()
                channel_id = (ws.telegram_channel_id or "").strip()
                signature = (ws.telegram_signature or "").strip()
                timezone_name = (ws.timezone_name or "").strip()

    if not bot_token:
        bot_token = (settings.telegram_bot_token or "").strip()
    if not review_chat_id:
        review_chat_id = (get_runtime_str("telegram_review_chat_id") or settings.telegram_review_chat_id or "").strip()
    if not channel_id:
        channel_id = (get_runtime_str("telegram_channel_id") or settings.telegram_channel_id or "").strip()
    if not signature:
        signature = (get_runtime_str("telegram_signature") or settings.telegram_signature or "@neuro_vibes_future").strip()
    if not timezone_name:
        timezone_name = (get_runtime_str("timezone_name") or "Europe/Moscow").strip()

    set_telegram_context(
        bot_token=bot_token,
        review_chat_id=review_chat_id,
        channel_id=channel_id,
        signature=signature,
        timezone_name=timezone_name,
    )


def telegram_bot_token() -> str:
    return (_bot_token_var.get() or "").strip()


def telegram_review_chat_id() -> str:
    return (_review_chat_var.get() or "").strip()


def telegram_channel_id() -> str:
    return (_channel_id_var.get() or "").strip()


def telegram_signature() -> str:
    return (_signature_var.get() or "").strip() or "@neuro_vibes_future"


def telegram_timezone_name() -> str:
    return (_tz_var.get() or "").strip() or "Europe/Moscow"
