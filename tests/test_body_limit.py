import asyncio
from typing import Any

from fastapi.testclient import TestClient
from starlette.types import Message, Receive, Scope


def _run_asgi(
    app: Any,
    *,
    method: str,
    path: str,
    headers: list[tuple[bytes, bytes]],
    receive: Receive,
) -> list[Message]:
    messages: list[Message] = []

    async def send(message: Message) -> None:
        messages.append(message)

    scope: Scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": headers,
        "client": ("testclient", 50000),
        "server": ("testserver", 80),
    }

    asyncio.run(app(scope, receive, send))
    return messages


def _json_error(messages: list[Message]) -> tuple[int, dict[str, object]]:
    start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    import json

    return int(start["status"]), json.loads(body.decode("utf-8"))


def test_content_length_over_limit_rejected_before_read(
    small_body_client: TestClient,
) -> None:
    async def receive() -> Message:
        raise AssertionError("el cuerpo no debe leerse si Content-Length excede")

    messages = _run_asgi(
        small_body_client.app,
        method="POST",
        path="/api/v1/examples",
        headers=[
            (b"host", b"test"),
            (b"content-type", b"application/json"),
            (b"content-length", b"1073741824"),
        ],
        receive=receive,
    )
    status, body = _json_error(messages)
    assert status == 413
    assert body == {"error": "payload_too_large"}


def test_chunked_body_over_limit_returns_413(small_body_client: TestClient) -> None:
    chunks = [b'{"displayName":"', b"x" * 80, b'"}']
    index = 0

    async def receive() -> Message:
        nonlocal index
        if index >= len(chunks):
            return {"type": "http.request", "body": b"", "more_body": False}
        data = chunks[index]
        index += 1
        return {
            "type": "http.request",
            "body": data,
            "more_body": index < len(chunks),
        }

    messages = _run_asgi(
        small_body_client.app,
        method="POST",
        path="/api/v1/examples",
        headers=[
            (b"host", b"test"),
            (b"content-type", b"application/json"),
        ],
        receive=receive,
    )
    status, body = _json_error(messages)
    assert status == 413
    assert body == {"error": "payload_too_large"}


def test_http_client_body_over_limit_returns_413(small_body_client: TestClient) -> None:
    response = small_body_client.post(
        "/api/v1/examples",
        content=b'{"displayName":"' + b"x" * 80 + b'"}',
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == {"error": "payload_too_large"}
    assert response.headers["x-api-version"] == "v1"
    assert b": " not in response.content


def test_content_length_at_limit_is_accepted(small_body_client: TestClient) -> None:
    payload = b"x" * 64
    response = small_body_client.post(
        "/api/v1/_test/echo-size",
        content=payload,
        headers={"content-type": "application/octet-stream"},
    )

    assert response.status_code == 200
    assert response.json() == {"size": 64}


def test_content_length_over_limit_by_one_is_413(small_body_client: TestClient) -> None:
    async def receive() -> Message:
        raise AssertionError("el cuerpo no debe leerse si Content-Length excede")

    messages = _run_asgi(
        small_body_client.app,
        method="POST",
        path="/api/v1/_test/echo-size",
        headers=[
            (b"host", b"test"),
            (b"content-type", b"application/octet-stream"),
            (b"content-length", b"65"),
        ],
        receive=receive,
    )
    status, body = _json_error(messages)
    assert status == 413
    assert body == {"error": "payload_too_large"}
