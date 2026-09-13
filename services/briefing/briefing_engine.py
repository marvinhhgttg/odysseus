"""Morning-briefing engine.

Produces a rich, German-language morning briefing that approximates the
runners' reference format:

    1 Mails          (recent 24 h / rest of week / newsletter, per-mail LLM
                      summaries with importance + urgency)
    2 Kalender heute (source-tagged events)
    3 Ausblick 7 Tage (+ conflict detection with fix suggestions)
    4 Offene Tasks   (Google Tasks, bucketed by due date)
    5 Top-5-Aktionen (LLM-prioritised)
    6 Vorgeschlagene Aktionen (display-only: [K] calendar TEMPLATE deep
                      links, [D] drive folder suggestions, [E] reply drafts)

Everything is best-effort: a failing source degrades to a hint line instead
of killing the whole briefing.
"""

from __future__ import annotations

import asyncio
import email as email_mod
import json
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

from services.briefing import prompts

GERMAN_WEEKDAYS = [
    "Montag", "Dienstag", "Mittwoch", "Donnerstag",
    "Freitag", "Samstag", "Sonntag",
]
GERMAN_MONTHS = [
    "Januar", "Februar", "März", "April", "Mai", "Juni",
    "Juli", "August", "September", "Oktober", "November", "Dezember",
]

_PERSONAL_CAL_TOKENS = ("marc", "natalia", "geteilt", "shared", "gemeinsam", "fami", "sonne", "privat")
_INFO_CAL_TOKENS = ("wochennummer", "ferien", "fussball", "fußball", "plan", "coach", "kira")


def _fmt_date(dt: datetime) -> str:
    """'Samstag, 12. September 2026'"""
    return f"{GERMAN_WEEKDAYS[dt.weekday()]}, {dt.day}. {GERMAN_MONTHS[dt.month - 1]} {dt.year}"


def _fmt_day(dt: datetime) -> str:
    """'12.09.2026'"""
    return dt.strftime("%d.%m.%Y")


def _fmt_daymonth(dt: datetime) -> str:
    """'12.09'"""
    return dt.strftime("%d.%m.")


def _fmt_time(dt: datetime) -> str:
    """'08:12 Uhr'"""
    return dt.strftime("%H:%M Uhr")


def _fmt_weekday_short(dt: datetime) -> str:
    return GERMAN_WEEKDAYS[dt.weekday()]


def _extract_json_array(text: str) -> Optional[list]:
    cleaned = re.sub(r"```(?:json)?", "", text or "").strip()
    start = cleaned.find("[")
    end = cleaned.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start : end + 1])
    except Exception:
        return None
    return data if isinstance(data, list) else None


def _batched(seq: list, size: int):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


async def _run_llm(candidates, messages) -> str:
    from src.llm_core import llm_call_async_with_fallback

    return await llm_call_async_with_fallback(
        candidates,
        messages,
        timeout=120,
        temperature=0.2,
        max_tokens=2800,
    )


def _resolve_candidates(owner: str):
    from src.task_endpoint import resolve_task_candidates

    return resolve_task_candidates(owner=owner)


def _calendar_is_personal(cal_name: str) -> bool:
    lower = (cal_name or "").lower()
    for token in _PERSONAL_CAL_TOKENS:
        if token in lower:
            return True
    return False


def _calendar_is_informational(cal_name: str) -> bool:
    lower = (cal_name or "").lower()
    return any(token in lower for token in _INFO_CAL_TOKENS)


def _parse_ev_dt(value: str, all_day: bool) -> datetime:
    """Parse an event datetime emitted by `_event_to_dict` into a local-naive
    datetime ready for comparison against other local event times."""
    if all_day:
        return datetime.strptime(value[:10], "%Y-%m-%d")
    if value.endswith("Z"):
        aware = datetime.fromisoformat(value[:-1] + "+00:00")
        return aware.astimezone().replace(tzinfo=None)
    return datetime.fromisoformat(value)


# ─────────────────────────── sources ───────────────────────────

def _enabled_email_accounts(owner: str = ""):
    from core.database import SessionLocal, EmailAccount
    from sqlalchemy import and_, or_

    db = SessionLocal()
    try:
        q = db.query(EmailAccount).filter(EmailAccount.enabled == True)  # noqa: E712
        if owner:
            unowned = or_(EmailAccount.owner == None, EmailAccount.owner == "")  # noqa: E711
            same_mailbox = or_(EmailAccount.imap_user == owner, EmailAccount.from_address == owner)
            q = q.filter(or_(EmailAccount.owner == owner, and_(unowned, same_mailbox)))
        return q.order_by(EmailAccount.is_default.desc(), EmailAccount.created_at.asc()).all()
    finally:
        db.close()


