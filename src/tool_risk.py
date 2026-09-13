"""Central tool-risk classification for approval shadow mode."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping, Optional


class ToolRisk(str, Enum):
    READ_ONLY = "read_only"
    LOCAL_WRITE = "local_write"
    EXTERNAL_WRITE = "external_write"
    DESTRUCTIVE = "destructive"
    HOST_CONTROL = "host_control"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ToolRiskAssessment:
    risk: ToolRisk
    source: str
    approval_would_be_required: bool


READ_ONLY_TOOLS = frozenset({
    "read_file",
    "grep",
    "glob",
    "ls",
    "get_workspace",
    "web_search",
    "web_fetch",
    "search_chats",
    "list_models",
    "list_sessions",
    "list_email_accounts",
    "list_emails",
    "read_email",
    "search_emails",
    "list_served_models",
    "list_downloads",
    "list_cached_models",
    "search_hf_models",
    "list_serve_presets",
    "list_cookbook_servers",
    "resolve_contact",
    "chat_with_model",
    "ask_teacher",
    "ask_user",
    "tail_serve_output",
    "vault_search",
    "vault_get",
    "manage_google_drive",
    "get_briefing",
})

LOCAL_WRITE_TOOLS = frozenset({
    "write_file",
    "edit_file",
    "create_document",
    "edit_document",
    "update_document",
    "suggest_document",
    "manage_documents",
    "create_session",
    "send_to_session",
    "pipeline",
    "manage_memory",
    "manage_skills",
    "manage_tasks",
    "manage_notes",
    "generate_image",
    "edit_image",
    "trigger_research",
    "manage_research",
    "update_plan",
    "ui_control",
    "draft_email",
    "draft_email_reply",
    "ai_draft_email_reply",
    "download_attachment",
})

EXTERNAL_WRITE_TOOLS = frozenset({
    "builtin_browser",
    "send_email",
    "reply_to_email",
    "bulk_email",
    "mark_email_read",
    "archive_email",
    "manage_calendar",
    "manage_contact",
    "manage_webhooks",
})

DESTRUCTIVE_TOOLS = frozenset({
    "delete_email",
    "manage_session",
    "cancel_download",
    "stop_served_model",
})

HOST_CONTROL_TOOLS = frozenset({
    "bash",
    "python",
    "manage_bg_jobs",
    "manage_endpoints",
    "manage_mcp",
    "manage_tokens",
    "manage_settings",
    "api_call",
    "app_api",
    "vault_unlock",
    "download_model",
    "serve_model",
    "serve_preset",
    "adopt_served_model",
})

_MCP_READ_PREFIXES = (
    "list",
    "get",
    "read",
    "search",
    "fetch",
    "query",
    "find",
    "describe",
    "show",
    "view",
    "lookup",
    "count",
    "status",
    "info",
    "inspect",
    "summar",
)


def _annotation_value(annotations: Any, name: str) -> Optional[bool]:
    if annotations is None:
        return None
    if isinstance(annotations, Mapping):
        value = annotations.get(name)
    else:
        value = getattr(annotations, name, None)
    return value if isinstance(value, bool) else None


def _assessment(risk: ToolRisk, source: str) -> ToolRiskAssessment:
    return ToolRiskAssessment(
        risk=risk,
        source=source,
        approval_would_be_required=risk in {
            ToolRisk.EXTERNAL_WRITE,
            ToolRisk.DESTRUCTIVE,
            ToolRisk.HOST_CONTROL,
            ToolRisk.UNKNOWN,
        },
    )


def _bare_mcp_name(tool_name: str) -> str:
    if not tool_name.startswith("mcp__"):
        return tool_name
    parts = tool_name.split("__", 2)
    return parts[2] if len(parts) == 3 and parts[2] else tool_name


def classify_tool_risk(
    tool_name: object,
    *,
    annotations: Any = None,
) -> ToolRiskAssessment:
    """Classify a tool without inspecting or logging its arguments."""

    if not isinstance(tool_name, str) or not tool_name.strip():
        return _assessment(ToolRisk.UNKNOWN, "invalid_tool_name")

    name = tool_name.strip().lower()
    is_mcp = name.startswith("mcp__")
    bare_name = _bare_mcp_name(name)

    if is_mcp:
        destructive_hint = _annotation_value(annotations, "destructiveHint")
        read_only_hint = _annotation_value(annotations, "readOnlyHint")

        if destructive_hint is True:
            return _assessment(ToolRisk.DESTRUCTIVE, "mcp_destructive_hint")
        if read_only_hint is True:
            return _assessment(ToolRisk.READ_ONLY, "mcp_read_only_hint")
        if read_only_hint is False:
            return _assessment(ToolRisk.EXTERNAL_WRITE, "mcp_not_read_only_hint")

    if bare_name in HOST_CONTROL_TOOLS:
        return _assessment(ToolRisk.HOST_CONTROL, "native_registry")
    if bare_name in DESTRUCTIVE_TOOLS:
        return _assessment(ToolRisk.DESTRUCTIVE, "native_registry")
    if bare_name in EXTERNAL_WRITE_TOOLS:
        return _assessment(ToolRisk.EXTERNAL_WRITE, "native_registry")
    if bare_name in LOCAL_WRITE_TOOLS:
        return _assessment(ToolRisk.LOCAL_WRITE, "native_registry")
    if bare_name in READ_ONLY_TOOLS:
        return _assessment(ToolRisk.READ_ONLY, "native_registry")

    if is_mcp and bare_name.startswith(_MCP_READ_PREFIXES):
        return _assessment(ToolRisk.READ_ONLY, "mcp_name_heuristic")
    if is_mcp:
        return _assessment(ToolRisk.UNKNOWN, "mcp_unclassified")

    return _assessment(ToolRisk.UNKNOWN, "native_unclassified")
