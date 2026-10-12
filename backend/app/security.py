import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from pwdlib import PasswordHash
import jwt
from .config import settings

password_hasher = PasswordHash.recommended()


def hash_password(password: str) -> str:
    return password_hasher.hash(password)


def verify_password(password: str, encoded: str) -> bool:
    try:
        return password_hasher.verify(password, encoded)
    except Exception:
        return False


def make_access_token(user_id: int, username: str, role: str, stage: str | None) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": str(user_id), "username": username, "role": role, "stage": stage,
                       "type": "access", "iat": now, "exp": now + timedelta(minutes=settings.access_token_minutes)},
                      settings.jwt_secret, algorithm="HS256")


def make_refresh_token(user_id: int) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode({"sub": str(user_id), "type": "refresh", "jti": secrets.token_urlsafe(24),
                       "iat": now, "exp": now + timedelta(days=settings.refresh_token_days)},
                      settings.jwt_refresh_secret, algorithm="HS256")


def decode_token(token: str, refresh: bool = False) -> dict:
    return jwt.decode(token, settings.jwt_refresh_secret if refresh else settings.jwt_secret,
                      algorithms=["HS256"])


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()