def _gather_emails_sync(owner: str = "", since_days: int = 7, max_per_account: int = 30) -> List[dict]:
    from routes.email_helpers import _imap_connect, _decode_header

    try:
        accounts = _enabled_email_accounts(owner)
    except Exception:
        accounts = []
    emails: List[dict] = []
    cutoff = datetime.utcnow() - timedelta(days=since_days)
    for account in accounts:
        try:
            conn = _imap_connect(account.id, owner=owner)
        except Exception:
            continue
        try:
            conn.select("INBOX", readonly=True)
            since_str = cutoff.strftime("%d-%b-%Y")
            status, data = conn.uid("SEARCH", None, f"(SINCE {since_str})")
            if status != "OK" or not data or not data[0]:
                continue
            uids = data[0].split()[-max_per_account:]
            for uid_b in uids:
                uid = uid_b.decode() if isinstance(uid_b, bytes) else str(uid_b)
                try:
                    st, msg_data = conn.uid(
                        "FETCH", uid_b,
                        "(UID FLAGS RFC822.HEADER BODY.PEEK[TEXT]<0.1200>)",
                    )
                    if st != "OK" or not msg_data:
                        continue
                    flags_blob = b" ".join(
                        part[0] for part in msg_data
                        if isinstance(part, tuple) and part and isinstance(part[0], (bytes, bytearray))
                    )
                    unread = b"\\Seen" not in flags_blob
                    raw = b""
                    for part in msg_data:
                        if isinstance(part, tuple) and part[1]:
                            raw += part[1] + b"\n\n"
                    if not raw:
                        continue
                    msg = email_mod.message_from_bytes(raw)
                    if (msg.get("X-Odysseus-Origin") or "").strip().lower() == "odysseus-ui":
                        continue
                    subject = _decode_header(msg.get("Subject") or "") or "(ohne Betreff)"
                    from_raw = _decode_header(msg.get("From") or "") or ""
                    if "<" in from_raw:
                        from_short = from_raw.split("<", 1)[0].strip().strip('"') or from_raw
                    else:
                        from_short = from_raw

                    date_hdr = msg.get("Date") or ""
                    try:
                        parsed_dt = email_mod.utils.parsedate_to_datetime(date_hdr)
                        if parsed_dt is None:
                            raise ValueError("no date")
                    except Exception:
                        parsed_dt = datetime.utcnow()
                        aware_ts = parsed_dt.replace(tzinfo=None)
                    else:
                        aware_ts = parsed_dt.astimezone().replace(tzinfo=None)

                    body_snippet = ""
                    try:
                        if msg.is_multipart():
                            for part in msg.walk():
                                if part.get_content_type() == "text/plain":
                                    body_snippet = (part.get_payload(decode=True) or b"").decode("utf-8", errors="ignore")[:4000]
                                    break
                        else:
                            body_snippet = (msg.get_payload(decode=True) or b"").decode("utf-8", errors="ignore")[:4000]
                    except Exception:
                        body_snippet = ""

                    has_attachments = False
                    has_list_unsubscribe = bool(msg.get("List-Unsubscribe"))

                    emails.append({
                        "key": f"{account.id}:{uid}",
                        "uid": uid,
                        "account": account.name or account.id,
                        "subject": subject,
                        "from": from_short,
                        "ts": aware_ts,
                        "date_label": _fmt_day(aware_ts),
                        "time_label": _fmt_time(aware_ts),
                        "body": body_snippet,
                        "unread": unread,
                        "has_attachments": has_attachments,
                        "newsletter_signal": has_list_unsubscribe,
                    })
                except Exception:
                    continue
        except Exception:
            continue
        finally:
            try:
                conn.logout()
            except Exception:
                pass
    emails.sort(key=lambda e: e["ts"], reverse=True)
    return emails


