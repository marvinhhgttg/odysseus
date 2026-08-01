import asyncio
import logging
from types import SimpleNamespace

import pytest

from src import agent_runs
from src import tool_execution
from src.request_context import (
    RequestIdLogFilter,
    correlation_context,
    current_agent_run_id,
    current_request_id,
)


def _discard_run(session_id, run):
    agent_runs._RUNS.pop(session_id, None)
    if run.evict_task and not run.evict_task.done():
        run.evict_task.cancel()


@pytest.mark.asyncio
async def test_detached_run_keeps_request_and_unique_run_id():
    seen = []
    session_id = "correlation-session"
    agent_runs._RUNS.pop(session_id, None)

    async def stream():
        seen.append((current_request_id(), current_agent_run_id()))
        yield "data: test\\n\\n"

    with correlation_context(request_id="request-correlation-42"):
        run = agent_runs.start(session_id, stream())

    await run.task

    assert run.request_id == "request-correlation-42"
    assert run.run_id
    assert seen == [("request-correlation-42", run.run_id)]

    _discard_run(session_id, run)


def test_sequential_runs_receive_different_ids():
    first = agent_runs._Run()
    second = agent_runs._Run()

    assert first.run_id
    assert second.run_id
    assert first.run_id != second.run_id


def test_log_filter_exposes_run_id():
    record = logging.LogRecord(
        "test", logging.INFO, __file__, 1, "message", (), None
    )

    with correlation_context(
        request_id="request-1",
        agent_run_id="run-1",
    ):
        assert RequestIdLogFilter().filter(record)

    assert record.request_id == "request-1"
    assert record.run_id == "run-1"


@pytest.mark.asyncio
async def test_tool_logging_uses_metadata_not_content(monkeypatch, caplog):
    secret = "do-not-log-this-command"

    async def fake_impl(*args, **kwargs):
        return "unsafe description", {"exit_code": 0, "output": secret}

    monkeypatch.setattr(
        tool_execution,
        "_execute_tool_block_impl",
        fake_impl,
    )
    block = SimpleNamespace(tool_type="bash", content=secret)

    with caplog.at_level(logging.INFO, logger="src.tool_execution"):
        with correlation_context(
            request_id="request-2",
            agent_run_id="run-2",
        ):
            description, result = await tool_execution.execute_tool_block(
                block,
                session_id="session-2",
            )

    assert description == "unsafe description"
    assert result["output"] == secret

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "Tool execution started tool=bash session_id=session-2" in message
        for message in messages
    )
    assert any(
        "Tool execution finished tool=bash session_id=session-2" in message
        for message in messages
    )
    assert all(secret not in message for message in messages)
    assert all("unsafe description" not in message for message in messages)


@pytest.mark.asyncio
async def test_tool_cancellation_is_logged_and_propagated(
    monkeypatch, caplog
):
    async def cancelled_impl(*args, **kwargs):
        raise asyncio.CancelledError

    monkeypatch.setattr(
        tool_execution,
        "_execute_tool_block_impl",
        cancelled_impl,
    )
    block = SimpleNamespace(tool_type="bash", content="secret")

    with caplog.at_level(logging.INFO, logger="src.tool_execution"):
        with pytest.raises(asyncio.CancelledError):
            await tool_execution.execute_tool_block(
                block,
                session_id="session-cancelled",
            )

    messages = [record.getMessage() for record in caplog.records]
    assert any(
        "Tool execution cancelled tool=bash "
        "session_id=session-cancelled" in message
        for message in messages
    )
