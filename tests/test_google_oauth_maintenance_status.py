"""Tests for sweep_status() snapshot state (module-level side effect)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

from src.services import google_oauth_maintenance as gom
from src.services.google_oauth_maintenance import (
    run_sweep,
    sweep_status,
    _reset_state_for_tests,
)


NOW = datetime(2026, 9, 8, 20, 0, tzinfo=timezone.utc)


def _mk(*, id: str = "int1", provider: str = "google_drive",
        expires_at=None, refresh_token: str = "rt", enabled: bool = True):
    return {
        "id": id,
        "provider": provider,
        "preset": provider,
        "enabled": enabled,
        "oauth_expires_at": expires_at.isoformat() if isinstance(expires_at, datetime) else expires_at,
        "oauth_refresh_token": refresh_token,
        "settings": {"refresh_token": refresh_token},
    }


@pytest.fixture(autouse=True)
def _clean_state():
    _reset_state_for_tests()
    yield
    _reset_state_for_tests()


def test_sweep_status_empty_before_first_run():
    s = sweep_status()
    assert s["last_sweep_at"] is None
    assert s["last_result"] is None
    assert s["last_refresh_at"] == {}


def test_sweep_status_populated_after_run():
    items = [_mk(id="drive1", expires_at=NOW + timedelta(hours=2))]

    async def refresh(integration):
        return integration

    asyncio.run(run_sweep(
        load_integrations=lambda: items,
        refresh=refresh,
        now=NOW,
    ))

    s = sweep_status()
    assert s["last_sweep_at"] == NOW.isoformat()
    assert s["last_result"]["checked"] == 1
    assert s["last_result"]["refreshed"] == 1
    assert s["last_result"]["errors"] == []
    assert "drive1" in s["last_refresh_at"]
    assert s["last_refresh_at"]["drive1"] == NOW.isoformat()


def test_sweep_status_records_per_integration_refresh_timestamps():
    """Only successfully-refreshed integrations get a last_refresh_at entry."""
    items = [
        _mk(id="ok", expires_at=NOW + timedelta(hours=1)),
        _mk(id="broken", expires_at=NOW + timedelta(hours=1)),
    ]

    async def refresh(integration):
        if integration["id"] == "broken":
            raise RuntimeError("invalid_grant")
        return integration

    asyncio.run(run_sweep(
        load_integrations=lambda: items,
        refresh=refresh,
        now=NOW,
    ))

    s = sweep_status()
    assert "ok" in s["last_refresh_at"]
    assert "broken" not in s["last_refresh_at"]
    assert s["last_result"]["refreshed"] == 1
    assert s["last_result"]["errors"] and "broken" in s["last_result"]["errors"][0]


def test_sweep_status_does_not_track_fresh_integrations():
    items = [_mk(id="fresh", expires_at=NOW + timedelta(days=5))]

    async def refresh(integration):
        raise AssertionError("must not refresh a fresh integration")

    asyncio.run(run_sweep(
        load_integrations=lambda: items,
        refresh=refresh,
        now=NOW,
    ))

    s = sweep_status()
    assert s["last_result"]["skipped_fresh"] == 1
    assert s["last_refresh_at"] == {}
