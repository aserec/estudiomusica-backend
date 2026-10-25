import json
from typing import cast

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import get_settings
from app.errors import error_body

API_VERSION_HEADER = (b"x-api-version", b"v1")


def _header_map(scope: Scope) -> dict[str, str]:
    headers = cast(list[tuple[bytes, bytes]], scope.get("headers") or [])
    return {
        key.decode("latin-1").lower(): value.decode("latin-1") for key, value in headers
    }


def _with_version(headers: list[tuple[bytes, bytes]]) -> list[tuple[bytes, bytes]]:
    if any(key.lower() == b"x-api-version" for key, _ in headers):
        return headers
    return [*headers, API_VERSION_HEADER]


async def send_json_error(send: Send, status_code: int, error: str) -> None:
    payload = json.dumps(error_body(error)).encode("utf-8")
    await send(
        {
            "type": "http.response.start",
            "status": status_code,
            "headers": _with_version(
                [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode("latin-1")),
                ]
            ),
        }
    )
    await send({"type": "http.response.body", "body": payload})


class RejectedPreflightMiddleware:
    """Responde OPTIONS de un origen no permitido con el sobre JSON."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and scope.get("method") == "OPTIONS":
            origin = _header_map(scope).get("origin")
            allowed = get_settings().cors_origin_list()
            if origin is not None and origin not in allowed:
                await send_json_error(send, 403, "forbidden")
                return
        await self.app(scope, receive, send)


class ApiErrorMiddleware:
    """ASGI puro: añade X-API-Version y captura excepciones no manejadas."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_version(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = _with_version(list(message.get("headers") or []))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_version)
        except Exception as exc:
            settings = get_settings()
            payload = json.dumps(
                error_body(
                    "internal_error",
                    message=str(exc) if settings.expose_errors else None,
                )
            ).encode("utf-8")
            await send_with_version(
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(payload)).encode("latin-1")),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": payload})


class BodySizeLimitMiddleware:
    """Rechaza cuerpos grandes por Content-Length o contando el stream."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = get_settings().max_body_bytes
        content_length = _header_map(scope).get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = 0
            if declared > limit:
                await send_json_error(send, 413, "payload_too_large")
                return

        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] != "http.request":
                await self.app(scope, receive, send)
                return
            data = message.get("body", b"")
            if not isinstance(data, (bytes, bytearray)):
                data = b""
            total += len(data)
            if total > limit:
                await send_json_error(send, 413, "payload_too_large")
                return
            chunks.append(bytes(data))
            if not message.get("more_body"):
                break

        body = b"".join(chunks)
        sent = False

        async def replay() -> Message:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        await self.app(scope, replay, send)