def _gather_calendar(owner: str = "", days: int = 8) -> List[dict]:
    """Overlapping window (yesterday midnight … days ahead) expanded per
    occurrence. Each occurrence carries local-naive start/end plus the
    source-calendar tag."""
    from routes.calendar_routes import _expand_rrule
    from core.database import SessionLocal, CalendarCal, CalendarEvent
    from sqlalchemy import and_, or_

    now = datetime.now().replace(microsecond=0)
    start = now - timedelta(hours=24)
    end = now + timedelta(days=days)

    db = SessionLocal()
    try:
        cal_scope = CalendarCal.owner == owner if owner else (
            or_(
                CalendarCal.owner == None,  # noqa: E711
                CalendarCal.owner == "",
                CalendarCal.owner.isnot(None),
            )
        )
        q = db.query(CalendarEvent).join(CalendarCal).filter(
            CalendarEvent.status != "cancelled",
            cal_scope,
            or_(
                and_(
                    or_(CalendarEvent.rrule == "", CalendarEvent.rrule.is_(None)),
                    CalendarEvent.dtstart < end,
                    CalendarEvent.dtend > start,
                ),
                and_(
                    CalendarEvent.rrule.isnot(None),
                    CalendarEvent.rrule != "",
                    CalendarEvent.dtstart < end,
                ),
            ),
        )
        rows = q.order_by(CalendarEvent.dtstart).all()

        events: List[dict] = []
        for row in rows:
            occurrences = _expand_rrule(row, start, end)
            for occ in occurrences:
                events.append({
                    "uid": occ.get("uid", ""),
                    "summary": occ.get("summary") or "",
                    "all_day": bool(occ.get("all_day")),
                    "calendar": occ.get("calendar") or "",
                    "description": occ.get("description") or "",
                    "location": occ.get("location") or "",
                    "start": _parse_ev_dt(occ.get("dtstart") or "", bool(occ.get("all_day"))),
                    "end": _parse_ev_dt(occ.get("dtend") or "", bool(occ.get("all_day"))),
                })
        return events
    except Exception:
        return []
    finally:
        db.close()


async def _gather_tasks(owner: str = "") -> Dict[str, Any]:
    """Google Tasks first; falls back to checklist/pinned Notes so the task
    section never silently vanishes."""
    try:
        from src.services.google_tasks_service import fetch_google_tasks

        data = await fetch_google_tasks(owner)
        tasks = []
        for t in data.get("tasks", []):
            due = None
            raw_due = (t.get("due") or "").strip()
            if raw_due:
                due = datetime.strptime(raw_due[:10], "%Y-%m-%d")
            tasks.append({
                "title": t.get("title") or "(ohne Titel)",
                "due": due,
                "list_title": t.get("list_title") or "",
                "is_subtask": bool(t.get("is_subtask")),
            })
        return {
            "available": True,
            "source": "Google Tasks",
            "connected_email": data.get("connected_email", ""),
            "tasks": tasks,
        }
    except Exception:
        return await _gather_notes_tasks(owner)


async def _gather_notes_tasks(owner: str = "") -> Dict[str, Any]:
    from core.database import SessionLocal, Note

    tasks: List[dict] = []
    db = SessionLocal()
    try:
        q = db.query(Note).filter(Note.archived == False)  # noqa: E712
        if owner:
            q = q.filter(Note.owner == owner)
        for n in q.all():
            due = None
            if getattr(n, "due_date", None):
                try:
                    due = n.due_date
                    if isinstance(due, str):
                        due = datetime.strptime(str(due)[:10], "%Y-%m-%d")
                except Exception:
                    due = None
            if n.note_type == "checklist" and n.items:
                try:
                    items = json.loads(n.items)
                    pending = [it.get("text", "") for it in items if not it.get("done")]
                except Exception:
                    pending = []
                for text in pending[:20]:
                    tasks.append({
                        "title": text,
                        "due": due,
                        "list_title": n.title or "Checkliste",
                        "is_subtask": True,
                    })
            elif n.pinned and n.title:
                tasks.append({
                    "title": n.title,
                    "due": due,
                    "list_title": n.title,
                    "is_subtask": False,
                })
    except Exception:
        tasks = []
    finally:
        db.close()
    return {
        "available": True,
        "source": "Notizen",
        "connected_email": "",
        "tasks": tasks,
    }


# ─────────────────────────── LLM steps ───────────────────────────

