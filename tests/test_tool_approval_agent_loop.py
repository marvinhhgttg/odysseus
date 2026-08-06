import asyncio
import json

import pytest

import src.agent_loop as al
from src.agent_tools import ToolBlock
from src.tool_approval import ApprovalError


SECRET_COMMAND = "printf 'resume-secret\\n'"


def _collect(gen):
    async def _run():
        return [chunk async for chunk in gen]

    return asyncio.run(_run())


def _events(chunks):
    events = []
    for chunk in chunks:
        if chunk.startswith("data: ") and not chunk.startswith("data: [DONE]"):
            events.append(json.loads(chunk[6:]))
    return events


def _patch_loop_basics(monkeypatch):
    monkeypatch.setattr(
        al,
        "get_setting",
        lambda key, default=None: default,
        raising=False,
    )
    monkeypatch.setattr(
        al,
        "get_mcp_manager",
        lambda: None,
        raising=False,
    )
    monkeypatch.setattr(
        al,
        "estimate_tokens",
        lambda *args, **kwargs: 10,
        raising=False,
    )
    monkeypatch.setattr(
        al,
        "blocked_tools_for_owner",
        lambda owner: set(),
        raising=False,
    )


def test_approved_resume_executes_exact_persisted_block_once(monkeypatch):
    _patch_loop_basics(monkeypatch)
    resume_calls = []
    check_calls = []
    execution_calls = []
    llm_calls = []

    class FakeEnforcer:
        def resume(
            self,
            approval_id,
            *,
            owner,
            session_id,
            run_id,
        ):
            resume_calls.append(
                (approval_id, owner, session_id, run_id)
            )
            return ToolBlock("bash", SECRET_COMMAND)

        def check(self, *args, **kwargs):
            check_calls.append((args, kwargs))
            raise AssertionError(
                "resumed approval must not require approval again"
            )

    async def fake_execute(block, *args, **kwargs):
        execution_calls.append(
            {
                "tool": block.tool_type,
                "content": block.content,
                "session_id": kwargs.get("session_id"),
                "owner": kwargs.get("owner"),
            }
        )
        return (
            "bash",
            {
                "output": "resume-secret",
                "exit_code": 0,
            },
        )

    async def fail_llm(*args, **kwargs):
        llm_calls.append((args, kwargs))
        raise AssertionError(
            "LLM must not run before the resumed tool executes"
        )
        yield  # pragma: no cover

    monkeypatch.setattr(
        al,
        "ToolApprovalEnforcer",
        FakeEnforcer,
    )
    monkeypatch.setattr(
        al,
        "execute_tool_block",
        fake_execute,
        raising=False,
    )
    monkeypatch.setattr(
        al,
        "stream_llm_with_fallback",
        fail_llm,
        raising=False,
    )

    chunks = _collect(
        al.stream_agent_loop(
            "http://local.test/v1",
            "local-model",
            [{"role": "user", "content": "resume"}],
            max_rounds=1,
            relevant_tools={"bash"},
            owner="alice",
            session_id="session-1",
            approval_mode=True,
            approval_id="approval-1",
            run_id="run-1",
        )
    )
    events = _events(chunks)

    assert resume_calls == [
        ("approval-1", "alice", "session-1", "run-1")
    ]
    assert check_calls == []
    assert llm_calls == []
    assert execution_calls == [
        {
            "tool": "bash",
            "content": SECRET_COMMAND,
            "session_id": "session-1",
            "owner": "alice",
        }
    ]

    starts = [
        event
        for event in events
        if event.get("type") == "tool_start"
    ]
    outputs = [
        event
        for event in events
        if event.get("type") == "tool_output"
    ]

    assert len(starts) == 1
    assert starts[0]["tool"] == "bash"
    assert starts[0]["full_command"] == SECRET_COMMAND
    assert len(outputs) == 1
    assert outputs[0]["tool"] == "bash"


@pytest.mark.parametrize(
    "error_message",
    [
        "approval is unavailable",
        "approval binding does not match tool invocation",
        "cannot consume approval in state consumed",
    ],
)
def test_resume_failure_emits_correlated_error_without_execution(
    monkeypatch,
    error_message,
):
    _patch_loop_basics(monkeypatch)
    resume_calls = []
    execution_calls = []
    llm_calls = []

    class FailingEnforcer:
        def resume(
            self,
            approval_id,
            *,
            owner,
            session_id,
            run_id,
        ):
            resume_calls.append(
                (approval_id, owner, session_id, run_id)
            )
            raise ApprovalError(error_message)

        def check(self, *args, **kwargs):
            raise AssertionError(
                "check must not run after resume failure"
            )

    async def fail_execute(*args, **kwargs):
        execution_calls.append((args, kwargs))
        raise AssertionError(
            "tool must not execute after resume failure"
        )

    async def fail_llm(*args, **kwargs):
        llm_calls.append((args, kwargs))
        raise AssertionError(
            "LLM must not run after resume failure"
        )
        yield  # pragma: no cover

    monkeypatch.setattr(
        al,
        "ToolApprovalEnforcer",
        FailingEnforcer,
    )
    monkeypatch.setattr(
        al,
        "execute_tool_block",
        fail_execute,
        raising=False,
    )
    monkeypatch.setattr(
        al,
        "stream_llm_with_fallback",
        fail_llm,
        raising=False,
    )

    chunks = _collect(
        al.stream_agent_loop(
            "http://local.test/v1",
            "local-model",
            [{"role": "user", "content": "resume"}],
            max_rounds=1,
            relevant_tools={"bash"},
            owner="alice",
            session_id="session-1",
            approval_mode=True,
            approval_id="approval-1",
            run_id="run-1",
        )
    )
    events = _events(chunks)

    assert resume_calls == [
        ("approval-1", "alice", "session-1", "run-1")
    ]
    assert execution_calls == []
    assert llm_calls == []
    assert chunks[-1] == "data: [DONE]\n\n"
    assert events == [
        {
            "type": "approval_error",
            "approvalId": "approval-1",
            "runId": "run-1",
            "error": error_message,
        }
    ]
