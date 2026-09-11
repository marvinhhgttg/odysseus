"""Per-request correlation ID for HTTP responses and log records."""

from __future__ import annotations

import contextvars
import logging
import re
import time
import uuid
from contextlib import contextmanager
from typing import Iterator

from src.metrics import request_finished, request_started

_REQUEST_ID = contextvars.ContextVar("odysseus_request_id", default="-")
_AGENT_RUN_ID = contextvars.ContextVar("odysseus_agent_run_id", default="-")
_TASK_RUN_ID = contextvars.ContextVar("odysseus_task_run_id", default="-")
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def current_request_id() -> str:
    return _REQUEST_ID.get()


def current_agent_run_id() -> str:
    return _AGENT_RUN_ID.get()


def current_task_run_id() -> str:
    return _TASK_RUN_ID.get()


@contextmanager
def correlation_context(
    *,
    request_id: str | None = None,
    agent_run_id: str | None = None,
    task_run_id: str | None = None,
) -> Iterator[None]:
    """Temporarily bind correlation identifiers to the current async context."""
    request_token = (
        _REQUEST_ID.set(request_id) if request_id is not None else None
    )
    run_token = (
        _AGENT_RUN_ID.set(agent_run_id)
        if agent_run_id is not None
        else None
    )
    task_run_token = (
        _TASK_RUN_ID.set(task_run_id)
        if task_run_id is not None
        else None
    )
    try:
        yield
    finally:
        if task_run_token is not None:
            _TASK_RUN_ID.reset(task_run_token)
        if run_token is not None:
            _AGENT_RUN_ID.reset(run_token)
        if request_token is not None:
            _REQUEST_ID.reset(request_token)


def normalize_request_id(value: str | None) -> str:
    candidate = (value or "").strip()
    if candidate and _VALID_REQUEST_ID.fullmatch(candidate):
        return candidate
    return str(uuid.uuid4())


class RequestIdLogFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = current_request_id()
        record.run_id = current_agent_run_id()
        record.task_run_id = current_task_run_id()
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

        started = time.monotonic()
        method = scope.get("method") or ""
        terminal = {"status": 0}
        request_started()

        async def send_with_request_id(message):
            if message.get("type") == "http.response.start":
                terminal["status"] = message["status"]
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
            request_finished(method, terminal["status"], time.monotonic() - started)
