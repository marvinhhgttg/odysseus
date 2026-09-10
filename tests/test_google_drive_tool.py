"""Coverage for the read-only Google Drive organizer agent tool bridge."""

import json

import pytest

from src import agent_tools  # noqa: F401  # ensure full tool-module init order (app parity)
from src.tools import google_drive as google_drive_tool
from src.tools.google_drive import (
    _ALLOWED_ACTIONS,
    _compact_plan,
    _compact_scan,
    do_manage_google_drive,
)
from src.tool_index import BUILTIN_TOOL_DESCRIPTIONS
from src.tool_risk import READ_ONLY_TOOLS
from src.tool_schemas import FUNCTION_TOOL_SCHEMAS
from src.tool_security import PLAN_MODE_READONLY_TOOLS


def _fake_request(result=None):
    calls = []

    async def _req(method, path, *, owner=None, payload=None):
        calls.append(
            {"method": method, "path": path, "owner": owner, "payload": payload}
        )
        return result if result is not None else {}

    _req.calls = calls
    return _req


async def test_allowed_actions_are_read_only():
    assert _ALLOWED_ACTIONS == {"scan", "plan", "get_plan", "list_actions"}
    for forbidden in ("approve", "apply", "move", "rename", "delete"):
        assert forbidden not in _ALLOWED_ACTIONS


async def test_invalid_json_arguments(monkeypatch):
    monkeypatch.setattr(google_drive_tool, "_request", _fake_request({}))
    result = await do_manage_google_drive("not-json{")
    assert result["exit_code"] == 1
    assert result["error"] == "Invalid JSON arguments."


async def test_body_envelope_is_unwrapped(monkeypatch):
    fake = _fake_request(
        {"scan_run_id": "S1", "status": "complete", "preview_files": []}
    )
    monkeypatch.setattr(google_drive_tool, "_request", fake)
    result = await do_manage_google_drive(
        json.dumps({"body": {"action": "scan", "integration_id": "i1"}})
    )
    assert result["exit_code"] == 0
    assert fake.calls == [
        {
            "method": "POST",
            "path": "/api/google-drive-organizer/scan",
            "owner": None,
            "payload": {
                "integration_id": "i1",
                "scope": {"mode": "my_drive", "max_files": 25},
            },
        }
    ]


@pytest.mark.parametrize(
    "action",
    ["approve", "apply", "move", "rename", "delete", "approve_plan", "apply_plan"],
)
async def test_write_actions_are_rejected(monkeypatch, action):
    monkeypatch.setattr(google_drive_tool, "_request", _fake_request({}))
    result = await do_manage_google_drive(json.dumps({"action": action}))
    assert result["exit_code"] == 1
    assert "Unsupported action" in result["error"]
    for allowed in ("scan", "plan", "get_plan", "list_actions"):
        assert allowed in result["error"]


@pytest.mark.parametrize(
    ("alias", "expected_path"),
    [
        ("inventory", "/api/google-drive-organizer/scan"),
        ("analyze", "/api/google-drive-organizer/scan"),
        ("analyse", "/api/google-drive-organizer/scan"),
        ("create_plan", "/api/google-drive-organizer/plans"),
        ("build_plan", "/api/google-drive-organizer/plans"),
        ("show_plan", "/api/google-drive-organizer/plans/p1"),
        ("actions", "/api/google-drive-organizer/plans/p1/actions"),
    ],
)
async def test_action_aliases(monkeypatch, alias, expected_path):
    fake = _fake_request({})
    monkeypatch.setattr(google_drive_tool, "_request", fake)

    args = {"action": alias}
    if alias in ("inventory", "analyze", "analyse"):
        args["integration_id"] = "i1"
    elif alias in ("create_plan", "build_plan"):
        args["integration_id"] = "i1"
        args["scan_run_id"] = "s1"
    else:
        args["plan_id"] = "p1"

    result = await do_manage_google_drive(json.dumps(args))
    assert result["exit_code"] == 0, result
    assert fake.calls[-1]["path"] == expected_path


