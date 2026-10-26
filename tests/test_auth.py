import asyncio
import base64
import json
import logging
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.passwords import dummy_hash_for, hash_password, verify_password
from app.auth.tokens import TokenError, decode_session_token, encode_session_token
from app.config import get_settings
from app.db import RedactedDsn
from app.models import AuthSession, User
from app.schemas.auth import SignUpRequest
from tests.conftest import (
    PRODUCTION_AUTH_SECRET,
    PRODUCTION_TEST_USER,
    _create_async_engine,
)

pytestmark = pytest.mark.db

PASSWORD = "password12"
_LEAK_MARKERS = ("password12", "argon2", "test-auth-secret", "Bearer ")


def unique_email(prefix: str = "user") -> str:
    return f"{prefix}-{uuid.uuid4().hex}@example.com"


def signup_body(
    *,
    name: str = "Ada Lovelace",
    email: str | None = None,
    password: str = PASSWORD,
) -> dict[str, str]:
    return {
        "name": name,
        "email": email or unique_email(),
        "password": password,
    }


def assert_public_user(payload: dict[str, Any], *, email: str, name: str) -> None:
    assert set(payload) == {
        "id",
        "name",
        "email",
        "firstName",
        "lastName",
        "avatarFileId",
        "emailNotificationsEnabled",
    }
    assert payload["email"] == email
    assert payload["name"] == name
    assert payload["avatarFileId"] is None
    uuid.UUID(payload["id"])
    for marker in _LEAK_MARKERS:
        assert marker not in str(payload)


def set_cookie_header(response: Any) -> str:
    header = response.headers.get("set-cookie", "")
    return header if isinstance(header, str) else str(header)


def unsigned_alg_none_token(
    *,
    user_id: uuid.UUID | None = None,
    session_id: uuid.UUID | None = None,
) -> str:
    def _b64url(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    now = datetime.now(UTC)
    header = _b64url(b'{"alg":"none","typ":"JWT"}')
    payload = _b64url(
        json.dumps(
            {
                "sub": str(user_id or uuid.uuid4()),
                "jti": str(session_id or uuid.uuid4()),
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(hours=1)).timestamp()),
            },
            separators=(",", ":"),
        ).encode()
    )
    return f"{header}.{payload}."


async def test_signup_creates_alumno_and_session(session_client: Any) -> None:
    email = unique_email("ada")
    response = await session_client.post(
        "/api/v1/auth/signup",
        json=signup_body(email=email),
    )

    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"user"}
    assert_public_user(body["user"], email=email, name="Ada Lovelace")
    assert body["user"]["firstName"] == "Ada"
    assert body["user"]["lastName"] == "Lovelace"
    cookie = set_cookie_header(response)
    assert "session=" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" not in cookie
    assert "samesite=lax" in cookie.lower()
    for marker in _LEAK_MARKERS:
        assert marker not in response.text


async def test_signup_duplicate_email_returns_email_taken(
    session_client: Any,
) -> None:
    email = unique_email("dup")
    first = await session_client.post(
        "/api/v1/auth/signup", json=signup_body(email=email)
    )
    assert first.status_code == 201
    second = await session_client.post(
        "/api/v1/auth/signup",
        json=signup_body(name="Otra Persona", email=email.upper()),
    )
    assert second.status_code == 409
    assert second.json() == {"error": "email_taken"}
    assert PASSWORD not in second.text


async def test_signup_validation_errors(session_client: Any) -> None:
    missing = await session_client.post("/api/v1/auth/signup", json={})
    assert missing.status_code == 400
    assert missing.json()["error"] == "validation"
    assert set(missing.json()["fields"]) >= {"name", "email", "password"}

    bad_email = await session_client.post(
        "/api/v1/auth/signup",
        json=signup_body(email="no-es-un-email"),
    )
    assert bad_email.status_code == 400
    assert bad_email.json() == {"error": "validation", "fields": ["email"]}

    short = await session_client.post(
        "/api/v1/auth/signup",
        json=signup_body(password="123"),
    )
    assert short.status_code == 400
    assert short.json() == {"error": "validation", "fields": ["password"]}

    blank_name = await session_client.post(
        "/api/v1/auth/signup",
        json=signup_body(name="   "),
    )
    assert blank_name.status_code == 400
    assert blank_name.json() == {"error": "validation", "fields": ["name"]}


