from __future__ import annotations

from fastapi import Depends, Header, HTTPException
from sqlalchemy import select

from app.db import session_scope
from app.models import User, UserRole
from app.services.auth import decode_token


def get_current_user(authorization: str | None = Header(default=None)) -> User:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="missing_bearer_token")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = decode_token(token)
        user_id = int(payload.get("sub"))
    except Exception as exc:
        raise HTTPException(status_code=401, detail="invalid_token") from exc

    with session_scope() as session:
        user = session.scalars(select(User).where(User.id == user_id, User.is_active.is_(True))).first()
    if not user:
        raise HTTPException(status_code=401, detail="user_not_found")
    return user


def require_roles(*roles: UserRole):
    role_values = {r.value for r in roles}

    def dep(user: User = Depends(get_current_user)) -> User:
        if user.role.value not in role_values:
            raise HTTPException(status_code=403, detail="forbidden")
        return user

    return dep
