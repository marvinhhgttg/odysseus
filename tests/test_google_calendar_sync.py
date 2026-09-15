"""Tests for the Google Calendar outbound sync (Odysseus -> Google).

Covers the OAuth provider derivation, the event->resource payload mapper,
local-only staging decisions, delete tombstones and an end-to-end push using a
fake API client against a scratch SQLite database.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

import core.database as cdb
from src.services.google_oauth_service import provider_for_requested_scopes
from src.services import google_calendar_sync_service as sync_svc
from src.services.google_calendar_sync_service import (
    add_google_tombstone,
    build_event_payload,
    stage_only_local,
)


# ── OAuth provider derivation ────────────────────────────────────────────────

def test_provider_for_scopes_calendar():
    assert (
        provider_for_requested_scopes(
            ["https://www.googleapis.com/auth/calendar", "openid", "email"]
        )
        == "google_calendar"
    )
    # tasks still wins when both are requested (registered earlier).
    assert (
        provider_for_requested_scopes(
            ["https://www.googleapis.com/auth/tasks",
             "https://www.googleapis.com/auth/calendar"]
        )
        == "google_tasks"
    )
    assert provider_for_requested_scopes([]) == "google_drive"
    assert provider_for_requested_scopes(None) == "google_drive"


# ── Payload mapper ───────────────────────────────────────────────────────────

def _ev(**overrides):
    base = {
        "uid": "4ba7f4eb-3ba4-4bbd-a7d4-9b6b1a16166e",
        "summary": "Training",
        "description": "Mit Trainer",
        "location": "Feld 1",
        "dtstart": datetime(2026, 9, 14, 9, 0),
        "dtend": datetime(2026, 9, 14, 10, 0),
        "all_day": False,
        "is_utc": True,
        "rrule": "",
        "event_type": "health",
        "importance": "high",
        "status": "confirmed",
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_payload_basic_timed_utc():
    p = build_event_payload(_ev())
    assert p["id"] == "odisseu4ba7f4eb3ba44bbda7d49b6b1a16166e"
    assert p["summary"] == "Training"
    assert p["location"] == "Feld 1"
    assert p["start"] == {"dateTime": "2026-09-14T09:00:00Z", "timeZone": "UTC"}
    assert p["end"] == {"dateTime": "2026-09-14T10:00:00Z", "timeZone": "UTC"}
    assert p["extendedProperties"]["private"]["odysseus_uid"] == _ev().uid
    assert p["extendedProperties"]["private"]["odysseus_type"] == "health"
    assert p["extendedProperties"]["private"]["odysseus_importance"] == "high"


def test_payload_all_day_exclusive_end():
    p = build_event_payload(_ev(all_day=True, is_utc=False,
        dtend=datetime(2026, 9, 15, 0, 0)))
    assert p["start"] == {"date": "2026-09-14"}
    assert p["end"] == {"date": "2026-09-15"}  # dtend is the next-day boundary
    assert "dateTime" not in p["start"]


def test_payload_floating_local_gets_named_zone():
    p = build_event_payload(_ev(is_utc=False))
    assert p["start"] == {
        "dateTime": "2026-09-14T09:00:00",
        "timeZone": "Europe/Berlin",
    }
    assert p["end"] == {
        "dateTime": "2026-09-14T10:00:00",
        "timeZone": "Europe/Berlin",
    }


def test_payload_rrule_prefix_normalized():
    p = build_event_payload(_ev(rrule="RRULE:FREQ=WEEKLY;BYDAY=MO"))
    assert p["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=MO"]


def test_floating_tz_env_override(monkeypatch):
    import src.services.google_calendar_sync_service as mod
    monkeypatch.setattr(mod, "GOOGLE_CALENDAR_FLOATING_TZ", "America/New_York")
    p = mod.build_event_payload(_ev(is_utc=False))
    assert p["start"]["timeZone"] == "America/New_York"


def test_payload_cancelled_status_mapped():
    p = build_event_payload(_ev(status="cancelled"))
    assert p["status"] == "cancelled"


def test_payload_normalizes_reversed_time_range():
    p = build_event_payload(_ev(
        is_utc=False,
        dtend=datetime(2026, 9, 14, 8, 0),  # before start -> normalized +1h
    ))
    assert p["start"]["dateTime"] == "2026-09-14T09:00:00"
    assert p["end"]["dateTime"] == "2026-09-14T10:00:00"


# ── Local-only staging decisions ─────────────────────────────────────────────

def test_stage_only_local_accepts_local_events():
    ev = SimpleNamespace(
        calendar=SimpleNamespace(source="local"),
        remote_href=None,
        origin=None,
    )
    assert stage_only_local(ev) is True


def test_stage_only_local_rejects_caldav():
    ev = SimpleNamespace(
        calendar=SimpleNamespace(source="caldav"),
        remote_href="https://example.com/x.ics",
        origin="caldav",
    )
    assert stage_only_local(ev) is False


def test_stage_only_local_rejects_remote_href():
    ev = SimpleNamespace(
        calendar=SimpleNamespace(source="local"),
        remote_href="https://example.com/x.ics",
        origin=None,
    )
    assert stage_only_local(ev) is False


def test_stage_only_local_rejects_imported_origin():
    ev = SimpleNamespace(
        calendar=SimpleNamespace(source="local"),
        remote_href=None,
        origin="email_triage",
    )
    assert stage_only_local(ev) is False


# ── Delete tombstones ────────────────────────────────────────────────────────

def test_add_google_tombstone_creates_new_row():
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = None
    ev = SimpleNamespace(uid="u1", google_calendar_id="cal1", google_event_id="ev1")
    add_google_tombstone(db, ev, owner="marc")
    db.add.assert_called_once()
    row = db.add.call_args[0][0]
    assert row.uid == "u1"
    assert row.owner == "marc"
    assert row.google_calendar_id == "cal1"
    assert row.google_event_id == "ev1"


def test_add_google_tombstone_noop_without_event_id():
    db = MagicMock()
    add_google_tombstone(db, SimpleNamespace(uid="u2", google_event_id=None))
    db.add.assert_not_called()
    db.query.assert_not_called()


def test_add_google_tombstone_updates_existing():
    existing = MagicMock()
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = existing
    add_google_tombstone(
        db,
        SimpleNamespace(uid="u1", google_calendar_id="cal9", google_event_id="ev9"),
        owner="marc",
    )
    db.add.assert_not_called()
    assert existing.google_calendar_id == "cal9"
    assert existing.google_event_id == "ev9"
    assert existing.last_error is None


# ── End-to-end push vs a fake API client ─────────────────────────────────────

class _FakeCalendarClient:
    def __init__(self, calendars=None):
        self.inserted = []
        self.patched = []
        self.deleted = []
        self.calendar_list = calendars or [{"id": "odysseus", "summary": "Odysseus"}]

    def list_calendars(self, page_token=None):
        return {"items": self.calendar_list}

    def create_calendar(self, summary):
        raise AssertionError("calendar should already exist")

    def insert_event(self, calendar_id, payload):
        self.inserted.append((calendar_id, payload))
        return {"id": payload["id"]}

    def patch_event(self, calendar_id, event_id, payload):
        self.patched.append((calendar_id, event_id, payload))
        return {"id": event_id}

    def delete_event(self, calendar_id, event_id):
        self.deleted.append((calendar_id, event_id))
        return {}


def _make_db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'sync.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    cdb.Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, autoflush=False, autocommit=False)


def _fake_integration():
    return [
        {
            "name": "index",
            "provider": "google_calendar",
            "preset": "google_calendar",
            "enabled": True,
            "oauth_access_token": "tok",
            "oauth_expires_at": (datetime.now() + timedelta(hours=1)).isoformat(),
            "settings": {},
        }
    ]


async def _fresh(integration, force_refresh=False):
    return integration


async def test_push_event_creates_google_mirror(tmp_path):
    SessionFactory = _make_db(tmp_path)
    db = SessionFactory()
    db.add(cdb.CalendarCal(id="cal-1", name="Personal", owner="marc", source="local"))
    db.commit()
    db.add(cdb.CalendarEvent(
        uid="abc-123",
        calendar_id="cal-1",
        summary="Arzttermin",
        description="Blutbild",
        location="Praxis",
        dtstart=datetime(2026, 9, 20, 14, 0),
        dtend=datetime(2026, 9, 20, 15, 0),
        all_day=False,
        is_utc=True,
        status="confirmed",
    ))
    db.commit()
    db.close()

    client = _FakeCalendarClient()

    async def _freshc(integration, force_refresh=False):
        return client

    with patch.object(sync_svc, "load_integrations", return_value=_fake_integration()), \
         patch.object(sync_svc, "SessionLocal", SessionFactory), \
         patch.object(sync_svc, "_fresh_client", _freshc):
        result = await sync_svc.push_event_by_uid("abc-123", owner="marc")

    assert result == "created"
    assert len(client.inserted) == 1
    cal_id, payload = client.inserted[0]
    assert cal_id == "odysseus"
    assert payload["summary"] == "Arzttermin"
    assert payload["start"]["dateTime"] == "2026-09-20T14:00:00Z"

    db = SessionFactory()
    row = db.query(cdb.CalendarEvent).filter(cdb.CalendarEvent.uid == "abc-123").first()
    assert row.google_calendar_id == "odysseus"
    assert row.google_event_id == payload["id"]
    assert row.google_sync_state == "synced"
    db.close()


async def test_push_event_no_integration_is_noop(tmp_path):
    SessionFactory = _make_db(tmp_path)
    db = SessionFactory()
    db.add(cdb.CalendarCal(id="cal-1", name="Personal", owner="marc", source="local"))
    db.commit()
    db.add(cdb.CalendarEvent(
        uid="abc-456",
        calendar_id="cal-1",
        summary="Ohne Integration",
        dtstart=datetime(2026, 9, 20, 14, 0),
        dtend=datetime(2026, 9, 20, 15, 0),
    ))
    db.commit()
    db.close()

    with patch.object(sync_svc, "load_integrations", return_value=[]), \
         patch.object(sync_svc, "SessionLocal", SessionFactory):
        result = await sync_svc.push_event_by_uid("abc-456", owner="marc")

    assert result is None

    db = SessionFactory()
    row = db.query(cdb.CalendarEvent).filter(cdb.CalendarEvent.uid == "abc-456").first()
    assert row.google_sync_state is None
    assert row.google_event_id is None
    db.close()


async def test_push_deleted_removes_mirror(tmp_path):
    SessionFactory = _make_db(tmp_path)
    db = SessionFactory()
    db.add(cdb.CalendarCal(id="cal-1", name="Personal", owner="marc", source="local"))
    db.commit()
    db.add(cdb.CalendarEvent(
        uid="abc-789",
        calendar_id="cal-1",
        summary="Gelöscht",
        dtstart=datetime(2026, 9, 20, 14, 0),
        dtend=datetime(2026, 9, 20, 15, 0),
        google_calendar_id="odysseus",
        google_event_id="odysseus-abc-789",
    ))
    db.add(cdb.GoogleCalendarDeletedEvent(
        uid="abc-789",
        owner="marc",
        google_calendar_id="odysseus",
        google_event_id="odysseus-abc-789",
    ))
    db.commit()
    db.close()

    client = _FakeCalendarClient()

    async def _freshc(integration, force_refresh=False):
        return client

    with patch.object(sync_svc, "load_integrations", return_value=_fake_integration()), \
         patch.object(sync_svc, "SessionLocal", SessionFactory), \
         patch.object(sync_svc, "_fresh_client", _freshc):
        result = await sync_svc.push_deleted_by_uid("abc-789", owner="marc")

    assert result == "deleted"
    assert client.deleted == [("odysseus", "odysseus-abc-789")]

    db = SessionFactory()
    assert db.query(cdb.GoogleCalendarDeletedEvent).filter(
        cdb.GoogleCalendarDeletedEvent.uid == "abc-789"
    ).first() is None
    db.close()


async def test_sweep_stages_and_pushes_unstaged(tmp_path):
    SessionFactory = _make_db(tmp_path)
    db = SessionFactory()
    db.add(cdb.CalendarCal(id="cal-1", name="Personal", owner="marc", source="local"))
    db.commit()
    db.add(cdb.CalendarEvent(
        uid="sweep-001",
        calendar_id="cal-1",
        summary="Backfill",
        dtstart=datetime(2026, 9, 20, 14, 0),
        dtend=datetime(2026, 9, 20, 15, 0),
        is_utc=True,
    ))
    db.commit()
    db.close()

    client = _FakeCalendarClient()

    async def _freshc(integration, force_refresh=False):
        return client

    with patch.object(sync_svc, "load_integrations", return_value=_fake_integration()), \
         patch.object(sync_svc, "SessionLocal", SessionFactory), \
         patch.object(sync_svc, "_fresh_client", _freshc):
        summary = await sync_svc.sync_pending_sweep(owner="marc")

    assert summary["staged"] == 1
    assert summary["pushed"] == 1
    assert len(client.inserted) == 1

    db = SessionFactory()
    row = db.query(cdb.CalendarEvent).filter(cdb.CalendarEvent.uid == "sweep-001").first()
    assert row.google_sync_state == "synced"
    db.close()