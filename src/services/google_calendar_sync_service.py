from __future__ import annotations

import asyncio
import logging
import os
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from core.database import CalendarEvent, GoogleCalendarDeletedEvent, SessionLocal
from src.integrations import load_integrations
from src.services.google_calendar_client import GoogleCalendarClient
from src.services.google_oauth_service import ensure_fresh_google_token

logger = logging.getLogger(__name__)

GOOGLE_CALENDAR_PROVIDER = "google_calendar"
ODYSSEUS_CALENDAR_NAME = "Odysseus"

GOOGLE_SYNC_STATE_CREATE = "pending_create"
GOOGLE_SYNC_STATE_UPDATE = "pending_update"
GOOGLE_SYNC_STATE_DELETE = "pending_delete"
GOOGLE_SYNC_STATE_SYNCED = "synced"

# Calendar v3 requires an explicit time zone definition on every `dateTime`
# field (RFC 3339 allows a bare offset; floating wall-clock times need the
# named zone instead). Local events are stored as naive datetimes, so the
# sync pins them to this IANA zone — they keep their wall-clock time there.
GOOGLE_CALENDAR_FLOATING_TZ = os.getenv(
    "ODYSSEUS_CALENDAR_TIMEZONE", "Europe/Berlin"
)

_ODYSSEUS_CALENDAR_ID_CACHE: Optional[str] = None


def find_google_calendar_integration() -> Optional[Dict[str, Any]]:
    integrations = load_integrations()
    for integration in integrations:
        provider = (integration.get("provider") or "").strip()
        preset = (integration.get("preset") or "").strip()
        if provider == GOOGLE_CALENDAR_PROVIDER or preset == GOOGLE_CALENDAR_PROVIDER:
            if integration.get("enabled", True):
                return integration
    return None


def stage_event_for_google(ev: Any, state: str) -> None:
    """Mark a local event so the next sweep (or the immediate push) uploads it."""
    if ev is None:
        return
    if stage_only_local(ev):
        ev.google_sync_state = state
        if state == GOOGLE_SYNC_STATE_CREATE:
            ev.google_calendar_id = None


def stage_only_local(ev: Any) -> bool:
    """True when this event should be pushed to the Odysseus Google calendar.

    Local events only — CalDAV-sourced events (live `remote_href`, a caldav
    calendar, or a caldav origin) are managed by the CalDAV sync instead and
    are never mirrored to Google.
    """
    cal = ev.calendar if hasattr(ev, "calendar") else None
    if cal is not None and getattr(cal, "source", "local") == "caldav":
        return False
    if getattr(ev, "remote_href", None):
        return False
    origin = getattr(ev, "origin", None)
    if origin and origin != "local":
        return False
    return True


# Google Calendar event ids allow only lowercase base32hex: letters a-v and
# digits 0-9 (RFC 2938 §3.1.2), 5..1024 chars, must start with a letter.
_VALID_EVENT_ID_CHARS = frozenset(
    "abcdefghijklmnopqrstuv0123456789"
)


def _google_event_stable_id(ev: Any) -> str:
    """Stable client-supplied event id so (re)inserts are idempotent.

    The raw uid is a uuid4 hex string with hyphens; hyphens (and letters w-z,
    e.g. the "y" in "odysseus") are illegal in Google event ids, so the id is
    built from a valid lowercase prefix plus the sanitised hex uid.
    """
    uid = (getattr(ev, "uid", "") or "").lower()
    safe = "".join(ch for ch in uid if ch in _VALID_EVENT_ID_CHARS)
    return f"odisseu{safe or 'x'}"


