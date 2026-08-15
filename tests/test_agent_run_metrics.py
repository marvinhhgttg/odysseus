import json

from src import agent_runs


def test_metrics_event_is_retained_with_correlation_metadata(monkeypatch):
    monkeypatch.setattr(agent_runs, "_METRICS_HISTORY", agent_runs.deque(maxlen=100))

    run = agent_runs._Run()
    run.run_id = "run-123"
    run.request_id = "request-456"
    run.status = "done"

    event = (
        "data: "
        + json.dumps(
            {
                "type": "metrics",
                "data": {
                    "response_time": 1.25,
                    "time_to_first_token": 0.4,
                    "model": "test-model",
                    "tool_calls": 2,
                },
            }
        )
        + "\n\n"
    )

    agent_runs._publish(run, event)
    agent_runs._store_terminal_metrics(run)

    assert agent_runs.recent_metrics() == [
        {
            "response_time": 1.25,
            "time_to_first_token": 0.4,
            "model": "test-model",
            "tool_calls": 2,
            "run_id": "run-123",
            "request_id": "request-456",
            "status": "done",
            "event_count": 1,
        }
    ]


def test_metrics_history_is_newest_first_and_limit_is_bounded(monkeypatch):
    monkeypatch.setattr(agent_runs, "_METRICS_HISTORY", agent_runs.deque(maxlen=100))

    first = agent_runs._Run()
    first.run_id = "first"
    first.status = "done"
    agent_runs._publish(
        first,
        'data: {"type":"metrics","data":{"response_time":1}}\n\n',
    )
    agent_runs._store_terminal_metrics(first)

    second = agent_runs._Run()
    second.run_id = "second"
    second.status = "done"
    agent_runs._publish(
        second,
        'data: {"type":"metrics","data":{"response_time":2}}\n\n',
    )
    agent_runs._store_terminal_metrics(second)

    assert [item["run_id"] for item in agent_runs.recent_metrics(1)] == ["second"]
    assert [item["run_id"] for item in agent_runs.recent_metrics(99)] == [
        "second",
        "first",
    ]
    assert agent_runs.recent_metrics(0) == []
