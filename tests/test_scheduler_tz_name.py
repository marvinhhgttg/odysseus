"""Tests for IANA-timezone-aware scheduling (per-task tz_name).

compute_next_run with a tz_name interprets scheduled_time as local wall-clock
time in that zone and converts to naive UTC for DB storage, so a "daily 05:00"
task stays at 05:00 Berlin across DST transitions. Without a tz_name the legacy
naive-UTC interpretation is preserved.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from src.task_scheduler import compute_next_run, _resolve_task_timezone


def test_daily_berlin_dst_converts_to_utc():
    # 2026-09-14 is CEST (UTC+2): 05:00 Berlin = 03:00 UTC.
    now = datetime(2026, 9, 13, 20, 0)
    nxt = compute_next_run("daily", "05:00", after=now, tz_name="Europe/Berlin")
    assert nxt == datetime(2026, 9, 14, 3, 0)
    assert nxt.replace(tzinfo=timezone.utc).astimezone(
        ZoneInfo("Europe/Berlin")
    ).strftime("%H:%M") == "05:00"


def test_daily_berlin_cet_shift_keeps_local_time():
    # 2026-12-02 is CET (UTC+1): 05:00 Berlin = 04:00 UTC.
    now = datetime(2026, 12, 1, 20, 0)
    nxt = compute_next_run("daily", "05:00", after=now, tz_name="Europe/Berlin")
    assert nxt == datetime(2026, 12, 2, 4, 0)
    assert nxt.replace(tzinfo=timezone.utc).astimezone(
        ZoneInfo("Europe/Berlin")
    ).strftime("%H:%M") == "05:00"


def test_no_tz_preserves_legacy_naive_utc_behavior():
    now = datetime(2026, 9, 13, 20, 0)
    assert compute_next_run("daily", "05:00", after=now) == datetime(2026, 9, 14, 5, 0)


def test_resolve_task_timezone_prefers_task_tz_name():
    class Task:
        def __init__(self, tz_name=None, crew_member_id=None):
            self.tz_name = tz_name
            self.crew_member_id = crew_member_id

    # With a crew member whose tz differs, the explicit task tz_name wins.
    assert _resolve_task_timezone(None, Task(tz_name="Europe/Berlin", crew_member_id="c1")) == "Europe/Berlin"


def test_resolve_task_timezone_falls_back_to_crew():
    class Task:
        def __init__(self, tz_name=None, crew_member_id=None):
            self.tz_name = tz_name
            self.crew_member_id = crew_member_id

    class FakeDB:
        def query(self, model):
            return self

        def filter(self, *a, **k):
            return self

        def first(self):
            return _Cm()

    class _Cm:
        timezone = "America/New_York"

    assert _resolve_task_timezone(FakeDB(), Task(tz_name=None, crew_member_id="c1")) == "America/New_York"