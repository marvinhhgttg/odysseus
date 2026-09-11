"""Coverage for Punkt-6 internal-tool auth: loopback + token gate shared by
AuthMiddleware and require_admin, plus the masked diagnostics endpoint."""
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import Headers

from src import internal_tool_auth as ita
from core.middleware import (
    INTERNAL_TOOL_HEADER,
    INTERNAL_TOOL_TOKEN,
    INTERNAL_TOOL_USER,
)
from routes.diagnostics_routes import setup_diagnostics_routes
from src.auth_dependencies import require_admin


class _Stub:
    """Minimal stand-in for starlette Requests in pure-helper tests."""

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


# --- is_trusted_loopback ---------------------------------------------------


def test_loopback_ipv4_trusted():
    assert ita.is_trusted_loopback(_Stub(host="127.0.0.1")) is True


def test_loopback_ipv6_trusted():
    assert ita.is_trusted_loopback(_Stub(host="::1")) is True


def test_remote_host_not_trusted():
    assert ita.is_trusted_loopback(_Stub(host="203.0.113.7")) is False


def test_empty_host_not_trusted():
    assert ita.is_trusted_loopback(_Stub(host="")) is False


@pytest.mark.parametrize("fwd", [
    "x-forwarded-for", "x-forwarded-host", "x-real-ip",
    "forwarded", "cf-connecting-ip", "cf-ray", "cf-visitor",
])
def test_loopback_with_proxy_forward_header_not_trusted(fwd):
    request = _Stub(host="127.0.0.1", headers={fwd: "10.0.0.1"})
    assert ita.is_trusted_loopback(request) is False


# --- internal_tool_request_ok ----------------------------------------------


def test_ok_with_token_and_loopback():
    assert ita.internal_tool_request_ok(_tok_request()) is True


def test_ok_without_header():
    assert ita.internal_tool_request_ok(_Stub()) is False


def test_wrong_token_rejected():
    assert ita.internal_tool_request_ok(_tok_request(token="wrong-token")) is False


def test_correct_token_but_remote_rejected():
    request = _tok_request(host="203.0.113.7")
    assert ita.internal_tool_request_ok(request) is False


def test_correct_token_but_proxied_rejected():
    request = _tok_request(extra={"x-forwarded-for": "10.0.0.1"})
    assert ita.internal_tool_request_ok(request) is False


# --- resolve_internal_tool_user ---------------------------------------------


def test_impersonates_existing_owner():
    request = _tok_request(owner="alice")
    assert ita.resolve_internal_tool_user(request, {"alice": {}}) == "alice"


def test_does_not_impersonate_unknown_owner():
    request = _tok_request(owner="mallory")
    assert ita.resolve_internal_tool_user(request, {"alice": {}}) == INTERNAL_TOOL_USER


def test_no_owner_falls_back_to_reserved_user():
    assert ita.resolve_internal_tool_user(_tok_request(), {"alice": {}}) == INTERNAL_TOOL_USER


# --- internal_tool_status ----------------------------------------------------


def test_status_masks_token(monkeypatch):
    monkeypatch.delenv("ODYSSEUS_INTERNAL_TOKEN", raising=False)
    status = ita.internal_tool_status()
    assert status["enabled"] is True
    assert status["header"] == INTERNAL_TOOL_HEADER
    assert status["reserved_user"] == INTERNAL_TOOL_USER
    assert status["loopback_only"] is True
    assert status["token_source"] == "ephemeral_per_process"
    for value in status.values():
        assert INTERNAL_TOOL_TOKEN not in str(value)


def test_status_token_source_env(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_INTERNAL_TOKEN", "from-env")
    assert ita.internal_tool_status()["token_source"] == "env"


# --- require_admin gate -----------------------------------------------------
# TestClient's peer host is "testserver", never loopback, so header+token alone
# must FAIL the require_admin gate (defence in depth). With the loopback check
# satisfied (patched), the same header succeeds.


def _admin_app():
    app = FastAPI()

    @app.get("/admin-check")
    def _check(_: None = Depends(require_admin)):
        return {"ok": True}

    return app


def test_require_admin_rejects_header_from_remote(monkeypatch):
    monkeypatch.setattr(ita, "INTERNAL_TOOL_TOKEN", "secret-token")
    client = TestClient(_admin_app())
    response = client.get("/admin-check", headers={INTERNAL_TOOL_HEADER: "secret-token"})
    assert response.status_code == 403


def test_require_admin_allows_header_from_trusted_loopback(monkeypatch):
    monkeypatch.setattr(ita, "INTERNAL_TOOL_TOKEN", "secret-token")
    monkeypatch.setattr(ita, "is_trusted_loopback", lambda request: True)
    client = TestClient(_admin_app())
    response = client.get("/admin-check", headers={INTERNAL_TOOL_HEADER: "secret-token"})
    assert response.status_code == 200


def test_require_admin_rejects_remote_with_wrong_token(monkeypatch):
    monkeypatch.setattr(ita, "INTERNAL_TOOL_TOKEN", "secret-token")
    client = TestClient(_admin_app())
    response = client.get("/admin-check", headers={INTERNAL_TOOL_HEADER: "whoops"})
    assert response.status_code == 403


# --- diagnostics endpoint ----------------------------------------------------


def test_internal_tool_diagnostics_endpoint(monkeypatch):
    monkeypatch.delenv("ODYSSEUS_INTERNAL_TOKEN", raising=False)
    app = FastAPI()
    app.include_router(setup_diagnostics_routes(None, False, None))
    app.dependency_overrides[require_admin] = lambda: None
    client = TestClient(app)
    body = client.get("/api/diagnostics/internal-tool").json()
    assert body["enabled"] is True
    assert body["header"] == INTERNAL_TOOL_HEADER
    assert body["token_source"] == "ephemeral_per_process"
    assert INTERNAL_TOOL_TOKEN not in str(body)


def test_internal_tool_diagnostics_requires_admin():
    app = FastAPI()
    app.include_router(setup_diagnostics_routes(None, False, None))
    client = TestClient(app)
    assert client.get("/api/diagnostics/internal-tool").status_code == 403