"""Registro, login, logout y resolución de la sesión actual."""

from datetime import UTC, datetime, timedelta

from fastapi import Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.passwords import dummy_hash_for, hash_password, verify_password
from app.auth.tokens import (
    TokenError,
    decode_session_token,
    encode_session_token,
    extract_access_token,
)
from app.config import Settings
from app.exceptions import (
    AccountSuspended,
    EmailTaken,
    InvalidCredentials,
    Unauthorized,
    Validation,
)
from app.models import AuthSession, User
from app.models.user import PERSON_NAME_MAX, SystemRole
from app.schemas.auth import LogInRequest, PublicUser, SignUpRequest
from app.services.roles import assert_role_assignable


def split_display_name(name: str) -> tuple[str, str]:
    parts = name.split(None, 1)
    first = parts[0]
    last = parts[1] if len(parts) > 1 else ""
    if len(first) > PERSON_NAME_MAX or len(last) > PERSON_NAME_MAX:
        raise Validation(fields=["name"])
    return first, last


def to_public_user(user: User) -> PublicUser:
    return PublicUser(
        id=user.id,
        name=user.display_name,
        email=user.email,
        first_name=user.first_name,
        last_name=user.last_name,
        avatar_file_id=user.avatar_file_id,
        email_notifications_enabled=user.email_notifications_enabled,
    )


def _is_inactive(user: User) -> bool:
    return user.suspended_at is not None or user.deleted_at is not None


async def create_user_session(
    session: AsyncSession, settings: Settings, user: User
) -> tuple[AuthSession, str]:
    now = datetime.now(UTC)
    expires_at = now + timedelta(seconds=settings.auth_session_ttl_seconds)
    auth_session = AuthSession(user_id=user.id, expires_at=expires_at)
    session.add(auth_session)
    await session.flush()
    token = encode_session_token(
        secret=settings.auth_secret,
        user_id=user.id,
        session_id=auth_session.id,
        issued_at=now,
        expires_at=expires_at,
    )
    return auth_session, token


async def register_user(
    session: AsyncSession, settings: Settings, body: SignUpRequest
) -> tuple[User, str]:
    email = str(body.email)
    existing = await session.scalar(select(User).where(User.email == email))
    if existing is not None:
        raise EmailTaken()

    first_name, last_name = split_display_name(body.name)
    role_id = (
        SystemRole.ADMIN
        if email in settings.bootstrap_admin_email_set()
        else SystemRole.ALUMNO
    )
    await assert_role_assignable(session, role_id)
    user = User(
        first_name=first_name,
        last_name=last_name,
        email=email,
        password_hash=await hash_password(body.password, settings),
        system_role_id=role_id,
        is_admin=role_id == SystemRole.ADMIN,
        email_notifications_enabled=True,
    )
    try:
        async with session.begin_nested():
            session.add(user)
            await session.flush()
    except IntegrityError as exc:
        raise EmailTaken() from exc

    _auth_session, token = await create_user_session(session, settings, user)
    return user, token


async def login_user(
    session: AsyncSession, settings: Settings, body: LogInRequest
) -> tuple[User, str]:
    email = str(body.email)
    user = await session.scalar(select(User).where(User.email == email))
    password_hash = user.password_hash if user is not None else dummy_hash_for(settings)
    matches = await verify_password(body.password, password_hash)
    if user is None or not matches:
        raise InvalidCredentials()
    if _is_inactive(user):
        raise AccountSuspended()
    _auth_session, token = await create_user_session(session, settings, user)
    return user, token


def _session_is_active(auth_session: AuthSession) -> bool:
    now = datetime.now(UTC)
    return (
        auth_session.revoked_at is None
        and auth_session.deleted_at is None
        and auth_session.expires_at > now
    )


async def resolve_current_user(
    request: Request, session: AsyncSession, settings: Settings
) -> User:
    token = extract_access_token(request, settings)
    if token is None:
        raise Unauthorized()
    try:
        claims = decode_session_token(token, settings.auth_secret)
    except TokenError as exc:
        raise Unauthorized() from exc

    auth_session = await session.get(AuthSession, claims.session_id)
    if (
        auth_session is None
        or auth_session.user_id != claims.user_id
        or not _session_is_active(auth_session)
    ):
        raise Unauthorized()

    user = await session.get(User, claims.user_id)
    if user is None:
        raise Unauthorized()
    if _is_inactive(user):
        raise AccountSuspended()
    return user


async def revoke_current_session(
    request: Request, session: AsyncSession, settings: Settings
) -> None:
    token = extract_access_token(request, settings)
    if token is None:
        return
    try:
        claims = decode_session_token(token, settings.auth_secret)
    except TokenError:
        return
    auth_session = await session.get(AuthSession, claims.session_id)
    if auth_session is None or auth_session.revoked_at is not None:
        return
    auth_session.revoked_at = datetime.now(UTC)
    await session.flush()
