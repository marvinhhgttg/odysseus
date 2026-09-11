"""Coverage for the in-process HTTP metrics collector: middleware recording,
the JSON + Prometheus export endpoints, their admin gating, and reset."""

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.diagnostics_routes import setup_diagnostics_routes
from src import metrics
from src.auth_dependencies import require_admin
from src.request_context import RequestIdMiddleware


def _make_client(admin_bypass=True):
    metrics.reset_counts()
    http_app = FastAPI()

    @http_app.get("/probe")
    async def probe():
        return {"ok": True}

    @http_app.post("/probe")
    async def probe_post():
        return {"ok": True}

    @http_app.get("/boom")
    async def boom():
        raise RuntimeError("boom")

    http_app.include_router(setup_diagnostics_routes(None, False, None))
    if admin_bypass:
        http_app.dependency_overrides[require_admin] = lambda: None
    return TestClient(
        RequestIdMiddleware(http_app),
        raise_server_exceptions=False,
    )


@pytest.fixture()
def client():
    return _make_client()


def test_middleware_records_success_roundtrip(client):
    assert client.get("/probe").status_code == 200

    snap = metrics.snapshot()
    assert snap["requests_total"] >= 1
    assert snap["requests_by_status"].get(200) >= 1
    assert snap["requests_by_method"].get("GET") >= 1
    assert snap["active_requests"] == 0
    assert snap["errors_5xx_total"] == 0
    assert snap["latency_seconds"]["samples"] >= 1
    assert snap["latency_seconds"]["avg"] is not None
    assert snap["latency_seconds"]["p50"] is not None
    assert snap["latency_seconds"]["p95"] is not None
    assert snap["uptime_seconds"] >= 0
    assert snap["version"]


def test_status_and_method_buckets(client):
    assert client.get("/probe").status_code == 200
    assert client.post("/probe").status_code == 200
    assert client.get("/does-not-exist").status_code == 404

    snap = metrics.snapshot()
    assert snap["requests_total"] == 3
    assert snap["requests_by_status"] == {200: 2, 404: 1}
    assert snap["requests_by_method"] == {"GET": 2, "POST": 1}


def test_errors_recorded_in_5xx_bucket(client):
    assert client.get("/boom").status_code == 500

    snap = metrics.snapshot()
    assert snap["requests_by_status"].get(500) == 1
    assert snap["errors_5xx_total"] == 1


def test_multiple_requests_accumulate(client):
    for _ in range(5):
        assert client.get("/probe").status_code == 200
    assert metrics.snapshot()["requests_total"] == 5


def test_request_id_header_still_applied(client):
    response = client.get("/probe")
    assert response.status_code == 200
    assert "x-request-id" in response.headers


def test_reset_counts_clears_everything(client):
    assert client.get("/probe").status_code == 200
    assert metrics.snapshot()["requests_total"] == 1

    metrics.reset_counts()
    snap = metrics.snapshot()
    assert snap["requests_total"] == 0
    assert snap["requests_by_status"] == {}
    assert snap["requests_by_method"] == {}
    assert snap["latency_seconds"]["samples"] == 0
    assert snap["latency_seconds"]["avg"] is None


def test_prometheus_text_format(client):
    client.get("/probe")
    client.get("/boom")

    text = metrics.prometheus_text()
    assert text.endswith("\n")
    assert "# HELP odysseus_http_requests_total" in text
    assert "# TYPE odysseus_http_requests_total counter" in text
    assert 'odysseus_http_requests_total{status="200"} 1' in text
    assert 'odysseus_http_requests_total{status="500"} 1' in text
    assert 'odysseus_http_requests_total{method="GET"} 2' in text
    assert "odysseus_http_errors_total 1" in text
    assert "# TYPE odysseus_http_active_requests gauge" in text
    assert "odysseus_http_active_requests 0" in text
    assert '# TYPE odysseus_http_latency_seconds summary' in text
    assert (
        f'odysseus_http_latency_seconds_count {metrics.snapshot()["requests_total"]}'
        in text
    )
    assert "odysseus_uptime_seconds " in text
    assert 'odysseus_build_info{version=' in text


def test_json_endpoint_returns_snapshot(client):
    client.get("/probe")
    response = client.get("/api/diagnostics/metrics")
    assert response.status_code == 200
    body = response.json()
    assert body["requests_total"] >= 1
    assert "requests_by_status" in body
    assert "latency_seconds" in body
    assert "collected_at" in body


def test_export_endpoint_returns_prometheus_text(client):
    client.get("/probe")
    response = client.get("/api/diagnostics/metrics/export")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    body = response.text
    assert "# TYPE odysseus_http_requests_total counter" in body
    assert "odysseus_http_latency_seconds " in body


def test_metrics_endpoints_require_admin():
    client = _make_client(admin_bypass=False)
    assert client.get("/api/diagnostics/metrics").status_code == 403
    assert client.get("/api/diagnostics/metrics/export").status_code == 403