async def test_signup_and_login_reject_control_and_noncharacters(
    session_client: Any,
) -> None:
    """U+0001 (Cc) y U+FFFF (no-carácter) en name y en texto de login/signup."""

    for raw, field in (("a\u0001b", "name"), ("a\uffffb", "name")):
        response = await session_client.post(
            "/api/v1/auth/signup",
            json=signup_body(name=raw),
        )
        assert response.status_code == 400, raw
        assert response.json() == {"error": "validation", "fields": [field]}

    email = await session_client.post(
        "/api/v1/auth/signup",
        json=signup_body(email="a\u0001b@example.com"),
    )
    assert email.status_code == 400
    assert email.json() == {"error": "validation", "fields": ["email"]}

    password = await session_client.post(
        "/api/v1/auth/signup",
        json=signup_body(password="pass\u0001word12"),
    )
    assert password.status_code == 400
    assert password.json() == {"error": "validation", "fields": ["password"]}

    login_password = await session_client.post(
        "/api/v1/auth/login",
        json={"email": unique_email("cc"), "password": "pass\u0001word"},
    )
    assert login_password.status_code == 400
    assert login_password.json() == {"error": "validation", "fields": ["password"]}


async def test_signup_and_login_reject_nul(session_client: Any) -> None:
    """NUL en texto que llega a Postgres no debe ser 500 internal_error."""

    name = await session_client.post(
        "/api/v1/auth/signup",
        json=signup_body(name="a\u0000b"),
    )
    assert name.status_code == 400
    assert name.json() == {"error": "validation", "fields": ["name"]}

    email = await session_client.post(
        "/api/v1/auth/signup",
        json=signup_body(email="a\u0000b@example.com"),
    )
    assert email.status_code == 400
    assert email.json() == {"error": "validation", "fields": ["email"]}

    password = await session_client.post(
        "/api/v1/auth/signup",
        json=signup_body(password="pass\u0000word12"),
    )
    assert password.status_code == 400
    assert password.json() == {"error": "validation", "fields": ["password"]}
    assert "pass\u0000word12" not in password.text

    login_email = await session_client.post(
        "/api/v1/auth/login",
        json={"email": "a\u0000b@example.com", "password": PASSWORD},
    )
    assert login_email.status_code == 400
    assert login_email.json() == {"error": "validation", "fields": ["email"]}

    login_password = await session_client.post(
        "/api/v1/auth/login",
        json={"email": unique_email("nul"), "password": "pass\u0000word"},
    )
    assert login_password.status_code == 400
    assert login_password.json() == {"error": "validation", "fields": ["password"]}
    assert "pass\u0000word" not in login_password.text


def test_signup_request_rejects_nul() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError) as caught:
        SignUpRequest.model_validate(
            {"name": "a\u0000b", "email": "ada@example.com", "password": PASSWORD}
        )
    assert "name" in {err["loc"][-1] for err in caught.value.errors()}


def test_signup_request_rejects_control_and_noncharacters() -> None:
    from pydantic import ValidationError

    for name in ("a\u0001b", "a\uffffb"):
        with pytest.raises(ValidationError) as caught:
            SignUpRequest.model_validate(
                {"name": name, "email": "ada@example.com", "password": PASSWORD}
            )
        assert "name" in {err["loc"][-1] for err in caught.value.errors()}


def test_signup_request_normalizes_email() -> None:
    body = SignUpRequest.model_validate(
        {"name": "  Ada  ", "email": " Ada@Example.COM ", "password": PASSWORD}
    )
    assert str(body.email) == "ada@example.com"
    assert body.name == "Ada"


