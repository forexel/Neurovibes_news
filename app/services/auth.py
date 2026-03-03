from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import jwt
from passlib.context import CryptContext
from sqlalchemy import select, text

from app.core.config import settings
from app.db import session_scope
from app.models import User, UserRole


pwd_context = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


@dataclass(slots=True)
class SessionUser:
    id: int
    email: str
    password_hash: str
    role: UserRole
    is_active: bool = True


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    return pwd_context.verify(password, password_hash)


def _coerce_role(role: str | UserRole | None) -> UserRole:
    if isinstance(role, UserRole):
        return role
    try:
        return UserRole(str(role or UserRole.EDITOR.value))
    except Exception:
        return UserRole.EDITOR


def create_access_token(user: User | SessionUser) -> str:
    exp = datetime.utcnow() + timedelta(minutes=settings.jwt_ttl_minutes)
    role = getattr(user, "role", UserRole.EDITOR)
    role_value = role.value if isinstance(role, UserRole) else str(role)
    payload = {"sub": str(user.id), "email": user.email, "role": role_value, "exp": exp}
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algo)


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algo])


def get_user_by_email(email: str) -> SessionUser | None:
    with session_scope() as session:
        row = session.execute(
            text(
                "SELECT id, email, password_hash, role, is_active "
                "FROM public.users "
                "WHERE lower(email) = :email "
                "LIMIT 1"
            ),
            {"email": (email or "").strip().lower()},
        ).mappings().first()
        if row is None:
            return None
        return SessionUser(
            id=int(row["id"]),
            email=str(row["email"]),
            password_hash=str(row["password_hash"] or ""),
            role=_coerce_role(row["role"]),
            is_active=bool(row["is_active"]),
        )


def get_user_by_id(user_id: int) -> SessionUser | None:
    with session_scope() as session:
        row = session.execute(
            text(
                "SELECT id, email, password_hash, role, is_active "
                "FROM public.users "
                "WHERE id = :user_id "
                "LIMIT 1"
            ),
            {"user_id": int(user_id)},
        ).mappings().first()
        if row is None:
            return None
        return SessionUser(
            id=int(row["id"]),
            email=str(row["email"]),
            password_hash=str(row["password_hash"] or ""),
            role=_coerce_role(row["role"]),
            is_active=bool(row["is_active"]),
        )


def create_user(email: str, password: str, role: UserRole = UserRole.EDITOR) -> SessionUser:
    email_norm = (email or "").strip().lower()
    password_hash = hash_password(password)
    with session_scope() as session:
        row = session.execute(
            text(
                "INSERT INTO public.users (email, password_hash, role, is_active, created_at) "
                "VALUES (:email, :password_hash, :role, :is_active, NOW()) "
                "RETURNING id, email, password_hash, role, is_active"
            ),
            {
                "email": email_norm,
                "password_hash": password_hash,
                "role": role.value,
                "is_active": True,
            },
        ).mappings().first()
        return SessionUser(
            id=int(row["id"]),
            email=str(row["email"]),
            password_hash=str(row["password_hash"] or ""),
            role=_coerce_role(row["role"]),
            is_active=bool(row["is_active"]),
        )


def ensure_admin_user() -> bool:
    with session_scope() as session:
        existing = session.execute(
            text("SELECT id FROM public.users WHERE lower(email) = :email LIMIT 1"),
            {"email": (settings.admin_email or "").strip().lower()},
        ).first()
        if existing:
            return False
        session.execute(
            text(
                "INSERT INTO public.users (email, password_hash, role, is_active, created_at) "
                "VALUES (:email, :password_hash, :role, :is_active, NOW())"
            ),
            {
                "email": (settings.admin_email or "").strip().lower(),
                "password_hash": hash_password(settings.admin_password),
                "role": UserRole.ADMIN.value,
                "is_active": True,
            },
        )
        return True
