"""Tests for the deployment sandbox (sandbox_mode setting).

Covers the src/tool_security gate (sandbox_restricted_tool / blocked_tools_for_owner
stacking) and the src/tool_execution dispatch choke point. When the operator sets
sandbox_mode=restricted, the agent loses shell + file-write tools for EVERY caller —
admins and single-user owners included — while read-only investigation stays on.
"""

from types import SimpleNamespace

import pytest


def _make_block(tool, content):
    return SimpleNamespace(tool_type=tool, content=content)


@pytest.fixture
def restricted_settings(monkeypatch):
    """sandbox_mode=restricted; every other setting reads through to the real file."""
    from src import settings as settings_mod

    real_get = settings_mod.get_setting

    def fake_get(key, default=None):
        if key == "sandbox_mode":
            return "restricted"
        return real_get(key, default)

    monkeypatch.setattr(settings_mod, "get_setting", fake_get)
    return settings_mod


# ── Unit gates ──────────────────────────────────────────────────────────

def test_sandbox_restricted_tool_off_by_default(monkeypatch):
    from src import settings as settings_mod
    from src.tool_security import sandbox_restricted_tool

    real_get = settings_mod.get_setting

    def fake_get(key, default=None):
        if key == "sandbox_mode":
            return "off"
        return real_get(key, default)

    monkeypatch.setattr(settings_mod, "get_setting", fake_get)

    assert sandbox_restricted_tool("bash") is False
    assert sandbox_restricted_tool("write_file") is False


def test_sandbox_restricted_tool_blocks_power_tools_when_active(restricted_settings):
    from src.tool_security import sandbox_restricted_tool

    for tool in ("bash", "python", "write_file", "edit_file"):
        assert sandbox_restricted_tool(tool) is True, f"{tool} should be sandboxed"

    # Read-only investigation stays available in restricted mode.
    for tool in ("read_file", "grep", "glob", "ls", "web_search", "manage_tasks"):
        assert sandbox_restricted_tool(tool) is False, f"{tool} should stay enabled"


def test_sandbox_restricted_tool_fails_closed_on_settings_error(monkeypatch):
    from src import settings as settings_mod
    from src.tool_security import sandbox_restricted_tool

    def boom(key, default=None):
        raise RuntimeError("settings unavailable")

    monkeypatch.setattr(settings_mod, "get_setting", boom)

    assert sandbox_restricted_tool("bash") is True


def test_blocked_tools_for_owner_admin_stack_sandbox(restricted_settings, monkeypatch):
    from src.tool_security import blocked_tools_for_owner

    monkeypatch.setattr(
        "src.tool_security.owner_is_admin_or_single_user",
        lambda owner: True,
    )

    blocked = blocked_tools_for_owner("admin-user")
    assert {"bash", "python", "write_file", "edit_file"} <= blocked
    # Non-admin-only tools must NOT leak into an admin's stack just from the sandbox.
    assert "manage_mcp" not in blocked


def test_blocked_tools_for_owner_nonadmin_union(restricted_settings):
    from src.tool_security import blocked_tools_for_owner, NON_ADMIN_BLOCKED_TOOLS

    blocked = blocked_tools_for_owner("regular-user")
    assert {"bash", "python", "write_file", "edit_file"} <= blocked
    assert NON_ADMIN_BLOCKED_TOOLS <= blocked


def test_blocked_tools_for_owner_no_sandbox_when_off(monkeypatch):
    from src import settings as settings_mod
    from src.tool_security import blocked_tools_for_owner

    real_get = settings_mod.get_setting

    def fake_get(key, default=None):
        if key == "sandbox_mode":
            return "off"
        return real_get(key, default)

    monkeypatch.setattr(settings_mod, "get_setting", fake_get)
    monkeypatch.setattr(
        "src.tool_security.owner_is_admin_or_single_user",
        lambda owner: True,
    )

    assert blocked_tools_for_owner("admin-user") == set()
    assert "bash" not in blocked_tools_for_owner("admin-user")


# ── End-to-end dispatch choke point ─────────────────────────────────────

@pytest.mark.asyncio
async def test_bash_blocked_by_sandbox_even_for_admin(restricted_settings, monkeypatch):
    """sandbox_mode=restricted must block a bash call from an ADMIN owner."""
    monkeypatch.setattr(
        "src.tool_execution.owner_is_admin_or_single_user",
        lambda owner: True,
    )

    from src.tool_execution import execute_tool_block

    desc, result = await execute_tool_block(
        _make_block("bash", "echo sandboxed"),
        owner="admin-user",
    )
    assert result.get("exit_code") == 1
    assert "sandbox" in (result.get("error") or "").lower()


@pytest.mark.asyncio
async def test_write_file_blocked_by_sandbox_even_for_admin(restricted_settings, monkeypatch):
    monkeypatch.setattr(
        "src.tool_execution.owner_is_admin_or_single_user",
        lambda owner: True,
    )

    from src.tool_execution import execute_tool_block

    desc, result = await execute_tool_block(
        _make_block("write_file", "data/scratch.txt\nhello"),
        owner="admin-user",
    )
    assert result.get("exit_code") == 1
    assert "sandbox" in (result.get("error") or "").lower()


@pytest.mark.asyncio
async def test_tool_not_blocked_when_sandbox_off(monkeypatch):
    from src import settings as settings_mod
    from src.tool_execution import execute_tool_block

    real_get = settings_mod.get_setting

    def fake_get(key, default=None):
        if key == "sandbox_mode":
            return "off"
        return real_get(key, default)

    monkeypatch.setattr(settings_mod, "get_setting", fake_get)
    monkeypatch.setattr(
        "src.tool_execution.owner_is_admin_or_single_user",
        lambda owner: True,
    )

    # A non-sandboxed tool passes the sandbox gate when the mode is off.
    import src.tool_execution as te

    assert te.sandbox_restricted_tool("bash") is False

    desc, result = await execute_tool_block(
        _make_block("list_tasks", "{}"),
        owner="admin-user",
    )
    # The sandbox gate must not be the blocking reason — although the real
    # implementation may still fail for unrelated reasons, it must not report
    # a sandbox rejection.
    assert "sandbox" not in (result.get("error") or "").lower()