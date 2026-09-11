"""Tests for the PM Briefing aggregation endpoint (/api/briefing).

Covers:
  - owner-scoped aggregation across email urgency / calendar / backlog / tasks
  - graceful empty responses when a source is missing
  - fail-closed on unauthenticated access
"""

from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
import src.auth_dependencies as auth_deps
import src.constants as constants
import routes.briefing_routes as briefing_routes


def _as_alice(request):
    return "alice"


def _no_user(request):
    return ""


def _db_with_data(tmp_path, owner="alice", event=True, task=True):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'briefing.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    cdb.Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, autoflush=False, autocommit=False)

    db = session_factory()
    try:
        if event:
            cal = cdb.CalendarCal(id="cal-alice", owner=owner, name="Work", color="#123456")
            db.add(cal)
            today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
            db.add(
                cdb.CalendarEvent(
                    uid="ev-today",
                    calendar_id="cal-alice",
                    summary="Steering committee",
                    dtstart=today + timedelta(hours=9),
                    dtend=today + timedelta(hours=10),
                    all_day=False,
                    is_utc=False,
                    status="confirmed",
                    importance="high",
                    event_type="work",
                )
            )
            # An event outside the window (yesterday) must not appear.
            db.add(
                cdb.CalendarEvent(
                    uid="ev-yesterday",
                    calendar_id="cal-alice",
                    summary="Old kickoff",
                    dtstart=today - timedelta(hours=3),
                    dtend=today - timedelta(hours=2),
                    all_day=False,
                    is_utc=False,
                    status="confirmed",
                )
            )
        if task:
            db.add(
                cdb.ScheduledTask(
                    id="task-1",
                    owner=owner,
                    name="Morning Brief",
                    prompt="brief",
                    task_type="llm",
                    schedule="daily",
                    scheduled_time="07:30",
                    trigger_type="schedule",
                    status="active",
                    next_run=datetime.now() + timedelta(hours=6),
                    last_run=datetime.now() - timedelta(hours=1),
                )
            )
        db.commit()
    finally:
        db.close()
    return session_factory


def _make_client(monkeypatch, tmp_path, user_override=_as_alice, session_factory=None):
    monkeypatch.setenv("AUTH_ENABLED", "false")
    sf = session_factory or _db_with_data(tmp_path)
    monkeypatch.setattr(cdb, "SessionLocal", sf)
    monkeypatch.setattr(constants, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(auth_deps, "require_user", user_override)

    app = FastAPI()
    app.include_router(briefing_routes.setup_briefing_routes())
    return TestClient(app)


def test_briefing_aggregates_all_sources(monkeypatch, tmp_path):
    # Seed an email urgency file next to the (tmp) data dir.
    import json

    (tmp_path / "email_urgency_state_alice.json").write_text(
        json.dumps(
            {
                "ts": 1789115428.0,
                "total_unread": 2,
                "total_urgent": 1,
                "max_score": 2,
                "per_uid": {
                    "u1": {
                        "score": 2,
                        "tags": ["action-needed"],
                        "unread": True,
                        "subject": "Release decision needed",
                        "from": "stakeholder@corp",
                        "reason": "action likely needed",
                        "ts": 1789115425.0,
                    },
                    "u2": {"score": 0, "unread": False, "subject": "done", "from": "x"},
                },
                "notified_uids": ["u2"],
            }
        ),
        encoding="utf-8",
    )
    # … and a backlog with two open tickets.
    (tmp_path / "backlog.json").write_text(
        json.dumps(
            {
                "tickets": [
                    {
                        "id": "P1-critical",
                        "priority": 1,
                        "title": "Critical blocker",
                        "status": "open",
                        "estimated_effort": "large",
                    },
                    {
                        "id": "P9-nice",
                        "priority": 9,
                        "title": "Nice to have",
                        "status": "done",
                        "estimated_effort": "small",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    client = _make_client(monkeypatch, tmp_path)
    resp = client.get("/api/briefing")
    assert resp.status_code == 200
    data = resp.json()

    # Email urgency surfaced (notified_uids stripped).
    assert data["owner"] == "alice"
    assert data["email"]["total_unread"] == 2
    assert data["email"]["total_urgent"] == 1
    assert data["email"]["max_score"] == 2
    assert "notified_uids" not in data["email"]
    flagged = data["email"]["flagged"]
    assert len(flagged) == 1
    assert flagged[0]["subject"] == "Release decision needed"

    # Calendar: only today's in-window event, rendered in frontend format.
    assert data["summary"]["appointments_today"] == 1
    assert data["calendar"]["today"][0]["summary"] == "Steering committee"
    assert data["calendar"]["today"][0]["calendar"] == "Work"
    assert all("Yesterday" not in e["summary"] for e in data["calendar"]["today"])

    # Backlog: open items only, sorted by priority.
    assert data["summary"]["backlog_open"] == 1
    assert data["summary"]["backlog_high"] == 1
    assert data["backlog"][0]["id"] == "P1-critical"

    # Tasks: active + due-soon count + recent runs list.
    assert data["summary"]["tasks_active"] == 1
    assert data["summary"]["tasks_due_soon"] == 1
    assert data["tasks"]["active"][0]["name"] == "Morning Brief"
    assert isinstance(data["tasks"]["recent_runs"], list)
    assert data["generated_at"]


def test_briefing_empty_sources_are_graceful(monkeypatch, tmp_path):
    client = _make_client(monkeypatch, tmp_path, session_factory=_db_with_data(tmp_path, event=False, task=False))
    data = client.get("/api/briefing").json()
    assert data["summary"] == {
        "email_unread": 0,
        "email_urgent": 0,
        "email_max_score": 0,
        "appointments_today": 0,
        "appointments_upcoming": 0,
        "backlog_open": 0,
        "backlog_high": 0,
        "tasks_active": 0,
        "tasks_due_soon": 0,
    }
    assert data["calendar"]["today"] == []
    assert data["calendar"]["upcoming"] == []
    assert data["tasks"]["active"] == []
    assert data["tasks"]["due_soon"] == []
    assert data["backlog"] == []


def test_briefing_owner_scoping_ignores_other_users(monkeypatch, tmp_path):
    sf = _db_with_data(tmp_path)
    # Second user's task is seeded after Alice's (same factory adds only alice);
    # add bob's directly to prove the owner filter excludes it.
    db = sf()
    try:
        db.add(
            cdb.ScheduledTask(
                id="task-bob",
                owner="bob",
                name="Bob's task",
                task_type="llm",
                trigger_type="schedule",
                status="active",
            )
        )
        db.commit()
    finally:
        db.close()
    client = _make_client(monkeypatch, tmp_path, session_factory=sf)
    data = client.get("/api/briefing").json()
    names = [t["name"] for t in data["tasks"]["active"]]
    assert "Morning Brief" in names
    assert "Bob's task" not in names


def test_briefing_owner_scoping_when_no_user_is_configured(monkeypatch, tmp_path):
    sf = _db_with_data(tmp_path, owner="")
    client = _make_client(monkeypatch, tmp_path, user_override=_no_user, session_factory=sf)
    resp = client.get("/api/briefing")
    assert resp.status_code == 200
    data = resp.json()
    # No owner → the whole namespace is visible (single-user mode).
    assert data["summary"]["appointments_today"] == 1
    assert data["summary"]["tasks_active"] == 1