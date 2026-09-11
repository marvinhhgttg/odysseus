"""Punkt-6⊗Punkt-4: internal-tool loopback auth outcomes feed the metrics
collector (snapshot + Prometheus export) via a single audit point."""
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from src import internal_tool_auth as ita
from src import metrics
from core.middleware import INTERNAL_TOOL_HEADER, INTERNAL_TOOL_TOKEN, INTERNAL_TOOL_USER
from routes.diagnostics_routes import setup_diagnostics_routes
from src.auth_dependencies import require_admin


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset_counts()
    yield
    metrics.reset_counts()


class _Stub:
    """Minimal stand-in for starlette Requests in audit-helper tests."""

    def __init__(self, host="127.0.0.1", headers=None, owner=None):
        self.client = type("C", (), {"host": host})()
        self.headers = Headers(headers or {})
        if owner is not None:
            self.headers = Headers(dict(self.headers, **{"X-Odysseus-Owner": owner}))


def _tok_request(host="127.0.0.1", token=None, extra=None, owner=None):
    token = INTERNAL_TOOL_TOKEN if token is None else token
    headers = {INTERNAL_TOOL_HEADER: token}
    headers.update(extra or {})
    return _Stub(host=host, headers=headers, owner=owner)


def _outcomes():
    return metrics.snapshot()["internal_tool_outcomes"]


# --- audit_internal_tool_request --------------------------------------------


def test_no_header_returns_none_and_records_nothing():
    result = ita.audit_internal_tool_request(_Stub(), {"alice": {}})
    assert result is None
    assert _outcomes() == {}


def test_loopback_token_grants():
    result = ita.audit_internal_tool_request(_tok_request(), {"alice": {}})
    assert result["granted"] is True
    assert result["user"] == INTERNAL_TOOL_USER
    assert result["outcome"] == "granted"
    assert _outcomes() == {"granted": 1}


def test_loopback_token_impersonates_existing_owner():
    result = ita.audit_internal_tool_request(_tok_request(owner="alice"), {"alice": {}})
    assert result["user"] == "alice"
    assert result["outcome"] == "impersonated"
    assert _outcomes() == {"impersonated": 1}


def test_correct_token_but_remote_rejected():
    result = ita.audit_internal_tool_request(
        _tok_request(host="203.0.113.7"), {"alice": {}}
    )
    assert result["granted"] is False
    assert result["outcome"] == "rejected"
    assert _outcomes() == {"rejected": 1}


def test_wrong_token_loopback_rejected():
    result = ita.audit_internal_tool_request(
        _tok_request(token="not-the-token"), {"alice": {}}
    )
    assert result["granted"] is False
    assert _outcomes() == {"rejected": 1}


# --- metrics collector integration -------------------------------------------


def test_record_internal_tool_outcome_and_reset():
    ita.audit_internal_tool_request(_tok_request(), {"alice": {}})
    ita.audit_internal_tool_request(_tok_request(token="bad"), {"alice": {}})
    assert _outcomes() == {"granted": 1, "rejected": 1}
    metrics.reset_counts()
    assert _outcomes() == {}


def test_snapshot_exposes_internal_tool_outcomes():
    metrics.reset_counts()
    result = ita.audit_internal_tool_request(
        _tok_request(owner="alice"), {"alice": {}}
    )
    assert result["outcome"] == "impersonated"
    snap = metrics.snapshot()
    assert snap["internal_tool_outcomes"] == {"impersonated": 1}


def test_prometheus_emits_internal_tool_counter():
    metrics.reset_counts()
    ita.audit_internal_tool_request(_tok_request(), {"alice": {}})
    ita.audit_internal_tool_request(_tok_request(token="bad"), {"alice": {}})
    text = metrics.prometheus_text()
    assert "# HELP odysseus_internal_tool_requests_total" in text
    assert "odysseus_internal_tool_requests_total 2" in text
    assert 'odysseus_internal_tool_requests_total{result="granted"} 1' in text
    assert 'odysseus_internal_tool_requests_total{result="rejected"} 1' in text


def test_prometheus_internal_tool_zero_when_empty():
    metrics.reset_counts()
    text = metrics.prometheus_text()
    assert "odysseus_internal_tool_requests_total 0" in text


# --- end-to-end: require_admin re-checks gate without recording --------------
# (recording happens once, at the auth middleware; require_admin alone must
# not double-count.)


def _admin_app():
    app = FastAPI()

    @app.get("/admin-check")
    def _check(_: None = Depends(require_admin)):
        return {"ok": True}

    return app


def test_require_admin_rejects_remote_and_does_not_count(monkeypatch):
    metrics.reset_counts()
    monkeypatch.setattr(ita, "INTERNAL_TOOL_TOKEN", "secret-token")
    client = TestClient(_admin_app())
    response = client.get("/admin-check", headers={INTERNAL_TOOL_HEADER: "secret-token"})
    assert response.status_code == 403
    assert _outcomes() == {}


def test_require_admin_allows_loopback_without_recording(monkeypatch):
    monkeypatch.setattr(ita, "INTERNAL_TOOL_TOKEN", "secret-token")
    monkeypatch.setattr(ita, "is_trusted_loopback", lambda request: True)
    client = TestClient(_admin_app())
    response = client.get("/admin-check", headers={INTERNAL_TOOL_HEADER: "secret-token"})
    assert response.status_code == 200
    assert _outcomes() == {}


def test_diagnostics_metrics_endpoint_includes_outcomes():
    metrics.reset_counts()
    ita.audit_internal_tool_request(_tok_request(), {"alice": {}})
    app = FastAPI()
    app.include_router(setup_diagnostics_routes(None, False, None))
    app.dependency_overrides[require_admin] = lambda: None
    client = TestClient(app)
    body = client.get("/api/diagnostics/metrics").json()
    assert body["internal_tool_outcomes"] == {"granted": 1}

    export_text = client.get("/api/diagnostics/metrics/export").text
    assert 'odysseus_internal_tool_requests_total{result="granted"} 1' in export_text