async def test_scan_requires_integration_id(monkeypatch):
    fake = _fake_request({})
    monkeypatch.setattr(google_drive_tool, "_request", fake)
    result = await do_manage_google_drive('{"action": "scan"}')
    assert result == {"error": "scan requires integration_id.", "exit_code": 1}
    assert fake.calls == []


async def test_scan_folder_mode_requires_folder_id(monkeypatch):
    fake = _fake_request({})
    monkeypatch.setattr(google_drive_tool, "_request", fake)
    content = json.dumps(
        {
            "action": "scan",
            "integration_id": "i1",
            "scope": {"mode": "folder"},
        }
    )
    result = await do_manage_google_drive(content)
    assert result == {
        "error": "folder mode requires scope.folder_id.",
        "exit_code": 1,
    }
    assert fake.calls == []


async def test_scan_rejects_invalid_mode(monkeypatch):
    fake = _fake_request({})
    monkeypatch.setattr(google_drive_tool, "_request", fake)
    content = json.dumps(
        {
            "action": "scan",
            "integration_id": "i1",
            "scope": {"mode": "shared_drive"},
        }
    )
    result = await do_manage_google_drive(content)
    assert result["exit_code"] == 1
    assert result["error"] == "scope.mode must be 'my_drive' or 'folder'."
    assert fake.calls == []


async def test_scan_rejects_non_numeric_max_files(monkeypatch):
    fake = _fake_request({})
    monkeypatch.setattr(google_drive_tool, "_request", fake)
    content = json.dumps(
        {
            "action": "scan",
            "integration_id": "i1",
            "scope": {"max_files": "many"},
        }
    )
    result = await do_manage_google_drive(content)
    assert result["exit_code"] == 1
    assert result["error"] == "max_files must be a number."
    assert fake.calls == []


async def test_scan_clamps_max_files_to_100(monkeypatch):
    fake = _fake_request({"scan_run_id": "S", "preview_files": []})
    monkeypatch.setattr(google_drive_tool, "_request", fake)
    await do_manage_google_drive(
        json.dumps(
            {
                "action": "scan",
                "integration_id": "i1",
                "scope": {"max_files": 500},
            }
        )
    )
    assert fake.calls[0]["payload"]["scope"]["max_files"] == 100


async def test_scan_sends_owner_and_scope(monkeypatch):
    fake = _fake_request({"scan_run_id": "S", "preview_files": []})
    monkeypatch.setattr(google_drive_tool, "_request", fake)
    content = json.dumps(
        {
            "action": "scan",
            "integration_id": "i1",
            "mode": "folder",
            "folder_id": "f1",
            "max_files": 5,
            "page_token": "tok",
        }
    )
    await do_manage_google_drive(content, owner="bob")
    assert fake.calls[0]["owner"] == "bob"
    assert fake.calls[0]["payload"] == {
        "integration_id": "i1",
        "scope": {
            "mode": "folder",
            "max_files": 5,
            "folder_id": "f1",
            "page_token": "tok",
        },
    }


async def test_scan_compacts_snake_case_response(monkeypatch):
    preview = [{"name": f"file-{i}"} for i in range(30)]
    raw = {
        "scan_run_id": "S1",
        "status": "complete",
        "file_count_scanned": 30,
        "file_count_indexed": 25,
        "next_page_token": "tok2",
        "error_summary": "two warnings",
        "preview_files": preview,
    }
    fake = _fake_request(raw)
    monkeypatch.setattr(google_drive_tool, "_request", fake)
    result = await do_manage_google_drive(
        json.dumps({"action": "scan", "integration_id": "i1"})
    )
    assert result["exit_code"] == 0
    compact = result["data"]
    assert compact["scan_run_id"] == "S1"
    assert compact["file_count_scanned"] == 30
    assert compact["next_page_token"] == "tok2"
    assert len(compact["preview_files"]) == 20
    assert json.loads(result["results"]) == compact


