from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.routes.drive_organizer_routes import router


def build_app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def test_drive_scan_route_returns_queued():
    client = TestClient(build_app())
    resp = client.post(
        "/api/drive-organizer/scan",
        json={
            "integration_id": "int_001",
            "scope": {"mode": "folder", "folder_id": "fld_001", "max_files": 100},
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "queued"
    assert "scan_run_id" in body


def test_drive_plan_create_returns_ready():
    client = TestClient(build_app())
    resp = client.post(
        "/api/drive-organizer/plans",
        json={
            "integration_id": "int_001",
            "scan_run_id": "scan_001",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ready"
    assert body["summary"]["proposed_actions"] >= 0


def test_drive_plan_actions_returns_list():
    client = TestClient(build_app())
    resp = client.get("/api/drive-organizer/plans/plan_001/actions")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert body[0]["action_type"] == "move_file"


def test_drive_approve_request_returns_pending():
    client = TestClient(build_app())
    resp = client.post(
        "/api/drive-organizer/plans/plan_001/approve-request",
        json={
            "selection_mode": "subset",
            "action_ids": ["act_001"],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending"
    assert body["approval_id"]
    assert body["fingerprint"]


def test_drive_apply_route_returns_partial_apply():
    client = TestClient(build_app())
    resp = client.post(
        "/api/drive-organizer/plans/plan_001/apply",
        json={
            "approval_id": "apr_demo_001",
            "expected_fingerprint": "sha256:demo",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "partially_applied"
    assert body["applied"] >= 0