async def test_login_returns_session(session_client: Any) -> None:
    email = unique_email("login")
    await session_client.post("/api/v1/auth/signup", json=signup_body(email=email))
    await session_client.post("/api/v1/auth/logout")

    response = await session_client.post(
        "/api/v1/auth/login",
        json={"email": email.upper(), "password": PASSWORD},
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"user"}
    assert_public_user(body["user"], email=email, name="Ada Lovelace")
    assert "session=" in set_cookie_header(response)


async def test_login_invalid_credentials_do_not_reveal_email(
    session_client: Any,
) -> None:
    email = unique_email("hidden")
    await session_client.post("/api/v1/auth/signup", json=signup_body(email=email))

    wrong = await session_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "not-the-password"},
    )
    missing = await session_client.post(
        "/api/v1/auth/login",
        json={"email": unique_email("ghost"), "password": "not-the-password"},
    )
    assert wrong.status_code == 401
    assert missing.status_code == 401
    assert wrong.json() == {"error": "invalid_credentials"}
    assert missing.json() == {"error": "invalid_credentials"}
    assert PASSWORD not in wrong.text
    assert "not-the-password" not in missing.text


async def test_unknown_email_still_verifies_a_dummy_hash(
    session_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: list[str] = []
    original = verify_password

    async def _spy(password: str, password_hash: str) -> bool:
        seen.append(password_hash)
        return await original(password, password_hash)

    monkeypatch.setattr("app.services.auth.verify_password", _spy)
    response = await session_client.post(
        "/api/v1/auth/login",
        json={"email": unique_email("timing"), "password": "whatever1"},
    )
    assert response.status_code == 401
    assert seen == [dummy_hash_for(get_settings())]


async def test_login_suspended_or_deleted_account(
    session_client: Any, db_session: AsyncSession
) -> None:
    settings = get_settings()
    suspended_email = unique_email("suspended")
    deleted_email = unique_email("deleted")
    db_session.add(
        User(
            first_name="Sue",
            last_name="Spendida",
            email=suspended_email,
            password_hash=await hash_password(PASSWORD, settings),
            suspended_at=datetime.now(UTC),
        )
    )
    db_session.add(
        User(
            first_name="Del",
            last_name="Eted",
            email=deleted_email,
            password_hash=await hash_password(PASSWORD, settings),
            deleted_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    suspended = await session_client.post(
        "/api/v1/auth/login",
        json={"email": suspended_email, "password": PASSWORD},
    )
    deleted = await session_client.post(
        "/api/v1/auth/login",
        json={"email": deleted_email, "password": PASSWORD},
    )
    wrong = await session_client.post(
        "/api/v1/auth/login",
        json={"email": suspended_email, "password": "wrong-pass"},
    )
    assert suspended.status_code == 403
    assert suspended.json() == {"error": "account_suspended"}
    assert deleted.status_code == 403
    assert deleted.json() == {"error": "account_suspended"}
    assert wrong.status_code == 401
    assert wrong.json() == {"error": "invalid_credentials"}


async def test_me_returns_public_user_and_system_role(
    session_client: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    email = unique_email("me")
    await session_client.post("/api/v1/auth/signup", json=signup_body(email=email))
    response = await session_client.get("/api/v1/auth/me")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"user", "systemRole", "isAdmin"}
    assert_public_user(body["user"], email=email, name="Ada Lovelace")
    assert body["systemRole"] == "alumno"
    assert body["isAdmin"] is False

    admin_email = unique_email("boss")
    monkeypatch.setenv("BOOTSTRAP_ADMIN_EMAILS", admin_email)
    get_settings.cache_clear()
    await session_client.post(
        "/api/v1/auth/signup",
        json=signup_body(name="Boss Admin", email=admin_email),
    )
    admin_me = await session_client.get("/api/v1/auth/me")
    assert admin_me.status_code == 200
    assert admin_me.json()["systemRole"] == "admin"
    assert admin_me.json()["isAdmin"] is True


async def test_me_without_cookie_is_unauthorized(session_client: Any) -> None:
    response = await session_client.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}


async def test_me_accepts_bearer_token(session_client: Any) -> None:
    email = unique_email("bearer")
    created = await session_client.post(
        "/api/v1/auth/signup", json=signup_body(email=email)
    )
    token = created.cookies["session"]
    session_client.cookies.clear()
    response = await session_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["user"]["email"] == email


async def test_me_rejects_expired_or_tampered_token(session_client: Any) -> None:
    email = unique_email("token")
    created = await session_client.post(
        "/api/v1/auth/signup", json=signup_body(email=email)
    )
    token = created.cookies["session"]
    tampered = token[:-2] + ("A" if token[-1] != "A" else "B")
    expired = encode_session_token(
        secret=get_settings().resolved_auth_secret(),
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        issued_at=datetime.now(UTC) - timedelta(hours=2),
        expires_at=datetime.now(UTC) - timedelta(hours=1),
    )

    for bad in (tampered, expired, "not-a-jwt"):
        response = await session_client.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {bad}"},
        )
        assert response.status_code == 401
        assert response.json() == {"error": "unauthorized"}
        assert bad not in response.text
        assert get_settings().resolved_auth_secret() not in response.text


async def test_logout_revokes_session_and_clears_cookie(session_client: Any) -> None:
    created = await session_client.post(
        "/api/v1/auth/signup", json=signup_body(email=unique_email("out"))
    )
    token = created.cookies["session"]
    logout = await session_client.post("/api/v1/auth/logout")
    assert logout.status_code == 204
    assert logout.content == b""
    assert "session=" in set_cookie_header(logout)

    anonymous = await session_client.get("/api/v1/auth/me")
    assert anonymous.status_code == 401
    replay = await session_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert replay.status_code == 401
    assert replay.json() == {"error": "unauthorized"}
    assert token not in replay.text

    again = await session_client.post("/api/v1/auth/logout")
    assert again.status_code == 204


async def test_me_rejects_suspended_existing_session(
    session_client: Any, db_session: AsyncSession
) -> None:
    email = unique_email("later")
    created = await session_client.post(
        "/api/v1/auth/signup", json=signup_body(email=email)
    )
    user_id = uuid.UUID(created.json()["user"]["id"])
    user = await db_session.get(User, user_id)
    assert user is not None
    user.suspended_at = datetime.now(UTC)
    await db_session.flush()

    response = await session_client.get("/api/v1/auth/me")
    assert response.status_code == 403
    assert response.json() == {"error": "account_suspended"}

    user.suspended_at = None
    user.deleted_at = datetime.now(UTC)
    await db_session.flush()
    deleted = await session_client.get("/api/v1/auth/me")
    assert deleted.status_code == 403
    assert deleted.json() == {"error": "account_suspended"}


async def test_password_hash_is_not_plaintext(
    session_client: Any, db_session: AsyncSession
) -> None:
    email = unique_email("hash")
    created = await session_client.post(
        "/api/v1/auth/signup", json=signup_body(email=email)
    )
    user = await db_session.get(User, uuid.UUID(created.json()["user"]["id"]))
    assert user is not None
    assert user.password_hash.startswith("$argon2")
    assert PASSWORD not in user.password_hash
    assert await verify_password(PASSWORD, user.password_hash)


async def test_failed_login_does_not_log_secrets(
    session_client: Any, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.DEBUG):
        response = await session_client.post(
            "/api/v1/auth/login",
            json={"email": unique_email("log"), "password": "secret-pass"},
        )
    assert response.status_code == 401
    assert "secret-pass" not in caplog.text
    assert get_settings().resolved_auth_secret() not in caplog.text


def test_production_cookie_sets_secure() -> None:
    from fastapi import Response

    from app.auth.cookies import attach_session_cookie
    from app.config import Settings

    settings = Settings(
        app_env="production",
        database_url="postgresql+asyncpg://app:s3cret@db.example:5432/app",
        auth_secret="production-auth-secret-at-least-32ch",
        _env_file=None,
    )
    response = Response()
    attach_session_cookie(response, "dummy-token", settings)
    cookie = set_cookie_header(response)
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "samesite=lax" in cookie.lower()


def test_decode_rejects_alg_none_token() -> None:
    with pytest.raises(TokenError):
        decode_session_token(
            unsigned_alg_none_token(),
            "any-secret-value-not-used-for-alg-none",
        )


async def test_me_rejects_alg_none_token(session_client: Any) -> None:
    token = unsigned_alg_none_token()
    response = await session_client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 401
    assert response.json() == {"error": "unauthorized"}
    assert token not in response.text


def test_production_auth_signup_login_me_logout(
    production_client: TestClient,
) -> None:
    email = unique_email("prod")
    created = production_client.post(
        "/api/v1/auth/signup", json=signup_body(email=email)
    )
    assert created.status_code == 201
    assert_public_user(created.json()["user"], email=email, name="Ada Lovelace")
    cookie = set_cookie_header(created)
    assert "session=" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" in cookie
    assert "samesite=lax" in cookie.lower()
    assert PRODUCTION_AUTH_SECRET not in created.text

    logged_out = production_client.post("/api/v1/auth/logout")
    assert logged_out.status_code == 204

    login = production_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": PASSWORD},
    )
    assert login.status_code == 200
    login_cookie = set_cookie_header(login)
    assert "HttpOnly" in login_cookie
    assert "Secure" in login_cookie
    assert "samesite=lax" in login_cookie.lower()

    me = production_client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"] == email
    assert me.json()["systemRole"] == "alumno"
    assert me.json()["isAdmin"] is False

    boom = production_client.get("/api/v1/_test/boom")
    assert boom.status_code == 500
    assert boom.json() == {"error": "internal_error"}
    assert "message" not in boom.json()
    assert "secret-leak" not in boom.text
    assert PRODUCTION_AUTH_SECRET not in boom.text
    assert "Traceback" not in boom.text

    wrong = production_client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "wrong-password"},
    )
    assert wrong.status_code == 401
    assert wrong.json() == {"error": "invalid_credentials"}
    assert "wrong-password" not in wrong.text
    assert "argon2" not in wrong.text
    assert PRODUCTION_AUTH_SECRET not in wrong.text

    logout = production_client.post("/api/v1/auth/logout")
    assert logout.status_code == 204
    assert production_client.get("/api/v1/auth/me").status_code == 401
    assert production_client.get("/api/v1/examples").status_code == 404