async def test_scan_handles_camel_case_response(monkeypatch):
    raw = {
        "scanRunId": "S2",
        "status": "paged",
        "fileCountScanned": 12,
        "fileCountIndexed": 12,
        "nextPageToken": "n",
        "previewFiles": [{"name": "a"}],
    }
    fake = _fake_request(raw)
    monkeypatch.setattr(google_drive_tool, "_request", fake)
    result = await do_manage_google_drive(
        json.dumps({"action": "scan", "integration_id": "i1"})
    )
    assert result["exit_code"] == 0
    assert result["data"]["scan_run_id"] == "S2"
    assert result["data"]["next_page_token"] == "n"
    assert result["data"]["preview_files"] == [{"name": "a"}]


async def test_plan_requires_scan_run_id(monkeypatch):
    fake = _fake_request({})
    monkeypatch.setattr(google_drive_tool, "_request", fake)
    result = await do_manage_google_drive(
        json.dumps({"action": "plan", "integration_id": "i1"})
    )
    assert result == {"error": "plan requires scan_run_id.", "exit_code": 1}
    assert fake.calls == []


async def test_plan_passes_optional_fields_through(monkeypatch):
    fake = _fake_request({"plan_id": "P1", "status": "proposed", "actions": []})
    monkeypatch.setattr(google_drive_tool, "_request", fake)
    content = json.dumps(
        {
            "action": "plan",
            "integration_id": "i1",
            "scan_run_id": "s1",
            "policy_id": "pol",
            "instructions": "move drafts",
            "strategy": "fast",
            "unrelated": "ignored",
        }
    )
    result = await do_manage_google_drive(content)
    assert result["exit_code"] == 0, result
    assert fake.calls[0]["payload"] == {
        "integration_id": "i1",
        "scan_run_id": "s1",
        "policy_id": "pol",
        "instructions": "move drafts",
        "strategy": "fast",
    }
    assert fake.calls[0]["path"] == "/api/google-drive-organizer/plans"


async def test_plan_omits_empty_optional_fields(monkeypatch):
    fake = _fake_request({"plan_id": "P1", "status": "proposed"})
    monkeypatch.setattr(google_drive_tool, "_request", fake)
    content = json.dumps(
        {
            "action": "plan",
            "integration_id": "i1",
            "scan_run_id": "s1",
            "instructions": "",
            "strategy": None,
        }
    )
    await do_manage_google_drive(content)
    assert fake.calls[0]["payload"] == {"integration_id": "i1", "scan_run_id": "s1"}


async def test_plan_compacts_response(monkeypatch):
    raw = {
        "plan_id": "P1",
        "status": "proposed",
        "scan_run_id": "s1",
        "action_count": 7,
        "summary": "Organize drafts",
        "created_at": "2026-01-01",
    }
    fake = _fake_request(raw)
    monkeypatch.setattr(google_drive_tool, "_request", fake)
    result = await do_manage_google_drive(
        json.dumps({"action": "plan", "integration_id": "i1", "scan_run_id": "s1"})
    )
    assert result["exit_code"] == 0
    assert result["data"]["plan_id"] == "P1"
    assert result["data"]["action_count"] == 7
    assert json.loads(result["results"]) == result["data"]


async def test_get_plan_requires_plan_id(monkeypatch):
    fake = _fake_request({})
    monkeypatch.setattr(google_drive_tool, "_request", fake)
    result = await do_manage_google_drive('{"action": "get_plan"}')
    assert result == {"error": "get_plan requires plan_id.", "exit_code": 1}
    assert fake.calls == []


