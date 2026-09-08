"""Unit tests for src.services.google_oauth_maintenance."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

from src.services.google_oauth_maintenance import (
    DEFAULT_WARNING_WINDOW,
    SweepResult,
    needs_refresh,
    run_sweep,
    maintenance_loop,
)


NOW = datetime(2026, 9, 8, 20, 0, tzinfo=timezone.utc)


def _mk(*, id: str = "int1", provider: str = "google_drive",
        expires_at=None, refresh_token: str = "rt", enabled: bool = True,
        preset: str | None = None) -> Dict[str, Any]:
    return {
        "id": id,
        "provider": provider,
        "preset": preset if preset is not None else provider,
        "enabled": enabled,
        "oauth_expires_at": expires_at.isoformat() if isinstance(expires_at, datetime) else expires_at,
        "oauth_refresh_token": refresh_token,
        "settings": {"refresh_token": refresh_token},
    }


# ── needs_refresh ─────────────────────────────────────────────────────────────

def test_needs_refresh_missing_expiry_is_true():
    assert needs_refresh(_mk(expires_at=None), now=NOW) is True


def test_needs_refresh_expired_is_true():
    assert needs_refresh(_mk(expires_at=NOW - timedelta(hours=1)), now=NOW) is True


def test_needs_refresh_within_window_is_true():
    assert needs_refresh(_mk(expires_at=NOW + timedelta(hours=3)), now=NOW) is True


def test_needs_refresh_outside_window_is_false():
    assert needs_refresh(_mk(expires_at=NOW + timedelta(hours=48)), now=NOW) is False


def test_needs_refresh_naive_expiry_treated_as_utc():
    # Same wall-clock as NOW but naive; must be recognised as inside window.
    naive = NOW.replace(tzinfo=None) + timedelta(minutes=30)
    assert needs_refresh(_mk(expires_at=naive.isoformat()), now=NOW) is True


def test_needs_refresh_garbage_expiry_treated_as_stale():
    assert needs_refresh(_mk(expires_at="not-a-timestamp"), now=NOW) is True


# ── run_sweep ────────────────────────────────────────────────────────────────

def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro) if False else asyncio.run(coro)


def test_sweep_refreshes_expiring_integration():
    items = [_mk(id="a", expires_at=NOW + timedelta(hours=1))]
    calls: List[str] = []

    async def refresh(integration):
        calls.append(integration["id"])
        return integration

    res = _run(run_sweep(
        load_integrations=lambda: items,
        refresh=refresh,
        now=NOW,
    ))

    assert res.refreshed == 1
    assert res.checked == 1
    assert res.skipped_fresh == 0
    assert calls == ["a"]


def test_sweep_skips_fresh_integration():
    items = [_mk(id="a", expires_at=NOW + timedelta(days=3))]
    calls: List[str] = []

    async def refresh(integration):
        calls.append(integration["id"])
        return integration

    res = _run(run_sweep(
        load_integrations=lambda: items,
        refresh=refresh,
        now=NOW,
    ))

    assert res.refreshed == 0
    assert res.skipped_fresh == 1
    assert calls == []


def test_sweep_skips_disabled_integration():
    items = [_mk(id="a", expires_at=NOW - timedelta(hours=1), enabled=False)]

    async def refresh(integration):
        raise AssertionError("must not be called for disabled row")

    res = _run(run_sweep(
        load_integrations=lambda: items,
        refresh=refresh,
        now=NOW,
    ))
    assert res.checked == 0
    assert res.refreshed == 0


def test_sweep_ignores_non_google_providers():
    items = [{"id": "x", "provider": "miniflux", "enabled": True,
              "oauth_expires_at": (NOW - timedelta(days=30)).isoformat(),
              "oauth_refresh_token": "rt"}]

    async def refresh(integration):
        raise AssertionError("miniflux must not be refreshed here")

    res = _run(run_sweep(
        load_integrations=lambda: items,
        refresh=refresh,
        now=NOW,
    ))
    assert res.checked == 0
    assert res.refreshed == 0


def test_sweep_skips_when_refresh_token_missing():
    items = [_mk(id="a", expires_at=NOW - timedelta(hours=1), refresh_token="")]

    async def refresh(integration):
        raise AssertionError("must not attempt refresh without refresh_token")

    res = _run(run_sweep(
        load_integrations=lambda: items,
        refresh=refresh,
        now=NOW,
    ))
    assert res.checked == 1
    assert res.refreshed == 0
    assert res.skipped_no_refresh_token == 1


def test_sweep_captures_refresh_error_and_continues():
    items = [
        _mk(id="broken", expires_at=NOW + timedelta(hours=1)),
        _mk(id="ok", expires_at=NOW + timedelta(hours=2)),
    ]
    calls: List[str] = []

    async def refresh(integration):
        if integration["id"] == "broken":
            raise RuntimeError("invalid_grant")
        calls.append(integration["id"])
        return integration

    res = _run(run_sweep(
        load_integrations=lambda: items,
        refresh=refresh,
        now=NOW,
    ))

    assert res.checked == 2
    assert res.refreshed == 1
    assert calls == ["ok"]
    assert len(res.errors) == 1
    assert "broken" in res.errors[0]


def test_sweep_survives_load_integrations_failure():
    def boom():
        raise IOError("disk gone")

    async def refresh(integration):
        raise AssertionError("must not be called if load failed")

    res = _run(run_sweep(
        load_integrations=boom,
        refresh=refresh,
        now=NOW,
    ))
    assert res.refreshed == 0
    assert res.errors and "disk gone" in res.errors[0]


# ── maintenance_loop ─────────────────────────────────────────────────────────

def test_maintenance_loop_stops_on_event():
    """Loop must exit promptly when stop_event fires and complete one sweep."""
    items = [_mk(id="a", expires_at=NOW + timedelta(hours=1))]
    refresh_calls: List[str] = []

    async def refresh(integration):
        refresh_calls.append(integration["id"])
        return integration

    async def scenario():
        stop = asyncio.Event()
        task = asyncio.create_task(
            maintenance_loop(
                load_integrations=lambda: items,
                refresh=refresh,
                interval_seconds=3600,
                stop_event=stop,
            )
        )
        # Let the first sweep run.
        await asyncio.sleep(0.05)
        stop.set()
        await asyncio.wait_for(task, timeout=2.0)

    asyncio.run(scenario())
    assert refresh_calls == ["a"]  # at least the first sweep ran
