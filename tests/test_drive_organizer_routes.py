"""Route tests for the live Google Drive organizer API
(/api/google-drive-organizer, routes/google_drive_organizer_routes.py).

The earlier src/routes/drive_organizer_routes.py demo module is gone; its
routes never matched the real service contract. These tests exercise the
registered setup_google_drive_organizer_routes(service) router with a stubbed
service and a fixed authenticated owner.
"""

from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes import google_drive_organizer_routes as gd


def _make_app(service):
    app = FastAPI()
    app.include_router(gd.setup_google_drive_organizer_routes(service))
    return app


def _stub_service():
    service = MagicMock()
    service.start_scan = AsyncMock(
        return_value={"status": "queued", "scan_run_id": "scan_001"}
    )
    service.build_plan = MagicMock(
        return_value={"status": "ready", "summary": {"proposed_actions": 3}}
    )
    service.get_plan = MagicMock(return_value={"status": "ready"})
    service.list_actions = MagicMock(
        return_value=[{"action_type": "move_file", "id": "act_001"}]
    )
    service.create_approval_request = MagicMock(
        return_value={"status": "pending", "approval_id": "apr_001", "fingerprint": "fp"}
    )
    service.apply_plan = MagicMock(
        return_value={"status": "partially_applied", "applied": 2}
    )
    return service


def test_scan_calls_service_with_owner(monkeypatch):
    monkeypatch.setattr(gd, "require_user", lambda request: "alice")
    service = _stub_service()
    client = TestClient(_make_app(service))
    resp = client.post(
        "/api/google-drive-organizer/scan",
        json={
            "integration_id": "int_001",
            "policy_id": "pol_001",
            "scope": {"mode": "folder", "folder_id": "fld_001", "max_files": 100},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    assert body["scan_run_id"] == "scan_001"
    service.start_scan.assert_awaited_once_with(
        owner_id="alice",
        integration_id="int_001",
        policy_id="pol_001",
        scope={"mode": "folder", "folder_id": "fld_001", "max_files": 100},
    )


def test_scan_requires_authenticated_owner(monkeypatch):
    monkeypatch.setattr(gd, "require_user", lambda request: "")
    service = _stub_service()
    client = TestClient(_make_app(service))
    resp = client.post(
        "/api/google-drive-organizer/scan",
        json={"integration_id": "int_001", "scope": {}},
    )
    assert resp.status_code == 401
    service.start_scan.assert_not_called()


def test_build_plan_calls_service(monkeypatch):
    monkeypatch.setattr(gd, "require_user", lambda request: "alice")
    service = _stub_service()
    client = TestClient(_make_app(service))
    resp = client.post(
        "/api/google-drive-organizer/plans",
        json={"integration_id": "int_001", "scan_run_id": "scan_001"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["summary"]["proposed_actions"] == 3
    service.build_plan.assert_called_once_with(
        owner_id="alice",
        integration_id="int_001",
        scan_run_id="scan_001",
        policy_id=None,
    )


def test_list_actions_calls_service(monkeypatch):
    monkeypatch.setattr(gd, "require_user", lambda request: "alice")
    service = _stub_service()
    client = TestClient(_make_app(service))
    resp = client.get("/api/google-drive-organizer/plans/plan_001/actions")
    assert resp.status_code == 200
    body = resp.json()
    assert body[0]["action_type"] == "move_file"
    service.list_actions.assert_called_once_with(
        owner_id="alice", plan_id="plan_001"
    )


def test_approve_request_calls_service(monkeypatch):
    monkeypatch.setattr(gd, "require_user", lambda request: "alice")
    service = _stub_service()
    client = TestClient(_make_app(service))
    resp = client.post(
        "/api/google-drive-organizer/plans/plan_001/approve-request",
        json={"selection_mode": "subset", "action_ids": ["act_001"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["approval_id"]
    assert body["fingerprint"]
    service.create_approval_request.assert_called_once_with(
        owner_id="alice",
        plan_id="plan_001",
        selection_mode="subset",
        action_ids=["act_001"],
    )


def test_apply_plan_calls_service(monkeypatch):
    monkeypatch.setattr(gd, "require_user", lambda request: "alice")
    service = _stub_service()
    client = TestClient(_make_app(service))
    resp = client.post(
        "/api/google-drive-organizer/plans/plan_001/apply",
        json={"approval_id": "apr_001", "expected_fingerprint": "sha256:demo"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "partially_applied"
    assert body["applied"] == 2
    service.apply_plan.assert_called_once_with(
        owner_id="alice",
        plan_id="plan_001",
        approval_id="apr_001",
        expected_fingerprint="sha256:demo",
    )