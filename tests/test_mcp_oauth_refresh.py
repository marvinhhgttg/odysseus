"""Tests for the proactive MCP OAuth token refresh.

The Python MCP SDK refreshes OAuth access tokens lazily (on the first request
after expiry) and the availability watchdog reconnects crashed servers, but
nothing pre-emptively refreshed stored tokens for idle-connected servers. These
tests pin the new `refresh_mcp_server_access_token` / `refresh_mcp_servers_due`
machinery and the scheduler wiring.
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from src import mcp_oauth


def _make_db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'mcp.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    cdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _seed_server(SessionFactory, *, oauth_tokens=None, url="https://mcp.example.com", enabled=True, transport="http"):
    db = SessionFactory()
    srv = cdb.McpServer(
        id="mcp-1",
        name="Example MCP",
        transport=transport,
        url=url,
        is_enabled=enabled,
        oauth_tokens=oauth_tokens,
    )
    db.add(srv)
    db.commit()
    db.close()
    return srv


def _tokens_json(*, expires_at=None, refresh_token="rt-1", access_token="at-1"):
    tokens = {
        "tokens": {
            "access_token": access_token,
            "token_type": "Bearer",
            "refresh_token": refresh_token,
            "expires_in": 3600,
        },
        "client_info": {"client_id": "cid-1", "token_endpoint_auth_method": "none"},
    }
    if expires_at is not None:
        tokens["expires_at"] = expires_at
    return json.dumps(tokens)


async def test_valid_token_is_left_alone(tmp_path):
    SessionFactory = _make_db(tmp_path)
    future = time.time() + 3600
    _seed_server(SessionFactory, oauth_tokens=_tokens_json(expires_at=future))

    patcher = patch.object(mcp_oauth, "_perform_refresh", side_effect=AssertionError("should not refresh"))
    with patch.object(mcp_oauth, "DbTokenStorage") as mock_store, patcher:
        storage = mock_store.return_value
        storage.get_tokens = AsyncMock(return_value=SimpleNamespace(
            access_token="at-1", refresh_token="rt-1"
        ))
        storage.get_expires_at = AsyncMock(return_value=future)
        storage.set_meta = AsyncMock()
        with patch.object(cdb, "SessionLocal", SessionFactory):
            result = await mcp_oauth.refresh_mcp_server_access_token("mcp-1")

    assert result["status"] == "valid"
    assert "expires_at" in result


async def test_no_tokens_is_noop(tmp_path):
    SessionFactory = _make_db(tmp_path)
    _seed_server(SessionFactory, oauth_tokens=None)

    with patch.object(cdb, "SessionLocal", SessionFactory):
        result = await mcp_oauth.refresh_mcp_server_access_token("mcp-1")
    assert result["status"] == "no_tokens"


async def test_no_refresh_token_is_reported(tmp_path):
    SessionFactory = _make_db(tmp_path)
    _seed_server(SessionFactory, oauth_tokens=_tokens_json(refresh_token=None))

    with patch.object(cdb, "SessionLocal", SessionFactory):
        result = await mcp_oauth.refresh_mcp_server_access_token("mcp-1")
    assert result["status"] == "no_refresh_token"


async def test_disabled_or_missing_server(tmp_path):
    SessionFactory = _make_db(tmp_path)
    _seed_server(SessionFactory, oauth_tokens=_tokens_json(), enabled=False)

    with patch.object(cdb, "SessionLocal", SessionFactory):
        result = await mcp_oauth.refresh_mcp_server_access_token("mcp-1")
    assert result["status"] == "disabled_or_missing"


async def test_non_http_server_is_skipped(tmp_path):
    SessionFactory = _make_db(tmp_path)
    _seed_server(SessionFactory, oauth_tokens=_tokens_json(), url="", transport="stdio")

    with patch.object(cdb, "SessionLocal", SessionFactory):
        result = await mcp_oauth.refresh_mcp_server_access_token("mcp-1")
    assert result["status"] == "not_http"


async def test_expiring_token_is_refreshed(tmp_path):
    SessionFactory = _make_db(tmp_path)
    now = time.time()
    _seed_server(SessionFactory, oauth_tokens=_tokens_json(expires_at=now - 5))

    fake_provider = MagicMock()
    fake_provider._initialize = AsyncMock()
    fake_provider.context.storage = AsyncMock()

    with patch.object(cdb, "SessionLocal", SessionFactory), \
         patch.object(mcp_oauth, "DbTokenStorage") as mock_store, \
         patch.object(mcp_oauth, "build_provider", return_value=fake_provider), \
         patch.object(mcp_oauth, "_perform_refresh", return_value=(True, now + 7200, "")) as refresh_mock:
        storage = mock_store.return_value
        storage.get_tokens = AsyncMock(return_value=SimpleNamespace(
            access_token="at-1", refresh_token="rt-1"
        ))
        storage.get_expires_at = AsyncMock(return_value=now - 5)
        storage.set_meta = AsyncMock()
        result = await mcp_oauth.refresh_mcp_server_access_token("mcp-1")

    refresh_mock.assert_awaited_once_with(fake_provider, "https://mcp.example.com")
    assert result["status"] == "refreshed"
    assert result["expires_at"] == now + 7200


async def test_failed_refresh_is_reported(tmp_path):
    SessionFactory = _make_db(tmp_path)
    now = time.time()
    _seed_server(SessionFactory, oauth_tokens=_tokens_json(expires_at=now - 5))

    fake_provider = MagicMock()
    fake_provider._initialize = AsyncMock()

    with patch.object(cdb, "SessionLocal", SessionFactory), \
         patch.object(mcp_oauth, "DbTokenStorage") as mock_store, \
         patch.object(mcp_oauth, "build_provider", return_value=fake_provider), \
         patch.object(mcp_oauth, "_perform_refresh", return_value=(False, None, "HTTP 400")):
        storage = mock_store.return_value
        storage.get_tokens = AsyncMock(return_value=SimpleNamespace(
            access_token="at-1", refresh_token="rt-1"
        ))
        storage.get_expires_at = AsyncMock(return_value=now - 5)
        storage.set_meta = AsyncMock()
        result = await mcp_oauth.refresh_mcp_server_access_token("mcp-1")

    assert result["status"] == "refresh_failed"
    assert result["error"] == "HTTP 400"


async def test_sweep_counts_statuses(tmp_path):
    SessionFactory = _make_db(tmp_path)
    future = time.time() + 3600
    db = SessionFactory()
    db.add(cdb.McpServer(
        id="mcp-fresh", name="Fresh", transport="http",
        url="https://a.example.com", is_enabled=True,
        oauth_tokens=_tokens_json(expires_at=future),
    ))
    db.add(cdb.McpServer(
        id="mcp-plain", name="Plain", transport="stdio",
        url="", is_enabled=True, oauth_tokens=None,
    ))
    db.commit()
    db.close()

    async def fake_refresh(server_id, *, min_ttl_seconds=900):
        if server_id == "mcp-fresh":
            return {"status": "valid"}
        return {"status": "no_tokens"}

    with patch.object(cdb, "SessionLocal", SessionFactory), \
         patch.object(mcp_oauth, "refresh_mcp_server_access_token", side_effect=fake_refresh):
        summary = await mcp_oauth.refresh_mcp_servers_due()

    assert summary["checked"] == 1
    assert summary["valid"] == 1
    assert summary["no_tokens"] == 0


# ── Scheduler wiring (source pinning) ────────────────────────────────────────

def _all_functions(source, names=()):
    import ast
    tree = ast.parse(source)
    result = {}
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (not names or node.name in names):
            result[node.name] = ast.unparse(node)
    return result


def test_scheduler_has_google_and_mcp_loops():
    from pathlib import Path
    source = Path("src/task_scheduler.py").read_text()
    methods = _all_functions(source, {"_mcp_oauth_refresh_loop", "_google_calendar_loop", "start", "stop"})
    assert "refresh_mcp_servers_due" in methods["_mcp_oauth_refresh_loop"]
    assert "sync_pending_sweep" in methods["_google_calendar_loop"]
    assert "_google_calendar_loop" in methods["start"] and "_mcp_oauth_refresh_loop" in methods["start"]
    assert "_google_sync_task" in methods["stop"] and "_mcp_oauth_refresh_task" in methods["stop"]


def test_db_token_storage_persists_expiry():
    from pathlib import Path
    methods = _all_functions(Path("src/mcp_oauth.py").read_text(), {"set_tokens"})
    body = methods["set_tokens"]
    assert "calculate_token_expiry" in body
    assert "expires_at" in body


def test_refresh_machinery_exists():
    from pathlib import Path
    methods = _all_functions(Path("src/mcp_oauth.py").read_text())
    assert {"refresh_mcp_server_access_token", "refresh_mcp_servers_due",
            "_perform_refresh", "_discover_token_endpoint"} <= set(methods)