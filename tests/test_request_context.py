import logging

import httpx
import pytest
from fastapi import FastAPI

from src.request_context import (
    RequestIdLogFilter,
    RequestIdMiddleware,
    current_request_id,
)


def _app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestIdMiddleware)

    @app.get("/request-id")
    async def request_id():
        return {"request_id": current_request_id()}

    return app


@pytest.mark.asyncio
async def test_generates_request_id():
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        response = await client.get("/request-id")

    request_id = response.headers["x-request-id"]
    assert request_id
    assert response.json() == {"request_id": request_id}


@pytest.mark.asyncio
async def test_preserves_valid_incoming_request_id():
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        response = await client.get(
            "/request-id",
            headers={"X-Request-ID": "client-request-42"},
        )

    assert response.headers["x-request-id"] == "client-request-42"
    assert response.json() == {"request_id": "client-request-42"}


@pytest.mark.asyncio
async def test_replaces_invalid_incoming_request_id():
    transport = httpx.ASGITransport(app=_app())
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test"
    ) as client:
        response = await client.get(
            "/request-id",
            headers={"X-Request-ID": "invalid request id"},
        )

    assert response.headers["x-request-id"] != "invalid request id"
    assert response.json()["request_id"] == response.headers["x-request-id"]


def test_log_filter_has_safe_default_outside_request():
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "message", (), None
    )
    assert RequestIdLogFilter().filter(record)
    assert record.request_id == "-"
