from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from core.database import ToolApprovalRecord
from routes import chat_routes
from src.tool_approval import (
    ApprovalError,
    ApprovalStatus,
    ToolApproval,
)
from src.tool_approval_store import ToolApprovalStore


SECRET_TOOL_CONTENT = "printf 'route-secret-must-not-leak\\n'"


class FakeApprovalStore:
    def __init__(self):
        self.calls = []
        self.error = None

    def list_for_session(self, *, owner, session_id):
        self.calls.append(("list", session_id, owner))
        if self.error is not None:
            raise self.error
        return [_approval("pending")]

    def approve(self, approval_id, *, owner):
        self.calls.append(("approve", approval_id, owner))
        if self.error is not None:
            raise self.error
        return _approval("approved")

    def reject(self, approval_id, *, owner):
        self.calls.append(("reject", approval_id, owner))
        if self.error is not None:
            raise self.error
        return _approval("rejected")


def _approval(status):
    return SimpleNamespace(
        id="approval-1",
        session_id="session-1",
        run_id="run-1",
        status=SimpleNamespace(value=status),
        expires_at=datetime(2026, 8, 6, 10, 0, tzinfo=timezone.utc),
        tool_name="bash",
        risk="host_control",
        tool_content=SECRET_TOOL_CONTENT,
    )


def _route_endpoint(router, path, method="POST"):
    for route in router.routes:
        if route.path == path and method in route.methods:
            return route.endpoint
    raise AssertionError(f"{method} route not found: {path}")


def _router(monkeypatch, store):
    monkeypatch.setattr(chat_routes, "ToolApprovalStore", lambda: store)
    monkeypatch.setattr(
        chat_routes,
        "require_user",
        lambda request: request.state.current_user,
    )
    monkeypatch.setattr(
        chat_routes,
        "_verify_session_owner",
        lambda request, session_id: None,
    )
    return chat_routes.setup_chat_routes(
        session_manager=None,
        chat_handler=None,
        chat_processor=None,
        memory_manager=None,
        research_handler=None,
        upload_handler=None,
    )


def _request(owner="alice"):
    return SimpleNamespace(
        state=SimpleNamespace(current_user=owner),
    )


@pytest.mark.asyncio
async def test_approve_route_is_owner_scoped_and_redacts_tool_content(monkeypatch):
    store = FakeApprovalStore()
    router = _router(monkeypatch, store)
    endpoint = _route_endpoint(
        router,
        "/api/tool-approvals/{approval_id}/approve",
    )

    response = await endpoint(_request("alice"), "approval-1")

    assert store.calls == [("approve", "approval-1", "alice")]
    assert response == {
        "approvalId": "approval-1",
        "sessionId": "session-1",
        "runId": "run-1",
        "status": "approved",
        "expiresAt": "2026-08-06T10:00:00+00:00",
    }
    assert SECRET_TOOL_CONTENT not in repr(response)
    assert "tool" not in response
    assert "toolContent" not in response


@pytest.mark.asyncio
async def test_reject_route_is_owner_scoped(monkeypatch):
    store = FakeApprovalStore()
    router = _router(monkeypatch, store)
    endpoint = _route_endpoint(
        router,
        "/api/tool-approvals/{approval_id}/reject",
    )

    response = await endpoint(_request("bob"), "approval-1")

    assert store.calls == [("reject", "approval-1", "bob")]
    assert response["status"] == "rejected"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "operation"),
    [
        ("/api/tool-approvals/{approval_id}/approve", "approve"),
        ("/api/tool-approvals/{approval_id}/reject", "reject"),
    ],
)
async def test_invalid_approval_transition_returns_conflict(
    monkeypatch,
    path,
    operation,
):
    store = FakeApprovalStore()
    store.error = ApprovalError("approval is unavailable")
    router = _router(monkeypatch, store)
    endpoint = _route_endpoint(router, path)

    with pytest.raises(HTTPException) as exc:
        await endpoint(_request("mallory"), "approval-1")

    assert store.calls == [(operation, "approval-1", "mallory")]
    assert exc.value.status_code == 409
    assert exc.value.detail == "approval is unavailable"


