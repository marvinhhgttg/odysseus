# routes/briefing_routes.py
"""PM Briefing — read-only aggregation endpoint for the Briefing panel.

Combines four existing data surfaces into one snapshot so the UI can render
a single "what's going on today" tile:

  - email urgency state (same file the unread dot reads)
  - calendar events for today + the next 7 days (local + CalDAV)
  - open backlog items (data/backlog.json)
  - scheduled task scheduler status (active tasks + recent runs)

Everything is best-effort: a missing source yields empty sections, never an
error. Nothing here writes to the database.
"""

import json as _json
import logging
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Request

logger = logging.getLogger(__name__)


def _with_deps():
    from core.database import CalendarCal, CalendarEvent, ScheduledTask, SessionLocal, TaskRun
    from src.auth_dependencies import require_user
    from src.auth_helpers import _auth_disabled, owner_filter
    from src.constants import DATA_DIR
    return {
        "CalendarCal": CalendarCal,
        "CalendarEvent": CalendarEvent,
        "ScheduledTask": ScheduledTask,
        "SessionLocal": SessionLocal,
        "TaskRun": TaskRun,
        "require_user": require_user,
        "_auth_disabled": _auth_disabled,
        "owner_filter": owner_filter,
        "DATA_DIR": DATA_DIR,
    }


def _load_urgency(deps, owner: str) -> dict:
    slug = "".join(c if (c.isalnum() or c in "-_.@") else "_" for c in (owner or "default"))
    path = Path(deps["DATA_DIR"]) / f"email_urgency_state_{slug}.json"
    if not path.exists():
        return {"total_unread": 0, "total_urgent": 0, "max_score": 0, "per_uid": {}}
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"total_unread": 0, "total_urgent": 0, "max_score": 0, "per_uid": {}}
    # `notified_uids` is an internal scheduler debounce, not UI-relevant.
    data.pop("notified_uids", None)
    return data


def _load_backlog(deps) -> list:
    path = Path(deps["DATA_DIR"]) / "backlog.json"
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    tickets = data.get("tickets") if isinstance(data, dict) else None
    if not isinstance(tickets, list):
        return []
    return [
        {
            "id": str(t.get("id", "")),
            "priority": int(t.get("priority", 99)) if isinstance(t.get("priority"), int) else 99,
            "title": str(t.get("title", "")),
            "status": str(t.get("status", "open")),
            "estimated_effort": str(t.get("estimated_effort", "")),
            "created_at": t.get("created_at"),
        }
        for t in tickets
    ]


def _events_in_window(deps, owner: str, db, start_dt, end_dt):
    """Mirror the calendar list_events query (expanded recurrences).

    Returns already-serialized event dicts (frontend format), sorted by start.
    """
    from sqlalchemy import and_, or_
    from routes.calendar_routes import _expand_rrule

    CalendarEvent = deps["CalendarEvent"]
    CalendarCal = deps["CalendarCal"]
    # Auth-disabled single-user semantics: match daily_brief's handling.
    allow_null_owner = _auth_single_user(deps)
    q = db.query(CalendarEvent).join(CalendarCal).filter(
        CalendarEvent.status != "cancelled",
        or_(
            and_(
                or_(CalendarEvent.rrule == "", CalendarEvent.rrule.is_(None)),
                CalendarEvent.dtstart < end_dt,
                CalendarEvent.dtend > start_dt,
            ),
            and_(
                CalendarEvent.rrule.isnot(None),
                CalendarEvent.rrule != "",
                CalendarEvent.dtstart < end_dt,
            ),
        ),
    )
    if owner:
        q = deps["owner_filter"](q, CalendarCal, owner, include_shared=allow_null_owner)
    events = q.order_by(CalendarEvent.dtstart).all()
    expanded = []
    for e in events:
        expanded.extend(_expand_rrule(e, start_dt, end_dt))
    expanded.sort(key=lambda d: d["dtstart"])
    return expanded


def _auth_single_user(deps) -> bool:
    """True when running unconfigured/single-user (owner=null rows allowed)."""
    try:
        from core.auth import AuthManager
        return not AuthManager().is_configured
    except Exception:
        return False