async def _analyze_emails(candidates, emails: List[dict]) -> Dict[str, dict]:
    """Per-email LLM summaries + importance/urgency, in small batches."""
    results: Dict[str, dict] = {}
    for batch in _batched(emails, 5):
        blocks = []
        for e in batch:
            blocks.append(
                f"===== {e['key']} =====\n"
                f"Von: {e.get('from')}\n"
                f"Betreff: {e.get('subject')}\n"
                f"Datum: {e.get('date_label')} {e.get('time_label')}\n"
                f"Anhänge: {'ja' if e.get('has_attachments') else 'nein'}\n"
                f"Unsub-Signal: {'ja' if e.get('newsletter_signal') else 'nein'}\n"
                f"Ungelesen: {'ja' if e.get('unread') else 'nein'}\n"
                f"Inhalt:\n{e.get('body') or '(leer)'}\n"
            )
        try:
            text = await _run_llm(
                candidates,
                [
                    {"role": "system", "content": prompts.EMAIL_ANALYSIS_SYSTEM},
                    {"role": "user", "content": prompts.EMAIL_ANALYSIS_USER.format(emails="\n".join(blocks))},
                ],
            )
            parsed = _extract_json_array(text)
        except Exception:
            parsed = None
        if not parsed:
            continue
        for item in parsed:
            if not isinstance(item, dict):
                continue
            key = str(item.get("key") or "")
            if key:
                results[key] = item
    return results


async def _resolve_conflicts(candidates, conflicts: List[dict]) -> str:
    if not conflicts:
        return ""
    lines = []
    for i, c in enumerate(conflicts, 1):
        lines.append(
            f"{i}. {c['day']}: „{c['a_summary']}\" ({c['a_time']}, [{c['a_cal']}]) "
            f"überschneidet sich mit „{c['b_summary']}\" ({c['b_time']}, [{c['b_cal']}])."
        )
    try:
        text = await _run_llm(
            candidates,
            [
                {"role": "system", "content": prompts.CALENDAR_CONFLICT_SYSTEM},
                {"role": "user", "content": prompts.CALENDAR_CONFLICT_USER.format(conflicts="\n".join(lines))},
            ],
        )
        return text.strip()
    except Exception:
        return "\n".join(
            f"- {_fmt_day(c['day_dt'])}: „{c['a_summary']}“ vs. „{c['b_summary']}“"
            for c in conflicts
        )


async def _top_actions(candidates, context: str) -> List[str]:
    if not context.strip():
        return []
    try:
        text = await _run_llm(
            candidates,
            [
                {"role": "system", "content": prompts.TOP_ACTIONS_SYSTEM},
                {"role": "user", "content": prompts.TOP_ACTIONS_USER.format(context=context)},
            ],
        )
    except Exception:
        return []
    lines = [re.sub(r"^\s*\d+\.\s*", "", ln).strip() for ln in text.splitlines() if re.match(r"^\s*\d+\.", ln)]
    return [ln for ln in lines if ln][:5]


# ─────────────────────────── assembly ───────────────────────────

def _calendar_deep_link(title: str, start: str, end: str, location: str = "", details: str = "") -> str:
    from urllib.parse import quote

    start_digits = re.sub(r"\D", "", start or "")[:12]
    end_digits = re.sub(r"\D", "", end or "")[:12]
    if not start_digits:
        return ""
    if not end_digits:
        end_digits = start_digits
    if len(end_digits) < len(start_digits):
        end_digits = end_digits.ljust(len(start_digits), "0")
    params = {
        "action": "TEMPLATE",
        "text": title or "Termin",
        "dates": f"{start_digits}/{end_digits}",
    }
    if location:
        params["location"] = location
    if details:
        params["details"] = details
    query = "&".join(f"{k}={quote(str(v))}" for k, v in params.items())
    return f"https://calendar.google.com/calendar/render?{query}"


def _find_conflicts(events: List[dict]) -> List[dict]:
    conflicts: List[dict] = []
    today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    for day_offset in range(0, 2):
        day_start = today + timedelta(days=day_offset)
        day_end = day_start + timedelta(days=1)
        day_events = [e for e in events if e["start"] < day_end and e["end"] > day_start]
        for i in range(len(day_events)):
            for j in range(i + 1, len(day_events)):
                a, b = day_events[i], day_events[j]
                if not (a["start"] < b["end"] and b["start"] < a["end"]):
                    continue
                a_personal = _calendar_is_personal(a["calendar"])
                b_personal = _calendar_is_personal(b["calendar"])
                if not (a_personal or b_personal):
                    continue
                # Info feeds (Fußball, Ferien, Wochennummer, Coach/Kira …)
                # never produce conflicts — they are informational timelines,
                # not personal appointments.
                if _calendar_is_informational(a["calendar"]) or _calendar_is_informational(b["calendar"]):
                    continue
                conflicts.append({
                    "day_dt": day_start,
                    "day": f"{_fmt_weekday_short(day_start)}, {_fmt_day(day_start)}",
                    "a_summary": a["summary"],
                    "a_cal": a["calendar"],
                    "a_time": _fmt_time(a["start"]) if not a["all_day"] else "ganztägig",
                    "b_summary": b["summary"],
                    "b_cal": b["calendar"],
                    "b_time": _fmt_time(b["start"]) if not b["all_day"] else "ganztägig",
                    "a": a,
                    "b": b,
                })
    return conflicts[:12]


