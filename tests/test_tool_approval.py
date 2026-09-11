from datetime import datetime, timedelta, timezone

import pytest

from src.tool_approval import (
    ApprovalError,
    ApprovalStatus,
    ToolApproval,
    approval_fingerprint,
    argument_hash,
)


NOW = datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc)

BASE = {
    "owner": "marc",
    "session_id": "session-1",
    "run_id": "run-1",
    "tool_name": "bash",
    "risk": "host_control",
    "arguments": {"command": "printf ok"},
}


def make_approval(**changes):
    values = {**BASE, **changes}
    return ToolApproval.create(**values, now=NOW)


def test_json_argument_hash_is_independent_of_key_order():
    left = argument_hash('{"b":2,"a":1}')
    right = argument_hash({"a": 1, "b": 2})
    assert left == right


def test_raw_argument_hash_preserves_whitespace():
    assert argument_hash("echo ok") != argument_hash("echo  ok")


def test_fingerprint_is_bound_to_owner():
    original = approval_fingerprint(**BASE)
    changed = approval_fingerprint(**{**BASE, "owner": "other"})
    assert original != changed


@pytest.mark.parametrize(
    "field,value",
    [
        ("session_id", "session-2"),
        ("run_id", "run-2"),
        ("tool_name", "python"),
        ("risk", "destructive"),
        ("arguments", {"command": "printf changed"}),
    ],
)
def test_fingerprint_is_bound_to_invocation(field, value):
    original = approval_fingerprint(**BASE)
    changed = approval_fingerprint(**{**BASE, field: value})
    assert original != changed


def test_create_starts_pending_with_expiry():
    approval = make_approval()
    assert approval.status is ApprovalStatus.PENDING
    assert approval.expires_at == NOW + timedelta(seconds=60)
    assert approval.decided_at is None
    assert approval.consumed_at is None


def test_approve_then_consume_exact_binding():
    approved = make_approval().approve(now=NOW + timedelta(seconds=1))
    consumed = approved.consume(
        **BASE,
        now=NOW + timedelta(seconds=2),
    )
    assert approved.status is ApprovalStatus.APPROVED
    assert consumed.status is ApprovalStatus.CONSUMED
    assert consumed.consumed_at == NOW + timedelta(seconds=2)


def test_rejected_approval_cannot_be_consumed():
    rejected = make_approval().reject(now=NOW + timedelta(seconds=1))
    with pytest.raises(ApprovalError, match="rejected"):
        rejected.consume(**BASE, now=NOW + timedelta(seconds=2))


def test_pending_approval_cannot_be_consumed():
    with pytest.raises(ApprovalError, match="pending"):
        make_approval().consume(**BASE, now=NOW + timedelta(seconds=1))


def test_consumed_approval_cannot_be_reused():
    consumed = (
        make_approval()
        .approve(now=NOW + timedelta(seconds=1))
        .consume(**BASE, now=NOW + timedelta(seconds=2))
    )
    with pytest.raises(ApprovalError, match="consumed"):
        consumed.consume(**BASE, now=NOW + timedelta(seconds=3))


def test_changed_arguments_fail_closed():
    approved = make_approval().approve(now=NOW + timedelta(seconds=1))
    changed = {**BASE, "arguments": {"command": "rm -rf target"}}
    with pytest.raises(ApprovalError, match="binding"):
        approved.consume(**changed, now=NOW + timedelta(seconds=2))


def test_changed_run_fails_closed():
    approved = make_approval().approve(now=NOW + timedelta(seconds=1))
    changed = {**BASE, "run_id": "another-run"}
    with pytest.raises(ApprovalError, match="binding"):
        approved.consume(**changed, now=NOW + timedelta(seconds=2))


def test_auto_approved_stale_pending_can_be_consumed():
    # No explicit decision: after the window the PENDING approval silently
    # becomes APPROVED and can be consumed.
    stale = make_approval()
    assert stale.effective_status(now=NOW + timedelta(minutes=10)) is ApprovalStatus.APPROVED
    consumed = stale.consume(**BASE, now=NOW + timedelta(minutes=10))
    assert consumed.status is ApprovalStatus.CONSUMED


def test_stale_approved_approval_stays_approved_and_consumable():
    approved = make_approval().approve(now=NOW + timedelta(seconds=1))
    # APPROVED does not die at expiry: the resume/consume path may run just
    # after the auto-approve window, because consume happens on resume.
    consumed = approved.consume(**BASE, now=NOW + timedelta(minutes=10))
    assert consumed.status is ApprovalStatus.CONSUMED


def test_rejecting_stale_pending_records_rejection():
    # Explicit Reject always wins, even after the auto-approve window.
    rejected = make_approval().reject(now=NOW + timedelta(minutes=10))
    assert rejected.status is ApprovalStatus.REJECTED


@pytest.mark.parametrize(
    "field",
    ["owner", "session_id", "run_id", "tool_name", "risk"],
)
def test_empty_binding_fields_are_rejected(field):
    values = {**BASE, field: " "}
    with pytest.raises(ApprovalError, match=field):
        ToolApproval.create(**values, now=NOW)


def test_nonpositive_ttl_is_rejected():
    with pytest.raises(ApprovalError, match="ttl"):
        ToolApproval.create(**BASE, now=NOW, ttl=timedelta(0))


def test_naive_timestamp_is_rejected():
    naive = datetime(2026, 8, 5, 9, 0)
    with pytest.raises(ApprovalError, match="timezone-aware"):
        ToolApproval.create(**BASE, now=naive)