def _task_to_brief(t) -> dict:
    return {
        "id": getattr(t, "id", ""),
        "name": getattr(t, "name", "") or "Untitled Task",
        "task_type": getattr(t, "task_type", None) or "llm",
        "action": getattr(t, "action", None),
        "schedule": getattr(t, "schedule", None),
        "scheduled_time": getattr(t, "scheduled_time", None),
        "cron_expression": getattr(t, "cron_expression", None),
        "next_run": t.next_run.isoformat() + "Z" if getattr(t, "next_run", None) else None,
        "last_run": t.last_run.isoformat() + "Z" if getattr(t, "last_run", None) else None,
        "status": getattr(t, "status", None),
        "output_target": getattr(t, "output_target", None),
        "run_count": getattr(t, "run_count", None) or 0,
    }


def setup_briefing_routes() -> APIRouter:
    router = APIRouter(prefix="/api/briefing", tags=["briefing"])

    @router.get("")
    async def get_briefing(request: Request):
        deps = _with_deps()
        require_user = deps["require_user"]
        owner = require_user(request)

        now = datetime.now()
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow = today + timedelta(days=1)
        in_a_week = today + timedelta(days=8)
        window_after_24h = now + timedelta(hours=24)

        # Email urgency — same source as the inbox unread dot.
        urgency = _load_urgency(deps, owner)
        per_uid = urgency.get("per_uid") or {}
        flagged = [
            {
                "score": int(v.get("score", 0)),
                "tags": v.get("tags") or [],
                "unread": bool(v.get("unread", False)),
                "subject": str(v.get("subject", "")),
                "from": str(v.get("from", "")),
                "reason": str(v.get("reason", "")),
                "ts": v.get("ts"),
            }
            for v in per_uid.values()
            if isinstance(v, dict) and (v.get("score", 0) or v.get("unread"))
        ]
        flagged.sort(key=lambda v: (-v["score"], -(v.get("ts") or 0)))

        db = deps["SessionLocal"]()
        try:
            today_events = _events_in_window(deps, owner, db, today, tomorrow)
            upcoming_events = _events_in_window(deps, owner, db, tomorrow, in_a_week)

            # Scheduled tasks for this owner.
            ScheduledTask = deps["ScheduledTask"]
            tq = db.query(ScheduledTask)
            if owner:
                tq = tq.filter(ScheduledTask.owner == owner)
            active = [
                t for t in tq.all()
                if (getattr(t, "status", None) or "active") == "active"
            ]
            active.sort(key=lambda t: (t.next_run or datetime.max))
            due_soon = [
                t for t in active
                if getattr(t, "next_run", None) and t.next_run <= window_after_24h
            ]

            TaskRun = deps["TaskRun"]
            runs = (
                db.query(TaskRun, ScheduledTask.name)
                .outerjoin(ScheduledTask, TaskRun.task_id == ScheduledTask.id)
                .order_by(TaskRun.started_at.desc())
                .limit(6)
                .all()
            )
            recent_runs = [
                {
                    "task_id": r.id,
                    "task_name": (name or r.task_id)[:60],
                    "status": r.status,
                    "started_at": r.started_at.isoformat() + "Z" if r.started_at else None,
                    "error": (r.error or "")[:200] if r.error else None,
                }
                for r, name in runs
            ]
        finally:
            db.close()

        backlog = _load_backlog(deps)
        open_backlog = [b for b in backlog if b["status"] == "open"]
        open_backlog.sort(key=lambda b: b["priority"])
        high_priority = [b for b in open_backlog if b["priority"] <= 3]

        return {
            "generated_at": datetime.utcnow().isoformat() + "Z",
            "owner": owner,
            "summary": {
                "email_unread": int(urgency.get("total_unread", 0)),
                "email_urgent": int(urgency.get("total_urgent", 0)),
                "email_max_score": int(urgency.get("max_score", 0)),
                "appointments_today": len(today_events),
                "appointments_upcoming": len(upcoming_events),
                "backlog_open": len(open_backlog),
                "backlog_high": len(high_priority),
                "tasks_active": len(active),
                "tasks_due_soon": len(due_soon),
            },
            "email": {
                "total_unread": int(urgency.get("total_unread", 0)),
                "total_urgent": int(urgency.get("total_urgent", 0)),
                "max_score": int(urgency.get("max_score", 0)),
                "flagged": flagged[:8],
                "ts": urgency.get("ts"),
            },
            "calendar": {
                "today": today_events,
                "upcoming": upcoming_events[:10],
            },
            "backlog": open_backlog[:12],
            "tasks": {
                "active": [_task_to_brief(t) for t in active[:12]],
                "due_soon": due_soon,
                "recent_runs": recent_runs,
            },
        }

    return router