def build_event_payload(ev: Any) -> Dict[str, Any]:
    """Map a CalendarEvent row to a Google Calendar v3 event resource."""
    start_dt: Optional[datetime] = getattr(ev, "dtstart", None)
    end_dt: Optional[datetime] = getattr(ev, "dtend", None)
    all_day = bool(getattr(ev, "all_day", False))
    is_utc = bool(getattr(ev, "is_utc", False))

    event_type = getattr(ev, "event_type", None)
    importance = getattr(ev, "importance", "normal") or "normal"

    payload: Dict[str, Any] = {
        "id": _google_event_stable_id(ev),
        "summary": getattr(ev, "summary", "") or "",
        "status": "cancelled" if getattr(ev, "status", None) == "cancelled" else "confirmed",
        "extendedProperties": {
            "private": {
                "odysseus_uid": getattr(ev, "uid", ""),
                "odysseus_type": event_type or "",
                "odysseus_importance": importance,
            }
        },
    }
    if getattr(ev, "location", None):
        payload["location"] = ev.location
    if getattr(ev, "description", None):
        payload["description"] = ev.description

    if all_day and start_dt is not None:
        if end_dt is None or end_dt <= start_dt:
            end_dt = start_dt + timedelta(days=1)
        payload["start"] = {"date": start_dt.date().isoformat()}
        payload["end"] = {"date": end_dt.date().isoformat()}
    else:
        # A mirrored event whose end precedes its start would be rejected by
        # Google ("specified time range is empty"); normalize to a sane
        # default duration like the all-day branch does.
        if end_dt is not None and start_dt is not None and end_dt <= start_dt:
            end_dt = start_dt + timedelta(hours=1)
        start_fmt, end_fmt = None, None
        if start_dt is not None:
            start_fmt = start_dt.isoformat() + ("Z" if is_utc else "")
        if end_dt is not None:
            end_fmt = end_dt.isoformat() + ("Z" if is_utc else "")
        if start_fmt is None:
            start_fmt = end_fmt
            end_dt = None
        payload["start"] = {"dateTime": start_fmt}
        if is_utc:
            payload["start"]["timeZone"] = "UTC"
        else:
            payload["start"]["timeZone"] = GOOGLE_CALENDAR_FLOATING_TZ
        if end_fmt is None and end_dt is not None:
            end_fmt = (end_dt + timedelta(hours=1)).isoformat() + ("Z" if is_utc else "")
        if end_fmt:
            payload["end"] = {"dateTime": end_fmt}
            if is_utc:
                payload["end"]["timeZone"] = "UTC"
            else:
                payload["end"]["timeZone"] = GOOGLE_CALENDAR_FLOATING_TZ

    rrule = (getattr(ev, "rrule", None) or "").strip()
    if rrule:
        rrule = rrule.removeprefix("RRULE:").rstrip(";")
        if rrule:
            payload["recurrence"] = [f"RRULE:{rrule}"]

    return payload


def add_google_tombstone(
    db: Any,
    ev: Any,
    owner: Optional[str] = None,
    google_calendar_id: Optional[str] = None,
    google_event_id: Optional[str] = None,
) -> None:
    """Save a delete tombstone before the local row vanishes."""
    cal_id = google_calendar_id or getattr(ev, "google_calendar_id", None)
    event_id = google_event_id or getattr(ev, "google_event_id", None)
    if not event_id:
        return
    stone = GoogleCalendarDeletedEvent(
        uid=getattr(ev, "uid", ""),
        owner=owner,
        google_calendar_id=cal_id,
        google_event_id=event_id,
    )
    existing = (
        db.query(GoogleCalendarDeletedEvent)
        .filter(GoogleCalendarDeletedEvent.uid == stone.uid)
        .first()
    )
    if existing:
        existing.google_calendar_id = cal_id
        existing.google_event_id = event_id
        existing.last_error = None
    else:
        db.add(stone)


async def _fresh_client(integration: Dict[str, Any]) -> GoogleCalendarClient:
    refreshed = await ensure_fresh_google_token(integration, force_refresh=False)
    return GoogleCalendarClient.from_integration(refreshed or integration)


async def ensure_odysseus_calendar(client: GoogleCalendarClient) -> str:
    """Return the id of the dedicated Odysseus calendar, creating it on demand."""
    global _ODYSSEUS_CALENDAR_ID_CACHE
    if _ODYSSEUS_CALENDAR_ID_CACHE:
        return _ODYSSEUS_CALENDAR_ID_CACHE
    page_token: Optional[str] = None
    for _ in range(5):
        data = client.list_calendars(page_token=page_token)
        for cal in data.get("items") or []:
            if (cal.get("summary") or "").strip() == ODYSSEUS_CALENDAR_NAME:
                _ODYSSEUS_CALENDAR_ID_CACHE = cal.get("id")
                return _ODYSSEUS_CALENDAR_ID_CACHE
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    created = client.create_calendar(ODYSSEUS_CALENDAR_NAME)
    _ODYSSEUS_CALENDAR_ID_CACHE = created.get("id")
    return _ODYSSEUS_CALENDAR_ID_CACHE


def clear_odysseus_calendar_cache() -> None:
    global _ODYSSEUS_CALENDAR_ID_CACHE
    _ODYSSEUS_CALENDAR_ID_CACHE = None


