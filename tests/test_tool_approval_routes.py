from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from routes import chat_routes
from src.tool_approval import ApprovalError


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
