"""Tests for MCP startup/availability hardening:

- DB servers connect through fire-and-forget background tasks so one slow
  server can't block every other (and startup returns immediately).
- A crashed server is auto-reconnected by the watchdog or the next tool call,
  honouring DB enable/disable state.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.mcp_manager import McpManager


def _row(server_id="srv-1", enabled=True, name="Test Svr", command="/bin/echo"):
    return SimpleNamespace(
        id=server_id,
        name=name,
        transport="stdio",
        command=command,
        args='["--flag"]',
        env='{"KEY": "val"}',
        url=None,
        is_enabled=enabled,
    )


def _db(fake_rows):
    fake = MagicMock()
    filt = fake.query.return_value.filter.return_value
    filt.all.return_value = fake_rows
    filt.first.return_value = fake_rows[0] if fake_rows else None
    return fake


def test_should_auto_reconnect_builtin_and_enabled_only():
    mgr = McpManager()
    mgr._expected_up.add("db-server")
    assert mgr._should_auto_reconnect("memory") is True  # builtin
    assert mgr._should_auto_reconnect("builtin_browser") is True
    assert mgr._should_auto_reconnect("db-server") is True
    assert mgr._should_auto_reconnect("disabled-server") is False


def test_connect_all_enabled_spawns_bg_connects_and_marks_expected():
    mgr = McpManager()
    mgr._connect_guarded = AsyncMock(return_value=True)

    async def run():
        with patch("core.database.SessionLocal", return_value=_db([_row("a"), _row("b", enabled=False)])), \
             patch("core.database.McpServer", MagicMock()):
            await mgr.connect_all_enabled()
        # Fire-and-forget tasks settle a beat after connect_all_enabled returns.
        await asyncio.sleep(0.05)

    asyncio.run(run())

    assert mgr._expected_up == {"a"}
    assert mgr._connect_guarded.call_count == 1
    assert mgr._connect_guarded.call_args[0][0] == "a"


def test_reconnect_honours_db_disable():
    mgr = McpManager()
    mgr._params["srv-1"] = {"name": "t", "transport": "stdio", "command": "/bin/echo",
                            "args": [], "env": {}, "url": None}
    mgr._ever_connected.add("srv-1")
    mgr._expected_up.add("srv-1")

    async def run():
        with patch("core.database.SessionLocal", return_value=_db([_row(enabled=False)])), \
             patch("core.database.McpServer", MagicMock()):
            return await mgr._reconnect("srv-1")

    result = asyncio.run(run())
    assert result is False
    assert "srv-1" not in mgr._ever_connected
    assert "srv-1" not in mgr._params
    assert "srv-1" not in mgr._expected_up


def test_reconnect_rebuilds_enabled_db_server_with_fresh_row():
    mgr = McpManager()
    mgr._params["srv-1"] = {"name": "old-name", "transport": "stdio", "command": "/bin/old",
                            "args": [], "env": {}, "url": None}
    mgr.connect_server = AsyncMock(return_value=True)

    async def run():
        with patch("core.database.SessionLocal", return_value=_db([_row(enabled=True)])), \
             patch("core.database.McpServer", MagicMock()):
            return await mgr._reconnect("srv-1")

    result = asyncio.run(run())
    assert result is True
    mgr.connect_server.assert_called_once()
    kwargs = mgr.connect_server.call_args.kwargs
    assert kwargs["name"] == "Test Svr"
    assert kwargs["command"] == "/bin/echo"
    assert kwargs["args"] == ["--flag"]
    assert kwargs["env"] == {"KEY": "val"}
    assert "srv-1" in mgr._expected_up
    assert "srv-1" in mgr._ever_connected


def test_watchdog_reconnects_dead_server():
    mgr = McpManager()
    healthy = MagicMock()
    healthy.send_ping = AsyncMock(return_value=None)
    dead = MagicMock()
    dead.send_ping = AsyncMock(side_effect=RuntimeError("connection lost"))

    mgr._sessions["healthy"] = healthy
    mgr._sessions["dead"] = dead
    mgr._ever_connected.update(["healthy", "dead"])
    mgr._expected_up.add("dead")
    mgr._reconnect = AsyncMock(return_value=True)

    async def run():
        await mgr._check_connections(ping_timeout=1.0)

    asyncio.run(run())
    healthy.send_ping.assert_awaited_once()
    dead.send_ping.assert_awaited_once()
    assert mgr._reconnect.await_count == 1
    assert mgr._reconnect.await_args.args[0] == "dead"


def test_watchdog_skips_disabled_server_reconnect():
    mgr = McpManager()
    dead = MagicMock()
    dead.send_ping = AsyncMock(side_effect=RuntimeError("connection lost"))
    mgr._sessions["dead"] = dead
    mgr._ever_connected.add("dead")
    mgr._reconnect = AsyncMock(return_value=True)

    async def run():
        await mgr._check_connections(ping_timeout=1.0)

    asyncio.run(run())
    assert mgr._reconnect.await_count == 0


def test_watchdog_lifecycle_start_stop():
    mgr = McpManager()

    async def run():
        mgr.start_availability_watchdog(interval=3600, ping_timeout=1)
        assert mgr._watchdog_task is not None
        mgr.stop_availability_watchdog()
        assert mgr._watchdog_task is None

    asyncio.run(run())


def test_call_tool_auto_reconnects_enabled_db_server():
    """call_tool used to only auto-reconnect builtins; DB servers need it too."""
    mgr = McpManager()
    mgr._expected_up.add("srv-1")
    mgr._ever_connected.add("srv-1")
    mgr._params["srv-1"] = {"name": "t", "transport": "stdio", "command": "/bin/echo",
                            "args": [], "env": {}, "url": None}

    fail = MagicMock()
    fail.call_tool = AsyncMock(side_effect=RuntimeError("broken pipe"))
    work = MagicMock()
    work.call_tool = AsyncMock(
        return_value=SimpleNamespace(content=[], isError=False)
    )

    sessions = {"srv-1": fail}
    mgr._sessions = sessions

    def _swap(sid):
        sessions[sid] = work
        return True

    mgr._reconnect = AsyncMock(side_effect=_swap)

    async def run():
        return await mgr.call_tool("mcp__srv-1__some_tool", {})

    result = asyncio.run(run())
    assert result["exit_code"] == 0
    assert mgr._reconnect.await_count == 1