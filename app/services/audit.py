from __future__ import annotations

from app.db import session_scope
from app.models import AuditLog


def audit(action: str, entity_type: str, entity_id: str, payload: dict | None = None, user_id: int | None = None) -> None:
    with session_scope() as session:
        session.add(
            AuditLog(
                user_id=user_id,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                payload=payload,
            )
        )