async def push_event_by_uid(uid: str, owner: str = "") -> Optional[str]:
    """Upload or update one local event into the Odysseus calendar.

    Returns a short human status ("created"/"updated"/"skipped") or None when
    no google_calendar integration is connected. Never raises — failures are
    logged and the event stays pending for the sweep to retry.
    """
    integration = find_google_calendar_integration()
    if not integration:
        return None
    db = SessionLocal()
    try:
        ev = db.query(CalendarEvent).filter(CalendarEvent.uid == uid).first()
        if ev is None:
            return "gone"
        state = ev.google_sync_state
        if state == GOOGLE_SYNC_STATE_DELETE:
            db.rollback()
            await push_deleted_by_uid(uid, owner=owner)
            return "deleted"
        if not stage_only_local(ev):
            return "skipped"
        client = await _fresh_client(integration)
        calendar_id = await ensure_odysseus_calendar(client)
        payload = build_event_payload(ev)
        action = "created"
        if ev.google_event_id and state != GOOGLE_SYNC_STATE_CREATE:
            try:
                client.patch_event(
                    calendar_id,
                    ev.google_event_id,
                    payload,
                )
                action = "updated"
            except HTTPException as e:
                if e.status_code != 404:
                    raise
                action = "recreated"
                _insert_with_conflict_fallback(client, calendar_id, payload)
                ev = db.query(CalendarEvent).filter(CalendarEvent.uid == uid).first()
        else:
            _insert_with_conflict_fallback(client, calendar_id, payload)
        ev.google_calendar_id = calendar_id
        ev.google_event_id = _google_event_stable_id(ev)
        ev.google_sync_state = GOOGLE_SYNC_STATE_SYNCED
        ev.google_synced_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
        return action
    except Exception as e:
        db.rollback()
        logger.warning(f"google_calendar push event {uid}: {e}")
        return None
    finally:
        db.close()


def _insert_with_conflict_fallback(
    client: GoogleCalendarClient,
    calendar_id: str,
    payload: Dict[str, Any],
) -> str:
    """Insert, falling back to PATCH when the stable id already exists."""
    try:
        created = client.insert_event(calendar_id, payload)
        return created.get("id") or payload.get("id") or ""
    except HTTPException as e:
        if e.status_code == 409:
            client.patch_event(calendar_id, payload.get("id") or "", payload)
            return payload.get("id") or ""
        raise


async def push_deleted_by_uid(uid: str, owner: str = "", retry: bool = True) -> str:
    """Delete the Google mirror for a locally deleted event via its tombstone."""
    integration = find_google_calendar_integration()
    if not integration:
        return "no-integration"
    db = SessionLocal()
    try:
        stone = (
            db.query(GoogleCalendarDeletedEvent)
            .filter(GoogleCalendarDeletedEvent.uid == uid)
            .first()
        )
        if stone is None or not stone.google_event_id:
            return "no-tombstone"
        client = await _fresh_client(integration)
        try:
            client.delete_event(stone.google_calendar_id, stone.google_event_id)
        except HTTPException as e:
            if retry and e.status_code == 404:
                pass
            elif retry and e.status_code >= 400:
                raise
        db.delete(stone)
        db.commit()
        return "deleted"
    except Exception as e:
        db.rollback()
        logger.warning(f"google_calendar push delete {uid}: {e}")
        return "error"
    finally:
        db.close()


async def sync_pending_sweep(owner: str = "") -> Dict[str, Any]:
    """Idempotent sweep: stage unstaged local events, then upload all pending.

    Called on a timer so backfill (existing events created before the
    integration existed) and crash-retries converge by themselves.
    """
    if not find_google_calendar_integration():
        return {"staged": 0, "pushed": 0, "skipped": 0}
    db = SessionLocal()
    staged = 0
    try:
        queued: List[str] = []
        for ev in (
            db.query(CalendarEvent)
            .filter(
                CalendarEvent.google_sync_state.is_(None),
                CalendarEvent.google_calendar_id.is_(None),
            )
            .all()
        ):
            if stage_only_local(ev):
                ev.google_sync_state = GOOGLE_SYNC_STATE_CREATE
                staged += 1
        db.commit()
        for ev in (
            db.query(CalendarEvent)
            .filter(
                CalendarEvent.google_sync_state.is_not(None),
                CalendarEvent.google_sync_state != GOOGLE_SYNC_STATE_SYNCED,
            )
            .all()
        ):
            queued.append(ev.uid)
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning(f"google_calendar sweep staging: {e}")
        return {"staged": staged, "pushed": 0, "skipped": 0}
    finally:
        db.close()

    pushed = 0
    for uid in queued:
        result = await push_event_by_uid(uid, owner=owner)
        if result not in (None, "gone", "skipped"):
            pushed += 1
    return {"staged": staged, "pushed": pushed, "skipped": len(queued) - pushed}


def schedule_google_push(uid: str, owner: str = "") -> asyncio.Task:
    """Fire-and-forget immediate push of one event (best-effort)."""

    async def _run() -> None:
        try:
            await push_event_by_uid(uid, owner=owner)
        except Exception as e:
            logger.warning(f"google_calendar scheduled push {uid}: {e}")

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    return loop.create_task(_run())


def schedule_google_delete(uid: str, owner: str = "") -> asyncio.Task:
    """Fire-and-forget best-effort removal of the Google mirror row."""

    async def _run() -> None:
        try:
            await push_deleted_by_uid(uid, owner=owner)
        except Exception as e:
            logger.warning(f"google_calendar scheduled delete {uid}: {e}")

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return None
    return loop.create_task(_run())