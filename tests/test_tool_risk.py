import logging
from types import SimpleNamespace

import pytest

from src.tool_risk import (
    ToolRisk,
    classify_tool_risk,
)


@pytest.mark.parametrize(
    ("tool", "expected"),
    [
        ("read_file", ToolRisk.READ_ONLY),
        ("glob", ToolRisk.READ_ONLY),
        ("ask_user", ToolRisk.READ_ONLY),
        ("tail_serve_output", ToolRisk.READ_ONLY),
        ("write_file", ToolRisk.LOCAL_WRITE),
        ("manage_memory", ToolRisk.LOCAL_WRITE),
        ("update_plan", ToolRisk.LOCAL_WRITE),
        ("ui_control", ToolRisk.LOCAL_WRITE),
        ("send_email", ToolRisk.EXTERNAL_WRITE),
        ("builtin_browser", ToolRisk.EXTERNAL_WRITE),
        ("delete_email", ToolRisk.DESTRUCTIVE),
        ("manage_session", ToolRisk.DESTRUCTIVE),
        ("bash", ToolRisk.HOST_CONTROL),
        ("python", ToolRisk.HOST_CONTROL),
        ("unknown_new_tool", ToolRisk.UNKNOWN),
    ],
)
def test_native_tool_risk_classification(tool, expected):
    assert classify_tool_risk(tool).risk is expected


def test_external_and_higher_risks_would_require_approval():
    for tool in ("send_email", "delete_email", "bash", "unknown_new_tool"):
        assert classify_tool_risk(tool).approval_would_be_required is True


def test_read_and_local_write_do_not_yet_require_approval():
    for tool in ("read_file", "write_file"):
        assert classify_tool_risk(tool).approval_would_be_required is False


def test_mcp_annotations_take_precedence():
    read = classify_tool_risk(
        "mcp__demo__remove_everything",
        annotations={"readOnlyHint": True},
    )
    destructive = classify_tool_risk(
        "mcp__demo__list_items",
        annotations={"destructiveHint": True},
    )

    assert read.risk is ToolRisk.READ_ONLY
    assert read.source == "mcp_read_only_hint"
    assert destructive.risk is ToolRisk.DESTRUCTIVE
    assert destructive.source == "mcp_destructive_hint"


def test_mcp_name_heuristic_and_fail_closed_default():
    read = classify_tool_risk("mcp__demo__list_items")
    unknown = classify_tool_risk("mcp__demo__process_items")

    assert read.risk is ToolRisk.READ_ONLY
    assert read.source == "mcp_name_heuristic"
    assert unknown.risk is ToolRisk.UNKNOWN
    assert unknown.approval_would_be_required is True


@pytest.mark.parametrize("tool", [None, 123, "", "   "])
def test_invalid_tool_names_are_unknown(tool):
    assessment = classify_tool_risk(tool)
    assert assessment.risk is ToolRisk.UNKNOWN
    assert assessment.approval_would_be_required is True


@pytest.mark.asyncio
async def test_execution_wrapper_emits_content_free_shadow_log(
    monkeypatch,
    caplog,
):
    import src.tool_execution as execution

    secret = "SHADOW_LOG_SECRET_X7K9"

    async def fake_impl(*args, **kwargs):
        return "done", {"output": secret, "exit_code": 0}

    monkeypatch.setattr(execution, "_execute_tool_block_impl", fake_impl)

    block = SimpleNamespace(
        tool_type="bash",
        content=f"printf '{secret}'",
    )

    with caplog.at_level(logging.INFO, logger=execution.__name__):
        description, result = await execution.execute_tool_block(
            block,
            session_id="risk-shadow-test",
        )

    assert description == "done"
    assert result["exit_code"] == 0

    shadow_records = [
        record for record in caplog.records
        if record.getMessage().startswith("Tool risk shadow ")
    ]
    assert len(shadow_records) == 1

    message = shadow_records[0].getMessage()
    assert "tool=bash" in message
    assert "risk_level=host_control" in message
    assert "approval_would_be_required=True" in message
    assert "session_id=risk-shadow-test" in message
    assert secret not in message


def test_mcp_annotations_cannot_downgrade_native_host_control():
    assessment = classify_tool_risk(
        "bash",
        annotations={"readOnlyHint": True},
    )

    assert assessment.risk is ToolRisk.HOST_CONTROL
    assert assessment.source == "native_registry"
    assert assessment.approval_would_be_required is True


def test_every_known_native_tool_has_explicit_risk_classification():
    from src.tool_policy import known_tool_names

    unknown = {
        name
        for name in known_tool_names()
        if classify_tool_risk(name).risk is ToolRisk.UNKNOWN
    }

    assert unknown == set()


@pytest.mark.asyncio
async def test_mcp_annotations_drive_shadow_log_without_blocking_execution(
    monkeypatch,
    caplog,
):
    import src.tool_execution as execution

    monkeypatch.setattr(
        execution,
        "_owner_is_admin",
        lambda owner: True,
    )

    class FakeMcpManager:
        def __init__(self):
            self.annotation_lookups = []
            self.calls = []

        def get_tool_annotations(self, qualified_name):
            self.annotation_lookups.append(qualified_name)
            return {"destructiveHint": True}

        async def call_tool(self, qualified_name, arguments):
            self.calls.append((qualified_name, arguments))
            return {
                "stdout": "executed",
                "exit_code": 0,
            }

    manager = FakeMcpManager()
    monkeypatch.setattr(
        execution,
        "get_mcp_manager",
        lambda: manager,
    )

    block = SimpleNamespace(
        tool_type="mcp__demo__list_items",
        content='{"limit": 3}',
    )

    with caplog.at_level(logging.INFO, logger=execution.__name__):
        description, result = await execution.execute_tool_block(
            block,
            session_id="mcp-risk-shadow-test",
        )

    assert description == "mcp: mcp__demo__list_items"
    assert result["exit_code"] == 0
    assert result["stdout"] == "executed"

    assert manager.annotation_lookups == [
        "mcp__demo__list_items"
    ]
    assert manager.calls == [
        (
            "mcp__demo__list_items",
            {"limit": 3},
        )
    ]

    shadow_records = [
        record
        for record in caplog.records
        if record.getMessage().startswith("Tool risk shadow ")
    ]
    assert len(shadow_records) == 1

    message = shadow_records[0].getMessage()
    assert "tool=mcp__demo__list_items" in message
    assert "risk_level=destructive" in message
    assert "risk_source=mcp_destructive_hint" in message
    assert "approval_would_be_required=True" in message
    assert "session_id=mcp-risk-shadow-test" in message
    assert "limit" not in message
    assert "executed" not in message