@pytest.mark.asyncio
async def test_list_route_returns_pending_approval_without_secrets(monkeypatch):
    store = FakeApprovalStore()
    router = _router(monkeypatch, store)
    endpoint = _route_endpoint(
        router,
        "/api/tool-approvals",
        method="GET",
    )

    response = await endpoint(
        _request("alice"),
        session_id="session-1",
    )

    assert store.calls == [("list", "session-1", "alice")]
    assert response == {
        "approvals": [
            {
                "approvalId": "approval-1",
                "sessionId": "session-1",
                "runId": "run-1",
                "tool": "bash",
                "risk": "host_control",
                "status": "pending",
                "expiresAt": "2026-08-06T10:00:00+00:00",
            }
        ]
    }

    serialized = repr(response)
    assert SECRET_TOOL_CONTENT not in serialized
    assert "toolContent" not in serialized
    assert "tool_content" not in serialized
    assert "argumentHash" not in serialized
    assert "argument_hash" not in serialized
    assert "fingerprint" not in serialized


@pytest.mark.asyncio
async def test_list_route_rejects_unauthenticated_request(monkeypatch):
    store = FakeApprovalStore()
    router = _router(monkeypatch, store)
    endpoint = _route_endpoint(
        router,
        "/api/tool-approvals",
        method="GET",
    )

    def reject_unauthenticated(request):
        raise HTTPException(status_code=401, detail="authentication required")

    monkeypatch.setattr(
        chat_routes,
        "require_user",
        reject_unauthenticated,
    )

    with pytest.raises(HTTPException) as exc:
        await endpoint(
            _request(),
            session_id="session-1",
        )

    assert exc.value.status_code == 401
    assert store.calls == []


@pytest.mark.asyncio
async def test_list_route_rejects_foreign_session_before_store(monkeypatch):
    store = FakeApprovalStore()
    router = _router(monkeypatch, store)
    endpoint = _route_endpoint(
        router,
        "/api/tool-approvals",
        method="GET",
    )

    def reject_foreign_session(request, session_id):
        raise HTTPException(status_code=404, detail="session not found")

    monkeypatch.setattr(
        chat_routes,
        "_verify_session_owner",
        reject_foreign_session,
    )

    with pytest.raises(HTTPException) as exc:
        await endpoint(
            _request("mallory"),
            session_id="session-1",
        )

    assert exc.value.status_code == 404
    assert store.calls == []


@pytest.mark.asyncio
async def test_list_route_maps_store_failure_to_conflict(monkeypatch):
    store = FakeApprovalStore()
    store.error = ApprovalError("approvals are unavailable")
    router = _router(monkeypatch, store)
    endpoint = _route_endpoint(
        router,
        "/api/tool-approvals",
        method="GET",
    )

    with pytest.raises(HTTPException) as exc:
        await endpoint(
            _request("alice"),
            session_id="session-1",
        )

    assert exc.value.status_code == 409
    assert exc.value.detail == "approvals are unavailable"


def test_restore_approval_resume_request_drops_synthetic_latest_turn():
    original = [
        {
            "role": "user",
            "content": "Run printf and return only its output.",
        },
        {
            "role": "assistant",
            "content": "I need approval.",
        },
        {
            "role": "user",
            "content": "Continue the approved tool call.",
        },
    ]

    restored = chat_routes._restore_approval_resume_request(
        original,
        "Continue the approved tool call.",
    )

    assert restored == original[:-1]
    assert len(original) == 3


def test_restore_approval_resume_request_keeps_only_user_turn():
    original = [
        {
            "role": "user",
            "content": "Continue the approved tool call.",
        },
    ]

    restored = chat_routes._restore_approval_resume_request(
        original,
        "Continue the approved tool call.",
    )

    assert restored == original


def test_restore_approval_resume_request_keeps_different_latest_turn():
    original = [
        {
            "role": "user",
            "content": "Run the approved command.",
        },
        {
            "role": "assistant",
            "content": "Approval requested.",
        },
        {
            "role": "user",
            "content": "This is a different real request.",
        },
    ]

    restored = chat_routes._restore_approval_resume_request(
        original,
        "Continue the approved tool call.",
    )

    assert restored == original


