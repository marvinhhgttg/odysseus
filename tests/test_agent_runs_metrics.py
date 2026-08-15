from src import agent_runs


def test_recent_metrics_empty():
    assert agent_runs.recent_metrics(limit=10) == []


def test_summarize_metrics_empty():
    summary = agent_runs.summarize_metrics([])
    assert summary["count"] == 0
    assert summary["avg_response_time"] is None
    assert summary["p50_response_time"] is None
    assert summary["p95_response_time"] is None
