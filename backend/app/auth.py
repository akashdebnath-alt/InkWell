import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from .config import settings
from .db import User, get_db

TOKEN_LIFETIME_SECONDS = 60 * 60 * 24 * 7
_bearer = HTTPBearer(auto_error=False)
_runtime_secret = secrets.token_bytes(32)


def _jwt_secret() -> bytes:
    configured_secret = settings.jwt_secret_key.strip()
    if configured_secret:
        return configured_secret.encode("utf-8")
    return _runtime_secret


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    password_hash = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=16384, r=8, p=1, dklen=32
    )
    return f"scrypt$16384$8$1${salt.hex()}${password_hash.hex()}"


def verify_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, n, r, p, salt, expected_hash = stored_hash.split("$")
        if algorithm != "scrypt":
            return False
        salt_bytes = bytes.fromhex(salt)
        expected_bytes = bytes.fromhex(expected_hash)
        actual_hash = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt_bytes,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected_bytes),
        )
    except (ValueError, TypeError):
        return False
    return secrets.compare_digest(actual_hash, expected_bytes)


def create_access_token(user_id: int) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(user_id),
            "iat": now,
            "exp": now + timedelta(seconds=TOKEN_LIFETIME_SECONDS),
        },
        _jwt_secret(),
        algorithm="HS256",
    )


def _user_id_from_token(token: str) -> int:
    try:
        claims = jwt.decode(
            token,
            _jwt_secret(),
            algorithms=["HS256"],
            options={"require": ["exp", "sub"]},
        )
        user_id = int(claims["sub"])
        if user_id <= 0:
            raise ValueError("Invalid user")
        return user_id
    except (jwt.InvalidTokenError, ValueError, TypeError, KeyError):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    user_id = _user_id_from_token(credentials.credentials)
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired access token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user
