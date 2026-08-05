from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from core.database import ToolApprovalRecord
from src.tool_approval import ApprovalError, ApprovalStatus, ToolApproval
from src.tool_approval_store import ToolApprovalStore


NOW = datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc)

BASE = {
    "owner": "marc",
    "session_id": "session-1",
    "run_id": "run-1",
    "tool_name": "bash",
    "risk": "host_control",
    "arguments": {"command": "printf ok"},
}


@pytest.fixture
def store(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'approvals.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    ToolApprovalRecord.__table__.create(engine)
    factory = sessionmaker(
        bind=engine,
        autocommit=False,
        autoflush=False,
    )
    try:
        yield ToolApprovalStore(factory)
    finally:
        engine.dispose()


def make_approval(**changes):
    values = {**BASE, **changes}
    return ToolApproval.create(
        **values,
        now=NOW,
        ttl=timedelta(minutes=10),
    )


def test_create_and_get_round_trip(store):
    original = make_approval()
    created = store.create(original)
    loaded = store.get(original.id, owner="marc")

    assert created == original
    assert loaded == original
    assert loaded.created_at.tzinfo == timezone.utc
    assert loaded.expires_at.tzinfo == timezone.utc


def test_owner_scope_hides_approval(store):
    approval = store.create(make_approval())

    with pytest.raises(ApprovalError, match="unavailable"):
        store.get(approval.id, owner="other")


def test_approve_is_persisted(store):
    approval = store.create(make_approval())
    decided = NOW + timedelta(seconds=1)

    approved = store.approve(
        approval.id,
        owner="marc",
        now=decided,
    )

    assert approved.status is ApprovalStatus.APPROVED
    assert approved.decided_at == decided
    assert store.get(
        approval.id,
        owner="marc",
    ).status is ApprovalStatus.APPROVED


def test_reject_is_persisted(store):
    approval = store.create(make_approval())
    rejected = store.reject(
        approval.id,
        owner="marc",
        now=NOW + timedelta(seconds=1),
    )

    assert rejected.status is ApprovalStatus.REJECTED


def test_decision_is_one_shot(store):
    approval = store.create(make_approval())
    store.approve(
        approval.id,
        owner="marc",
        now=NOW + timedelta(seconds=1),
    )

    with pytest.raises(ApprovalError, match="approved"):
        store.reject(
            approval.id,
            owner="marc",
            now=NOW + timedelta(seconds=2),
        )


def test_exact_approved_invocation_is_consumed(store):
    approval = store.create(make_approval())
    store.approve(
        approval.id,
        owner="marc",
        now=NOW + timedelta(seconds=1),
    )

    consumed = store.consume(
        approval.id,
        **BASE,
        now=NOW + timedelta(seconds=2),
    )

    assert consumed.status is ApprovalStatus.CONSUMED
    assert consumed.consumed_at == NOW + timedelta(seconds=2)


def test_consumption_is_atomic_and_cannot_be_repeated(store):
    approval = store.create(make_approval())
    store.approve(
        approval.id,
        owner="marc",
        now=NOW + timedelta(seconds=1),
    )
    store.consume(
        approval.id,
        **BASE,
        now=NOW + timedelta(seconds=2),
    )

    with pytest.raises(ApprovalError, match="consumed"):
        store.consume(
            approval.id,
            **BASE,
            now=NOW + timedelta(seconds=3),
        )


def test_changed_arguments_fail_closed(store):
    approval = store.create(make_approval())
    store.approve(
        approval.id,
        owner="marc",
        now=NOW + timedelta(seconds=1),
    )

    changed = {
        **BASE,
        "arguments": {"command": "rm -rf target"},
    }
    with pytest.raises(ApprovalError, match="binding"):
        store.consume(
            approval.id,
            **changed,
            now=NOW + timedelta(seconds=2),
        )

    assert store.get(
        approval.id,
        owner="marc",
    ).status is ApprovalStatus.APPROVED


def test_expired_approval_is_marked_expired(store):
    approval = store.create(make_approval())
    store.approve(
        approval.id,
        owner="marc",
        now=NOW + timedelta(seconds=1),
    )

    with pytest.raises(ApprovalError, match="expired"):
        store.consume(
            approval.id,
            **BASE,
            now=NOW + timedelta(minutes=10),
        )

    assert store.get(
        approval.id,
        owner="marc",
    ).status is ApprovalStatus.EXPIRED


def test_identical_fingerprints_can_have_distinct_approvals(store):
    first = store.create(make_approval())
    second = store.create(make_approval())

    assert first.id != second.id
    assert first.fingerprint == second.fingerprint


def test_naive_operation_timestamp_is_rejected(store):
    approval = store.create(make_approval())
    naive = datetime(2026, 8, 5, 9, 0)

    with pytest.raises(ApprovalError, match="timezone-aware"):
        store.approve(
            approval.id,
            owner="marc",
            now=naive,
        )


def test_table_has_expected_indexes():
    names = {index.name for index in ToolApprovalRecord.__table__.indexes}

    assert names == {
        "ix_tool_approvals_owner_session_status",
        "ix_tool_approvals_status_expires",
        "ix_tool_approvals_fingerprint",
    }