def _group_tasks(tasks: List[dict], now: datetime) -> Dict[str, List[dict]]:
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    buckets: Dict[str, List[dict]] = {
        "ueberfaellig": [],
        "heute": [],
        "woche": [],
        "spaeter": [],
        "ohne": [],
    }
    for t in tasks:
        due = t.get("due")
        if due is None:
            buckets["ohne"].append(t)
        elif due < today:
            buckets["ueberfaellig"].append(t)
        elif due < today + timedelta(days=1):
            buckets["heute"].append(t)
        elif due <= today + timedelta(days=7):
            buckets["woche"].append(t)
        else:
            buckets["spaeter"].append(t)
    return buckets


def _task_line(t: dict) -> str:
    suffix = " (Liste: %s)" % t["list_title"] if t.get("list_title") else ""
    if t.get("is_subtask"):
        suffix = " (Teilaufgabe)" + suffix
    return f"- {t['title']}{suffix}"


_URGENCY_LABELS = {
    "sofort": "sofort",
    "heute": "heute",
    "diese_woche": "diese Woche",
    "spaeter": "später",
}


def _urgency_label(raw: str) -> str:
    return _URGENCY_LABELS.get(str(raw or "").lower(), "später")


def _render_email_section(emails: List[dict], analysis: Dict[str, dict], kind_filter: str, hours: Tuple[float, float]) -> Tuple[List[str], List[dict]]:
    now = datetime.now().replace(microsecond=0)
    lines: List[str] = []
    suggestions: List[dict] = []
    for e in emails:
        a = analysis.get(e["key"]) or {}
        kind = str(a.get("kind") or "").lower()
        if kind == "newsletter":
            if kind_filter != "newsletter":
                continue
        else:
            if kind_filter == "newsletter":
                continue
        age_h = (now - e["ts"]).total_seconds() / 3600.0
        if kind_filter != "newsletter":
            if not (hours[0] <= age_h < hours[1]):
                continue
            if hours[0] >= 24 and not e["unread"]:
                continue
        summary = str(a.get("summary") or e.get("body") or "")[:600]
        importance = a.get("importance") or "mittel"
        urgency = _urgency_label(a.get("urgency"))
        lines.append(
            f"- **{e.get('from')}** — „{e.get('subject')}“ "
            f"({e.get('date_label')}, {e.get('time_label')}):\n"
            f"  {summary}\n"
            f"  Wichtigkeit: {importance}. Dringlichkeit: {urgency}."
        )
        if a.get("reply_needed"):
            suggestions.append({"type": "email", "sender": e.get("from"), "subject": e.get("subject"), "key": e["key"]})
    return lines, suggestions