def test_production_test_role_is_not_superuser(
    production_test_database_url: RedactedDsn,
    require_postgres: RedactedDsn,
) -> None:
    async def rolsuper() -> object:
        engine = _create_async_engine(require_postgres.secret())
        try:
            async with engine.connect() as connection:
                return await connection.scalar(
                    text("SELECT rolsuper FROM pg_roles WHERE rolname = :name"),
                    {"name": PRODUCTION_TEST_USER},
                )
        finally:
            await engine.dispose()

    assert asyncio.run(rolsuper()) is False


def test_signup_commit_failure_returns_500_without_cookie(
    monkeypatch: pytest.MonkeyPatch,
    db_client: TestClient,
    migrated_test_database: None,
) -> None:
    """A1/A6/A7: signup real (sin override de _get_session) y commit fallido.

    El override de conftest sustituye _get_session y ocultaba un 201+cookie
    si el commit iba después de responder. SessionDep (scope=function)
    hace el commit antes: 500 y ninguna cookie.
    """

    from sqlalchemy.ext.asyncio import AsyncSession as SAAsyncSession

    async def _boom(self: SAAsyncSession) -> None:
        raise RuntimeError("commit-failed")

    monkeypatch.setattr(SAAsyncSession, "commit", _boom)
    response = db_client.post("/api/v1/auth/signup", json=signup_body())
    assert response.status_code == 500
    cookie = set_cookie_header(response)
    assert "session=" not in cookie.lower()
    assert "set-cookie" not in {key.lower() for key in response.headers}
    assert not response.cookies


async def test_auth_session_row_is_created(
    session_client: Any, db_session: AsyncSession
) -> None:
    created = await session_client.post(
        "/api/v1/auth/signup", json=signup_body(email=unique_email("row"))
    )
    user_id = uuid.UUID(created.json()["user"]["id"])
    rows = (
        await db_session.scalars(
            select(AuthSession).where(AuthSession.user_id == user_id)
        )
    ).all()
    assert len(rows) == 1
