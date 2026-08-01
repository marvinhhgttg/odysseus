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


# Phase 2: agent-run lifecycle telemetry


def _lifecycle_messages(caplog):
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == "src.agent_runs"
        and record.getMessage().startswith("Agent run ")
    ]


@pytest.mark.asyncio
async def test_agent_run_lifecycle_logs_success(caplog):
    session_id = "lifecycle-success"
    agent_runs._RUNS.pop(session_id, None)

    async def stream():
        yield "data: one\n\n"
        yield "data: two\n\n"

    with caplog.at_level(logging.INFO, logger="src.agent_runs"):
        with correlation_context(request_id="request-success"):
            run = agent_runs.start(session_id, stream())
        await run.task

    messages = _lifecycle_messages(caplog)

    assert run.status == "done"
    assert len(run.buffer) == 2
    assert sum("Agent run started " in item for item in messages) == 1
    assert sum("Agent run finished " in item for item in messages) == 1
    assert not any("Agent run stopped " in item for item in messages)
    assert not any("Agent run failed " in item for item in messages)
    assert any(
        "session_id=lifecycle-success replaced_previous=false" in item
        for item in messages
    )
    assert any(
        "status=done" in item and "event_count=2" in item
        for item in messages
    )

    _discard_run(session_id, run)


@pytest.mark.asyncio
async def test_agent_run_lifecycle_logs_stop(caplog):
    session_id = "lifecycle-stop"
    agent_runs._RUNS.pop(session_id, None)
    entered = asyncio.Event()

    async def stream():
        yield "data: partial\n\n"
        entered.set()
        await asyncio.Event().wait()

    with caplog.at_level(logging.INFO, logger="src.agent_runs"):
        with correlation_context(request_id="request-stop"):
            run = agent_runs.start(session_id, stream())

        await entered.wait()
        assert agent_runs.stop(session_id)
        await run.task

    messages = _lifecycle_messages(caplog)

    assert run.status == "stopped"
    assert len(run.buffer) == 1
    assert sum("Agent run started " in item for item in messages) == 1
    assert sum("Agent run stopped " in item for item in messages) == 1
    assert not any("Agent run finished " in item for item in messages)
    assert not any("Agent run failed " in item for item in messages)
    assert any(
        "status=stopped" in item and "event_count=1" in item
        for item in messages
    )

    _discard_run(session_id, run)


@pytest.mark.asyncio
async def test_agent_run_failure_log_excludes_exception_text(caplog):
    session_id = "lifecycle-failure"
    agent_runs._RUNS.pop(session_id, None)
    secret = "do-not-log-agent-run-exception"

    async def stream():
        yield "data: before-error\n\n"
        raise RuntimeError(secret)

    with caplog.at_level(logging.INFO, logger="src.agent_runs"):
        with correlation_context(request_id="request-failure"):
            run = agent_runs.start(session_id, stream())
        await run.task

    messages = _lifecycle_messages(caplog)

    assert run.status == "error"
    assert len(run.buffer) == 3
    assert sum("Agent run started " in item for item in messages) == 1
    assert sum("Agent run failed " in item for item in messages) == 1
    assert not any("Agent run finished " in item for item in messages)
    assert not any("Agent run stopped " in item for item in messages)
    assert any(
        "status=error" in item
        and "event_count=3" in item
        and "error_type=RuntimeError" in item
        for item in messages
    )
    assert all(secret not in item for item in messages)
    assert all(record.exc_info is None for record in caplog.records)

    _discard_run(session_id, run)


@pytest.mark.asyncio
async def test_replaced_run_keeps_buffers_and_lifecycle_separate(caplog):
    session_id = "lifecycle-replacement"
    agent_runs._RUNS.pop(session_id, None)
    first_entered = asyncio.Event()

    async def first_stream():
        yield "data: first-only\n\n"
        first_entered.set()
        await asyncio.Event().wait()

    async def second_stream():
        yield "data: second-only\n\n"

    with caplog.at_level(logging.INFO, logger="src.agent_runs"):
        with correlation_context(request_id="request-first"):
            first = agent_runs.start(session_id, first_stream())

        await first_entered.wait()

        with correlation_context(request_id="request-second"):
            second = agent_runs.start(session_id, second_stream())

        await asyncio.gather(first.task, second.task)

    messages = _lifecycle_messages(caplog)

    assert first.status == "stopped"
    assert second.status == "done"
    assert first.run_id != second.run_id
    assert first.buffer == ["data: first-only\n\n"]
    assert second.buffer == ["data: second-only\n\n"]
    assert agent_runs._RUNS.get(session_id) is second
    assert first.evict_task is None
    assert any(
        "Agent run started session_id=lifecycle-replacement "
        "replaced_previous=true" in item
        for item in messages
    )
    assert sum("Agent run stopped " in item for item in messages) == 1
    assert sum("Agent run finished " in item for item in messages) == 1

    _discard_run(session_id, second)
