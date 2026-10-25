import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest
from pydantic import SecretStr

from app.auth.tokens import TokenError, decode_session_token, encode_session_token

_SECRET = "real-auth-secret-must-be-used-32ch+"
_PLACEHOLDER = "**********"


def test_token_is_signed_with_secret_value_not_placeholder() -> None:
    secret = SecretStr(_SECRET)
    assert str(secret) == _PLACEHOLDER
    now = datetime.now(UTC)
    token = encode_session_token(
        secret=secret,
        user_id=uuid.uuid4(),
        session_id=uuid.uuid4(),
        issued_at=now,
        expires_at=now + timedelta(hours=1),
    )

    payload = jwt.decode(token, _SECRET, algorithms=["HS256"])
    assert payload["sub"]
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, _PLACEHOLDER, algorithms=["HS256"])

    forged = jwt.encode(
        {
            "sub": str(uuid.uuid4()),
            "jti": str(uuid.uuid4()),
            "exp": now + timedelta(hours=1),
        },
        _PLACEHOLDER,
        algorithm="HS256",
    )
    with pytest.raises(TokenError):
        decode_session_token(forged, secret)
    claims = decode_session_token(token, secret)
    assert claims.user_id
