import asyncio
from typing import Any

from fastapi.testclient import TestClient
from starlette.types import Message, Scope

from tests.conftest import PROBE_FILE_BYTES


def _run_asgi_open(
    app: Any,
    *,
    method: str,
    path: str,
    headers: list[tuple[bytes, bytes]],
    body: bytes = b"",
) -> list[Message]:
    """ASGI call whose receive no se desconecta al terminar el cuerpo."""

    messages: list[Message] = []
    sent_body = False
    keep_open = asyncio.Event()

    async def receive() -> Message:
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": body, "more_body": False}
        await keep_open.wait()
        return {"type": "http.disconnect"}

    async def send(message: Message) -> None:
        messages.append(message)
        if message["type"] == "http.response.body" and not message.get(
            "more_body", False
        ):
            keep_open.set()

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

    async def main() -> None:
        await app(scope, receive, send)
        keep_open.set()

    asyncio.run(main())
    return messages


def test_streaming_response_sends_all_five_chunks(client: TestClient) -> None:
    messages = _run_asgi_open(
        client.app,
        method="GET",
        path="/api/v1/_test/stream",
        headers=[(b"host", b"test")],
    )
    chunks = [
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body" and message.get("body")
    ]
    assert b"".join(chunks) == b"chunk0chunk1chunk2chunk3chunk4"
    assert len(chunks) >= 5


def test_is_disconnected_is_false_after_reading_post(client: TestClient) -> None:
    messages = _run_asgi_open(
        client.app,
        method="POST",
        path="/api/v1/_test/disconnect",
        headers=[
            (b"host", b"test"),
            (b"content-type", b"text/plain"),
            (b"content-length", b"5"),
        ],
        body=b"hello",
    )
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    import json

    assert json.loads(body.decode("utf-8")) == {"disconnected": False}


def test_file_response_sends_full_body(client: TestClient) -> None:
    messages = _run_asgi_open(
        client.app,
        method="GET",
        path="/api/v1/_test/file",
        headers=[(b"host", b"test")],
    )
    body = b"".join(
        message.get("body", b"")
        for message in messages
        if message["type"] == "http.response.body"
    )
    assert body == PROBE_FILE_BYTES
    start = next(
        message for message in messages if message["type"] == "http.response.start"
    )
    assert start["status"] == 200