async def generate_morning_briefing(owner: str = "", max_emails: int = 15) -> str:
    now = datetime.now().replace(microsecond=0)
    candidates = _resolve_candidates(owner)

    emails_raw = await asyncio.to_thread(_gather_emails_sync, owner=owner)
    emails = emails_raw[:max_emails]
    calendar = _gather_calendar(owner=owner)
    tasks_data = await _gather_tasks(owner=owner)

    analysis: Dict[str, dict] = {}
    if candidates and emails:
        analysis = await _analyze_emails(candidates, emails)

    conflicts = _find_conflicts(calendar)
    conflict_lines: List[str] = []
    if conflicts:
        try:
            if candidates:
                conflict_text = await _resolve_conflicts(candidates, conflicts)
                if conflict_text:
                    conflict_lines = [ln for ln in conflict_text.splitlines() if ln.strip()]
        except Exception:
            pass

    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_events = [e for e in calendar if e["start"] < today + timedelta(days=1) and e["end"] > today]

    # ---- Section 1: Mails ----
    news, news_suggestions = _render_email_section(emails, analysis, "newsletter", (0, 168))
    fresh, fresh_suggestions = _render_email_section(emails, analysis, "normal", (0, 24))
    week, week_suggestions = _render_email_section(emails, analysis, "normal", (24, 168))
    email_suggestions = news_suggestions + fresh_suggestions + week_suggestions

    # ---- Section 6 suggestion refs ----
    cal_suggestions: List[dict] = []
    drive_suggestions: List[dict] = []
    for e in emails:
        a = analysis.get(e["key"]) or {}
        prop_cal = a.get("proposed_calendar")
        if isinstance(prop_cal, dict):
            cal_suggestions.append({"type": "calendar", **prop_cal, "source": e.get("from")})
        prop_drive = a.get("drive_filing")
        if isinstance(prop_drive, dict):
            drive_suggestions.append({**prop_drive, "source": e.get("from")})

    # ---- Section 5 top-actions context ----
    context_parts = []
    if today_events:
        context_parts.append("Heutige Termine:")
        for e in sorted(today_events, key=lambda x: x["start"]):
            when = _fmt_time(e["start"]) if not e["all_day"] else "ganztägig"
            context_parts.append(f"- {when} {e['summary']} [{e['calendar']}]")
    urgent_emails = [
        a for key, a in analysis.items()
        if a.get("urgency") in ("heute", "sofort") or a.get("importance") == "hoch"
    ]
    if urgent_emails:
        context_parts.append("Dringend aus E-Mails:")
        for a in urgent_emails[:5]:
            context_parts.append(f"- {str(a.get('summary') or '')[:200]}")
    if conflicts:
        context_parts.append(f"Konflikte: {len(conflicts)} erkannt.")
    groups = _group_tasks(tasks_data["tasks"], now)
    if groups["ueberfaellig"] or groups["heute"]:
        context_parts.append("Fällige Aufgaben:")
        for t in (groups["ueberfaellig"] + groups["heute"])[:6]:
            context_parts.append(f"- {t['title']}")
    for i, cs in enumerate(cal_suggestions, 1):
        context_parts.append(f"- Vorschlag [K{i}]: {cs.get('title')}")
    for i, ds in enumerate(drive_suggestions, 1):
        context_parts.append(f"- Vorschlag [D{i}]: Ablage {ds.get('folder')}")

    top_actions = await _top_actions(candidates, "\n".join(context_parts)) if candidates else []

    # ---- Assembly ----
    l: List[str] = []
    l.append(f"# Morgenbriefing – {_fmt_day(now)}")
    l.append("")
    l.append(f"Guten Morgen. Hier ist dein Morgenbriefing für **{_fmt_date(now)}**.")
    l.append("")

    l.append("## 1. Mails")
    l.append("### 1a. Neu (letzte 24 h)")
    if fresh:
        l.extend(fresh)
    else:
        l.append("Keine neuen Mails in den letzten 24 Stunden.")
    l.append("")
    l.append("### 1b. Diese Woche (offen, 24 h bis 7 Tage)")
    if week:
        l.extend(week)
    else:
        l.append("Keine offenen Mails aus dieser Woche.")
    l.append("")
    l.append("### 1c. Newsletter & Marketing")
    if news:
        shown = news[:3]
        extra = len(news) - len(shown)
        lines_c = shown[:]
        if extra > 0:
            lines_c.append(f"- … und {extra} weitere Newsletter ausgeblendet.")
        l.extend(lines_c)
    else:
        l.append("Keine Newsletter im Zeitfenster.")
    l.append("")

    l.append("## 2. Kalender heute")
    if today_events:
        for e in sorted(today_events, key=lambda x: (x["all_day"], x["start"])):
            tag = f"[{e['calendar']}]"
            if e["all_day"]:
                l.append(f"- {tag} Ganztägig: „{e['summary']}“")
            else:
                loc = f" ({e['location']})" if e.get("location") else ""
                l.append(f"- {tag} {_fmt_time(e['start'])} – {_fmt_time(e['end'])} Uhr: „{e['summary']}“{loc}")
    else:
        l.append("Keine Termine heute.")
    l.append("")
    if conflict_lines:
        l.append("### Konflikte & Hinweise")
        l.extend(conflict_lines)
        l.append("")
    else:
        l.append("> Keine Terminüberschneidungen heute.")
        l.append("")

    l.append("## 3. Ausblick 7 Tage")
    for day_offset in range(1, 8):
        d = today + timedelta(days=day_offset)
        d_events = [e for e in calendar if e["start"] < d + timedelta(days=1) and e["end"] > d]
        l.append(f"### {_fmt_weekday_short(d)}, {_fmt_daymonth(d)}")
        if d_events:
            for e in sorted(d_events, key=lambda x: (x["all_day"], x["start"]))[:6]:
                tag = f"[{e['calendar']}]"
                if e["all_day"]:
                    l.append(f"- {tag} Ganztägig: „{e['summary']}“")
                else:
                    loc = f" ({e['location']})" if e.get("location") else ""
                    l.append(f"- {tag} {_fmt_time(e['start'])}, „{e['summary']}“{loc}")
            if len(d_events) > 6:
                l.append(f"- … und {len(d_events) - 6} weitere.")
        else:
            l.append("- keine Termine —")
    l.append("")

    l.append("## 4. Offene Tasks")
    groups = _group_tasks(tasks_data["tasks"], now)
    bucket_meta = [
        ("ueberfaellig", "Überfällig"),
        ("heute", "Heute"),
        ("woche", "Diese Woche"),
        ("ohne", "Ohne Datum"),
        ("spaeter", "Später"),
    ]
    if not tasks_data["tasks"]:
        l.append("Keine offenen Aufgaben gefunden.")
    else:
        l.append(f"Quelle: {tasks_data.get('source', '')} ({len(tasks_data['tasks'])} offen).")
        for key, label in bucket_meta:
            items = groups[key]
            if not items:
                continue
            shown = items[:8]
            l.append(f"**{label}**")
            for t in shown:
                l.append(_task_line(t))
            if len(items) > 8:
                l.append(f"- … und {len(items) - 8} weitere.")
            l.append("")
    l.append("")

    l.append("## 5. Top-5-Aktionen für heute")
    if top_actions:
        for i, act in enumerate(top_actions, 1):
            l.append(f"{i}. {act}")
    else:
        l.append("Heute steht nichts Dringendes an – genieß den Tag.")
    l.append("")

    l.append("## 6. Vorgeschlagene Aktionen (Anzeige – nichts wird automatisch ausgeführt)")
    l.append("### 6.1 E-Mail-Entwürfe")
    e_lines = []
    for i, s in enumerate(email_suggestions[:6], 1):
        if s["type"] == "email":
            e_lines.append(f"- [E{i}] Antwort an {s['sender']} zu „{s['subject']}“")
    if e_lines:
        l.extend(e_lines)
    else:
        l.append("Keine Antwort erforderlich.")
    l.append("")
    l.append("### 6.2 Kalendereinträge")
    k_lines = []
    for i, s in enumerate(cal_suggestions[:5], 1):
        link = _calendar_deep_link(
            s.get("title") or "",
            s.get("start_iso") or "",
            s.get("end_iso") or "",
            s.get("location") or "",
            s.get("details") or "",
        )
        loc = f" ({s.get('location')})" if s.get("location") else ""
        k_lines.append(f"- [K{i}] „{s.get('title')}“{loc}")
        if link:
            k_lines.append(f"  → {link}")
    if k_lines:
        l.extend(k_lines)
    else:
        l.append("Keine Vorschläge aus E-Mails.")
    l.append("")
    l.append("### 6.3 Drive-Ablagen")
    d_lines = []
    for i, s in enumerate(drive_suggestions[:4], 1):
        files = ", ".join(str(f) for f in (s.get("files") or []))
        d_lines.append(f"- [D{i}] {s.get('folder')}: {files or 'Dateien aus Anhang'}")
    if d_lines:
        l.extend(d_lines)
    else:
        l.append("Keine Ablagen vorgeschlagen.")
    l.append("")
    l.append("### 6.4 So bestätigst du")
    l.append("Alle Vorschläge oben sind reine Anzeige – es wurde **nichts ausgeführt**. "
             "Sag mir einfach, was ich umsetzen soll (z. B. „bestätige K1“, „K2 anlegen“).")
    l.append("")

    l.append("---")
    l.append("*Hinweise:* Keine Aktion automatisch ausgeführt; Termin- und Mail-Vorschläge "
             "können von Knöpfen in der UI abweichen. Offene Antworten stehen unter 1b/1c.")
    return "\n".join(l)


async def run_briefing(owner: str = "") -> str:
    return await generate_morning_briefing(owner=owner)