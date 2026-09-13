"""Tests for the morning-briefing engine, Google Tasks wiring and tool hooks."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from unittest.mock import patch

from services.briefing import briefing_engine as be
from src.services.google_oauth_service import provider_for_requested_scopes


# ── OAuth provider derivation ────────────────────────────────────────────────

def test_provider_for_scopes_tasks():
    assert (
        provider_for_requested_scopes(
            ["https://www.googleapis.com/auth/tasks", "openid", "email", "profile"]
        )
        == "google_tasks"
    )
    assert provider_for_requested_scopes(
        ["https://www.googleapis.com/auth/drive.readonly", "openid", "email"]
    ) == "google_drive"
    assert provider_for_requested_scopes([]) == "google_drive"
    assert provider_for_requested_scopes(None) == "google_drive"


# ── Conflict detection ───────────────────────────────────────────────────────

def test_find_conflicts_all_day_vs_timed():
    now = datetime.now().replace(microsecond=0)
    events = [
        {
            "summary": "Schwarze Tonne",
            "calendar": "Marc & Nat",
            "all_day": True,
            "start": now.replace(hour=0, minute=0),
            "end": now.replace(hour=0, minute=0) + timedelta(days=1),
        },
        {
            "summary": "Leon Musik",
            "calendar": "Marc & Nat",
            "all_day": False,
            "start": now.replace(hour=16, minute=15),
            "end": now.replace(hour=17, minute=15),
        },
        {
            "summary": "Training",
            "calendar": "Fußball",
            "all_day": False,
            "start": now.replace(hour=18, minute=0),
            "end": now.replace(hour=19, minute=0),
        },
    ]
    conflicts = be._find_conflicts(events)
    assert len(conflicts) == 1
    c = conflicts[0]
    assert c["a_summary"] in ("Schwarze Tonne", "Leon Musik")
    assert c["b_summary"] in ("Schwarze Tonne", "Leon Musik")
    assert "ganztägig" in (c["a_time"], c["b_time"])


def test_find_conflicts_info_calendars_do_not_conflict():
    now = datetime.now().replace(microsecond=0)
    events = [
        {
            "summary": "Wochennummer",
            "calendar": "Wochennummer",
            "all_day": False,
            "start": now.replace(hour=9, minute=0),
            "end": now.replace(hour=10, minute=0),
        },
        {
            "summary": "Ferien Bayern",
            "calendar": "Ferien",
            "all_day": True,
            "start": now.replace(hour=0, minute=0),
            "end": now.replace(hour=0, minute=0) + timedelta(days=1),
        },
    ]
    assert be._find_conflicts(events) == []


# ── Task bucketing ───────────────────────────────────────────────────────────

def test_group_tasks_buckets():
    now = datetime(2026, 9, 12, 9, 0)
    tasks = [
        {"title": "old", "due": now - timedelta(days=1), "list_title": "L", "is_subtask": False},
        {"title": "today", "due": now.replace(hour=8, minute=0), "list_title": "L", "is_subtask": False},
        {"title": "week", "due": now + timedelta(days=3), "list_title": "L", "is_subtask": False},
        {"title": "later", "due": now + timedelta(days=10), "list_title": "L", "is_subtask": False},
        {"title": "nodate", "due": None, "list_title": "L", "is_subtask": False},
    ]
    groups = be._group_tasks(tasks, now)
    assert [t["title"] for t in groups["ueberfaellig"]] == ["old"]
    assert [t["title"] for t in groups["heute"]] == ["today"]
    assert [t["title"] for t in groups["woche"]] == ["week"]
    assert [t["title"] for t in groups["spaeter"]] == ["later"]
    assert [t["title"] for t in groups["ohne"]] == ["nodate"]


# ── Calendar deep links ──────────────────────────────────────────────────────

def test_calendar_deep_link_builds_template_url():
    url = be._calendar_deep_link(
        "Schwarze Tonne abholen",
        "2026-09-12T08:00",
        "2026-09-12T09:00",
        "Musterstadt",
        "Bürgerbüro",
    )
    assert url.startswith("https://calendar.google.com/calendar/render?")
    assert "action=TEMPLATE" in url
    assert "dates=202609120800/202609120900" in url


def test_calendar_deep_link_malformed_start_returns_empty():
    assert be._calendar_deep_link("X", "", "2026-09-12T09:00") == ""


# ── Engine assembly (offline, deterministic) ─────────────────────────────────

def test_morning_briefing_renders_sections_offline():
    async def _run():
        with (
            patch.object(be, "_resolve_candidates", return_value=[]),
            patch.object(
                be,
                "_gather_tasks",
                return_value={
                    "available": True,
                    "source": "Notizen",
                    "connected_email": "",
                    "tasks": [],
                },
            ),
        ):
            return await be.generate_morning_briefing(owner="", max_emails=10)

    text = asyncio.run(_run())
    for section in (
        "# Morgenbriefing",
        "## 1. Mails",
        "### 1a. Neu (letzte 24 h)",
        "## 2. Kalender heute",
        "## 3. Ausblick 7 Tage",
        "## 4. Offene Tasks",
        "## 5. Top-5-Aktionen",
        "## 6. Vorgeschlagene Aktionen",
    ):
        assert section in text


# ── Registration / wiring ────────────────────────────────────────────────────

def test_morning_briefing_action_registered():
    from src.builtin_actions import BUILTIN_ACTIONS, BUILTIN_ACTION_INFO

    assert "morning_briefing" in BUILTIN_ACTIONS
    assert "morning_briefing" in BUILTIN_ACTION_INFO


def test_morning_briefing_is_model_backed():
    from src.task_scheduler import TaskScheduler

    assert "morning_briefing" in TaskScheduler._MODEL_BACKED_ACTIONS
    assert "morning_briefing" not in TaskScheduler._SILENT_ACTIONS


def test_tasks_oauth_router_registered():
    from routes.google_oauth_routes import google_tasks_router

    assert google_tasks_router.prefix == "/api/auth/integrations/google-tasks"
    expected = {
        ("POST", "/api/auth/integrations/google-tasks/connect"),
        ("GET", "/api/auth/integrations/google-tasks/callback"),
        ("GET", "/api/auth/integrations/google-tasks/{integration_id}/oauth-status"),
        ("POST", "/api/auth/integrations/google-tasks/{integration_id}/refresh-token"),
        ("POST", "/api/auth/integrations/google-tasks/{integration_id}/disconnect"),
    }
    found = {
        (next(iter(getattr(route, "methods", set() or {"GET"}))), route.path)
        for route in google_tasks_router.routes
        if hasattr(route, "path")
    }
    assert expected <= found


def test_get_briefing_tool_registered():
    from src.tool_execution import _execute_tool_block_impl  # noqa: F401 (import check)
    from src.tools.briefing import do_get_briefing
    from src.tool_implementations import do_get_briefing as reexported
    from src.agent_tools import TOOL_TAGS

    assert callable(do_get_briefing)
    assert reexported is do_get_briefing
    assert "get_briefing" in TOOL_TAGS


def test_get_briefing_briefing_tagged_read_only():
    from src.tool_policy import _COMMON_TOOL_NAMES

    assert "get_briefing" in _COMMON_TOOL_NAMES


def test_google_tasks_client_builds_from_integration():
    from src.services.google_tasks_client import GoogleTasksClient

    client = GoogleTasksClient.from_integration(
        {"base_url": "https://www.googleapis.com", "oauth_access_token": "tok"}
    )
    assert client.access_token == "tok"
    assert client.base_url == "https://www.googleapis.com"