async def test_get_plan_includes_raw_plan(monkeypatch):
    raw = {"plan_id": "P1", "status": "proposed", "summary": "s"}
    fake = _fake_request(raw)
    monkeypatch.setattr(google_drive_tool, "_request", fake)
    result = await do_manage_google_drive('{"action": "get_plan", "plan_id": "P1"}')
    assert result["exit_code"] == 0
    assert fake.calls[0]["method"] == "GET"
    assert fake.calls[0]["path"] == "/api/google-drive-organizer/plans/P1"
    assert result["data"]["raw_plan"] == raw


async def test_list_actions_requires_plan_id(monkeypatch):
    fake = _fake_request({})
    monkeypatch.setattr(google_drive_tool, "_request", fake)
    result = await do_manage_google_drive('{"action": "list_actions"}')
    assert result == {"error": "list_actions requires plan_id.", "exit_code": 1}


async def test_list_actions_truncates_long_lists(monkeypatch):
    actions = [{"id": i} for i in range(150)]
    fake = _fake_request({"actions": actions})
    monkeypatch.setattr(google_drive_tool, "_request", fake)
    result = await do_manage_google_drive(
        json.dumps({"action": "list_actions", "plan_id": "P1"})
    )
    assert result["exit_code"] == 0
    assert result["data"]["action_count"] == 150
    assert result["data"]["truncated"] is True
    assert len(result["data"]["actions"]) == 100
    assert fake.calls[0]["path"] == "/api/google-drive-organizer/plans/P1/actions"


async def test_list_actions_handles_object_response(monkeypatch):
    fake = _fake_request({"foo": "bar"})
    monkeypatch.setattr(google_drive_tool, "_request", fake)
    result = await do_manage_google_drive(
        json.dumps({"action": "list_actions", "plan_id": "P1"})
    )
    assert result["exit_code"] == 0
    assert result["data"] == {"plan_id": "P1", "actions": {"foo": "bar"}}


async def test_http_error_response_is_forwarded(monkeypatch):
    error_result = {
        "exit_code": 1,
        "error": "Google Drive Organizer returned HTTP 400: {'detail': 'denied'}",
    }
    fake = _fake_request(error_result)
    monkeypatch.setattr(google_drive_tool, "_request", fake)
    result = await do_manage_google_drive(
        json.dumps({"action": "scan", "integration_id": "i1"})
    )
    assert result == error_result


async def test_unreachable_organizer_returns_error(monkeypatch):
    monkeypatch.setattr(
        google_drive_tool, "_INTERNAL_BASE", "http://127.0.0.1:9"
    )
    result = await do_manage_google_drive(
        json.dumps({"action": "scan", "integration_id": "i1"})
    )
    assert result["exit_code"] == 1
    assert "Google Drive Organizer is unreachable" in result["error"]


def test_compact_scan_supports_both_key_conventions():
    snake = {"file_count_scanned": 2}
    camel = {"fileCountScanned": 3}
    assert _compact_scan(snake)["file_count_scanned"] == 2
    assert _compact_scan(camel)["file_count_scanned"] == 3


def test_compact_plan_supports_both_key_conventions():
    assert _compact_plan({"planId": "X"})["plan_id"] == "X"
    assert _compact_plan({"id": "Y"})["plan_id"] == "Y"
    assert _compact_plan({})["plan_id"] is None


def test_tool_registration_is_read_only():
    schema = next(
        entry
        for entry in FUNCTION_TOOL_SCHEMAS
        if entry.get("function", {}).get("name") == "manage_google_drive"
    )
    params = schema["function"]["parameters"]
    assert set(params["properties"]["action"]["enum"]) == _ALLOWED_ACTIONS

    assert "manage_google_drive" in READ_ONLY_TOOLS
    assert "manage_google_drive" in PLAN_MODE_READONLY_TOOLS
    assert "manage_google_drive" in BUILTIN_TOOL_DESCRIPTIONS
    description = BUILTIN_TOOL_DESCRIPTIONS["manage_google_drive"]
    assert "read-only" in description.lower()
    assert "cannot" in description.lower()