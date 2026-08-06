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
        tool_content=SECRET_TOOL_CONTENT,
    )


def _route_endpoint(router, path):
    for route in router.routes:
        if route.path == path and "POST" in route.methods:
            return route.endpoint
    raise AssertionError(f"POST route not found: {path}")


def _router(monkeypatch, store):
    monkeypatch.setattr(chat_routes, "ToolApprovalStore", lambda: store)
    monkeypatch.setattr(
        chat_routes,
        "require_user",
        lambda request: request.state.current_user,
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
