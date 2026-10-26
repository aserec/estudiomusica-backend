"""JWT HS256 de sesión. Nunca registrar el token ni el secreto."""

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import jwt
from fastapi import Request
from pydantic import SecretStr

from app.config import Settings

_ALGORITHM = "HS256"


class TokenError(Exception):
    """Token ausente, caducado o manipulado. El detalle no se expone."""


@dataclass(frozen=True, slots=True)
class SessionClaims:
    user_id: uuid.UUID
    session_id: uuid.UUID
    expires_at: datetime


def _raw_secret(secret: str | SecretStr) -> str:
    if isinstance(secret, SecretStr):
        return secret.get_secret_value()
    return secret


def encode_session_token(
    *,
    secret: str | SecretStr,
    user_id: uuid.UUID,
    session_id: uuid.UUID,
    issued_at: datetime,
    expires_at: datetime,
) -> str:
    payload = {
        "sub": str(user_id),
        "jti": str(session_id),
        "iat": issued_at,
        "exp": expires_at,
    }
    return jwt.encode(payload, _raw_secret(secret), algorithm=_ALGORITHM)


def decode_session_token(token: str, secret: str | SecretStr) -> SessionClaims:
    try:
        payload = jwt.decode(
            token,
            _raw_secret(secret),
            algorithms=[_ALGORITHM],
            options={"require": ["sub", "jti", "exp"]},
        )
    except jwt.PyJWTError as exc:
        raise TokenError from exc

    try:
        user_id = uuid.UUID(str(payload["sub"]))
        session_id = uuid.UUID(str(payload["jti"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise TokenError from exc

    exp = payload.get("exp")
    if not isinstance(exp, int | float):
        raise TokenError
    expires_at = datetime.fromtimestamp(exp, tz=UTC)
    return SessionClaims(user_id=user_id, session_id=session_id, expires_at=expires_at)


def extract_access_token(request: Request, settings: Settings) -> str | None:
    authorization = request.headers.get("authorization")
    if authorization is not None:
        scheme, separator, value = authorization.partition(" ")
        if scheme.lower() == "bearer":
            token = value.strip()
            return token or None
        if separator:
            return None
    cookie = request.cookies.get(settings.auth_cookie_name)
    if cookie:
        return cookie
    return None
