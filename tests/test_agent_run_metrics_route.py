from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from src import agent_runs


def test_recent_agent_run_metrics_route_returns_json(monkeypatch):
    sample = [{
        "run_id": "run-test",
        "request_id": "request-test",
        "status": "done",
        "event_count": 3,
        "response_time": 1.25,
        "model": "gemma-test",
        "route": "agent",
    }]

    monkeypatch.setattr(agent_runs, "recent_metrics", lambda limit=50: sample)
    monkeypatch.setattr(
        agent_runs,
        "summarize_metrics",
        lambda metrics: {
            "count": len(metrics),
            "status_counts": {"done": len(metrics)},
            "avg_response_time": 1.25,
            "p50_response_time": 1.25,
            "p95_response_time": 1.25,
        },
    )

    router = APIRouter()

    @router.get("/api/agent-runs/metrics/recent")
    async def get_recent_agent_run_metrics(limit: int = 50):
        safe_limit = max(1, min(int(limit), 100))
        metrics = agent_runs.recent_metrics(limit=safe_limit)
        return {
            "items": metrics,
            "summary": agent_runs.summarize_metrics(metrics),
        }

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    response = client.get("/api/agent-runs/metrics/recent?limit=5")

    assert response.status_code == 200
    payload = response.json()
    assert payload["summary"]["count"] == 1
    assert payload["items"][0]["route"] == "agent"
    assert payload["items"][0]["model"] == "gemma-test"