@pytest.mark.asyncio
async def test_approval_routes_work_through_asgi_http(monkeypatch):
    store = FakeApprovalStore()
    router = _router(monkeypatch, store)

    app = FastAPI()
    app.include_router(router)

    @app.middleware("http")
    async def add_test_identity(request, call_next):
        request.state.current_user = request.headers.get(
            "x-test-user",
            "alice",
        )
        return await call_next(request)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://approval.test",
    ) as client:
        listed = await client.get(
            "/api/tool-approvals",
            params={"sessionId": "session-1"},
            headers={"x-test-user": "alice"},
        )

        assert listed.status_code == 200
        assert listed.json() == {
            "approvals": [
                {
                    "approvalId": "approval-1",
                    "sessionId": "session-1",
                    "runId": "run-1",
                    "tool": "bash",
                    "risk": "host_control",
                    "status": "pending",
                    "expiresAt": "2026-08-06T10:00:00+00:00",
                }
            ]
        }
        assert SECRET_TOOL_CONTENT not in listed.text

        approved = await client.post(
            "/api/tool-approvals/approval-1/approve",
            headers={"x-test-user": "alice"},
        )

        assert approved.status_code == 200
        assert approved.json() == {
            "approvalId": "approval-1",
            "sessionId": "session-1",
            "runId": "run-1",
            "status": "approved",
            "expiresAt": "2026-08-06T10:00:00+00:00",
        }
        assert SECRET_TOOL_CONTENT not in approved.text

    assert store.calls == [
        ("list", "session-1", "alice"),
        ("approve", "approval-1", "alice"),
    ]


@pytest.mark.asyncio
async def test_invalid_approval_transition_is_http_conflict(monkeypatch):
    store = FakeApprovalStore()
    store.error = ApprovalError("approval is unavailable")
    router = _router(monkeypatch, store)

    app = FastAPI()
    app.include_router(router)

    @app.middleware("http")
    async def add_test_identity(request, call_next):
        request.state.current_user = "alice"
        return await call_next(request)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://approval.test",
    ) as client:
        response = await client.post(
            "/api/tool-approvals/approval-1/approve"
        )

    assert response.status_code == 409
    assert response.json() == {"detail": "approval is unavailable"}
    assert store.calls == [("approve", "approval-1", "alice")]


@pytest.mark.asyncio
async def test_persisted_approval_survives_store_and_router_reload(
    monkeypatch,
    tmp_path,
):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'route-reload.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    ToolApprovalRecord.__table__.create(engine)
    factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
    )

    created_at = datetime.now(timezone.utc)
    approval = ToolApproval.create(
        owner="alice",
        session_id="session-1",
        run_id="run-1",
        tool_name="bash",
        risk="host_control",
        arguments=SECRET_TOOL_CONTENT,
        now=created_at,
        ttl=timedelta(minutes=10),
    )

    try:
        original_store = ToolApprovalStore(factory)
        original_store.create(
            approval,
            tool_content=SECRET_TOOL_CONTENT,
        )

        reloaded_store = ToolApprovalStore(factory)
        router = _router(monkeypatch, reloaded_store)

        app = FastAPI()
        app.include_router(router)

        @app.middleware("http")
        async def add_test_identity(request, call_next):
            request.state.current_user = "alice"
            return await call_next(request)

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url="http://approval.test",
        ) as client:
            listed = await client.get(
                "/api/tool-approvals",
                params={"sessionId": "session-1"},
            )

            assert listed.status_code == 200
            assert listed.json() == {
                "approvals": [
                    {
                        "approvalId": approval.id,
                        "sessionId": "session-1",
                        "runId": "run-1",
                        "tool": "bash",
                        "risk": "host_control",
                        "status": "pending",
                        "expiresAt": approval.expires_at.isoformat(),
                    }
                ]
            }
            assert SECRET_TOOL_CONTENT not in listed.text
            assert "toolContent" not in listed.text
            assert "argumentHash" not in listed.text
            assert "fingerprint" not in listed.text

            approved = await client.post(
                f"/api/tool-approvals/{approval.id}/approve"
            )

            assert approved.status_code == 200
            assert approved.json()["status"] == "approved"
            assert SECRET_TOOL_CONTENT not in approved.text

        verification_store = ToolApprovalStore(factory)
        persisted = verification_store.get(
            approval.id,
            owner="alice",
        )

        assert persisted.status is ApprovalStatus.APPROVED
        assert verification_store.load_tool_content(
            approval.id,
            owner="alice",
        ) == SECRET_TOOL_CONTENT
    finally:
        engine.dispose()
