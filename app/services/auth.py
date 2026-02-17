from __future__ import annotations

from datetime import datetime, timedelta

import jwt
from passlib.context import CryptContext
from sqlalchemy import select

from app.core.config import settings
from app.db import session_scope
from app.models import User, UserRole


pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def create_access_token(user: User) -> str:
    exp = datetime.utcnow() + timedelta(minutes=settings.jwt_ttl_minutes)
    payload = {"sub": str(user.id), "email": user.email, "role": user.role.value, "exp": exp}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algo)


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algo])


def get_user_by_email(email: str) -> User | None:
    with session_scope() as session:
        return session.scalars(select(User).where(User.email == email)).first()


def ensure_admin_user() -> bool:
    with session_scope() as session:
        existing = session.scalars(select(User).where(User.email == settings.admin_email)).first()
        if existing:
            return False
        session.add(
            User(
                email=settings.admin_email,
                password_hash=hash_password(settings.admin_password),
                role=UserRole.ADMIN,
                is_active=True,
            )
        )
        return True
