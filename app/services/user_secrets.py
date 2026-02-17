from __future__ import annotations

import base64
import hashlib
from typing import Final

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import settings


_SALT: Final[bytes] = b"neurovibes-news:user-secrets:v1"


def _fernet() -> Fernet:
    # Derive a stable symmetric key from JWT_SECRET (so one env var controls both).
    secret = (settings.jwt_secret or "").encode("utf-8")
    key = hashlib.sha256(_SALT + secret).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def encrypt_secret(plain: str) -> str:
    p = (plain or "").strip()
    if not p:
        return ""
    return _fernet().encrypt(p.encode("utf-8")).decode("utf-8")


def decrypt_secret(cipher: str) -> str:
    c = (cipher or "").strip()
    if not c:
        return ""
    try:
        return _fernet().decrypt(c.encode("utf-8")).decode("utf-8")
    except InvalidToken:
        return ""

