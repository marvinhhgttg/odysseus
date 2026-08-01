"""Per-request correlation ID for HTTP responses and log records."""

from __future__ import annotations

import contextvars
import logging
import re
import uuid

_REQUEST_ID = contextvars.ContextVar("odysseus_request_id", default="-")
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def current_request_id() -> str:
    return _REQUEST_ID.get()


def normalize_request_id(value: str | None) -> str:
    candidate = (value or "").strip()
    if candidate and _VALID_REQUEST_ID.fullmatch(candidate):
        return candidate
    return str(uuid.uuid4())


class RequestIdLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id()
        return True


class RequestIdMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        request_id = normalize_request_id(headers.get("x-request-id"))
        token = _REQUEST_ID.set(request_id)

        async def send_with_request_id(message):
            if message.get("type") == "http.response.start":
                response_headers = list(message.get("headers", []))
                response_headers.append(
                    (b"x-request-id", request_id.encode("latin-1"))
                )
                message["headers"] = response_headers
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            _REQUEST_ID.reset(token)
