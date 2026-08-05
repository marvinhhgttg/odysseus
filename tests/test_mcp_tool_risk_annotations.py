from src.mcp_manager import McpManager
from src.tool_risk import ToolRisk, classify_tool_risk


def test_get_tool_annotations_returns_exact_metadata():
    manager = McpManager()
    annotations = {
        "readOnlyHint": True,
        "destructiveHint": False,
    }
    manager._tools["demo"] = [
        {
            "name": "remove_everything",
            "annotations": annotations,
        }
    ]

    assert (
        manager.get_tool_annotations(
            "mcp__demo__remove_everything"
        )
        is annotations
    )


def test_get_tool_annotations_rejects_unknown_names():
    manager = McpManager()
    manager._tools["demo"] = [
        {
            "name": "list_items",
            "annotations": {"readOnlyHint": True},
        }
    ]

    assert manager.get_tool_annotations("list_items") is None
    assert manager.get_tool_annotations("mcp__demo") is None
    assert (
        manager.get_tool_annotations(
            "mcp__missing__list_items"
        )
        is None
    )
    assert (
        manager.get_tool_annotations(
            "mcp__demo__missing"
        )
        is None
    )


def test_lookup_metadata_drives_risk_classification():
    manager = McpManager()
    manager._tools["demo"] = [
        {
            "name": "remove_everything",
            "annotations": {"readOnlyHint": True},
        },
        {
            "name": "list_items",
            "annotations": {"destructiveHint": True},
        },
    ]

    read_name = "mcp__demo__remove_everything"
    read = classify_tool_risk(
        read_name,
        annotations=manager.get_tool_annotations(read_name),
    )
    assert read.risk is ToolRisk.READ_ONLY
    assert read.source == "mcp_read_only_hint"

    destructive_name = "mcp__demo__list_items"
    destructive = classify_tool_risk(
        destructive_name,
        annotations=manager.get_tool_annotations(
            destructive_name
        ),
    )
    assert destructive.risk is ToolRisk.DESTRUCTIVE
    assert destructive.source == "mcp_destructive_hint"
