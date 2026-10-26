import json
import logging
from typing import cast

from starlette.exceptions import HTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.config import Settings
from app.errors import error_body

logger = logging.getLogger(__name__)

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


def _compact_json(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


async def send_json_error(send: Send, status_code: int, error: str) -> None:
    payload = _compact_json(error_body(error))
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
    """Reescribe el rechazo de CORSMiddleware a 403 con el sobre JSON."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        pending_start: Message | None = None
        pending_body = bytearray()

        async def send_wrapper(message: Message) -> None:
            nonlocal pending_start
            if message["type"] == "http.response.start":
                headers = {
                    key.decode("latin-1").lower(): value.decode("latin-1")
                    for key, value in (message.get("headers") or [])
                }
                content_type = headers.get("content-type", "")
                if message["status"] == 400 and content_type.startswith("text/plain"):
                    pending_start = message
                    return
                headers_list = _with_version(list(message.get("headers") or []))
                await send({**message, "headers": headers_list})
                return

            if pending_start is not None and message["type"] == "http.response.body":
                pending_body.extend(message.get("body") or b"")
                if message.get("more_body"):
                    return
                text = pending_body.decode("utf-8", errors="replace")
                if text.startswith("Disallowed CORS"):
                    await send_json_error(send, 403, "forbidden")
                    return
                headers_list = _with_version(list(pending_start.get("headers") or []))
                await send({**pending_start, "headers": headers_list})
                await send(
                    {
                        "type": "http.response.body",
                        "body": bytes(pending_body),
                        "more_body": False,
                    }
                )
                return

            await send(message)

        await self.app(scope, receive, send_wrapper)


class ApiErrorMiddleware:
    """ASGI puro: añade X-API-Version y captura excepciones no manejadas."""

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.settings = settings

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started = False

        async def send_with_version(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                headers = _with_version(list(message.get("headers") or []))
                message = {**message, "headers": headers}
            await send(message)

        try:
            await self.app(scope, receive, send_with_version)
        except HTTPException:
            raise
        except Exception as exc:
            logger.exception("Excepción no manejada")
            if started:
                raise
            payload = _compact_json(
                error_body(
                    "internal_error",
                    message=str(exc) if self.settings.expose_errors else None,
                )
            )
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

    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        self.app = app
        self.max_body_bytes = settings.max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        limit = self.max_body_bytes
        content_length = _header_map(scope).get("content-length")
        if content_length is not None:
            try:
                declared = int(content_length)
            except ValueError:
                declared = 0
            if declared > limit:
                await send_json_error(send, 413, "payload_too_large")
                return

        total = 0
        body_finished = False

        async def limited_receive() -> Message:
            nonlocal total, body_finished
            if body_finished:
                return await receive()
            message = await receive()
            if message["type"] != "http.request":
                return message
            data = message.get("body", b"")
            if not isinstance(data, (bytes, bytearray)):
                data = b""
            total += len(data)
            if total > limit:
                raise HTTPException(status_code=413, detail="payload_too_large")
            if not message.get("more_body"):
                body_finished = True
            return message

        await self.app(scope, limited_receive, send)
