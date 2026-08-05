from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from core.database import (
    ToolApprovalRecord,
    migrate_add_tool_approval_content,
)
from src.tool_approval import ApprovalError, ApprovalStatus, ToolApproval
from src.tool_approval_store import ToolApprovalStore


NOW = datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc)

TOOL_CONTENT = '{"command":"printf ok"}'

BASE = {
    "owner": "marc",
    "session_id": "session-1",
    "run_id": "run-1",
    "tool_name": "bash",
    "risk": "host_control",
    "arguments": TOOL_CONTENT,
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
    created = store.create(original, tool_content=TOOL_CONTENT)
    loaded = store.get(original.id, owner="marc")

    assert created == original
    assert loaded == original
    assert loaded.created_at.tzinfo == timezone.utc
    assert loaded.expires_at.tzinfo == timezone.utc


def test_owner_scope_hides_approval(store):
    approval = store.create(make_approval(), tool_content=TOOL_CONTENT)

    with pytest.raises(ApprovalError, match="unavailable"):
        store.get(approval.id, owner="other")


def test_approve_is_persisted(store):
    approval = store.create(make_approval(), tool_content=TOOL_CONTENT)
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
    approval = store.create(make_approval(), tool_content=TOOL_CONTENT)
    rejected = store.reject(
        approval.id,
        owner="marc",
        now=NOW + timedelta(seconds=1),
    )

    assert rejected.status is ApprovalStatus.REJECTED


def test_decision_is_one_shot(store):
    approval = store.create(make_approval(), tool_content=TOOL_CONTENT)
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
    approval = store.create(make_approval(), tool_content=TOOL_CONTENT)
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
    approval = store.create(make_approval(), tool_content=TOOL_CONTENT)
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
    approval = store.create(make_approval(), tool_content=TOOL_CONTENT)
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
    approval = store.create(make_approval(), tool_content=TOOL_CONTENT)
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
    first = store.create(make_approval(), tool_content=TOOL_CONTENT)
    second = store.create(make_approval(), tool_content=TOOL_CONTENT)

    assert first.id != second.id
    assert first.fingerprint == second.fingerprint


def test_naive_operation_timestamp_is_rejected(store):
    approval = store.create(make_approval(), tool_content=TOOL_CONTENT)
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


def test_tool_content_round_trip_is_encrypted_at_rest(store):
    approval = store.create(
        make_approval(),
        tool_content=TOOL_CONTENT,
    )

    assert store.load_tool_content(
        approval.id,
        owner="marc",
    ) == TOOL_CONTENT

    bind = store._session_factory.kw["bind"]
    with bind.connect() as conn:
        raw = conn.exec_driver_sql(
            "SELECT tool_content FROM tool_approvals WHERE id = ?",
            (approval.id,),
        ).scalar_one()

    assert raw != TOOL_CONTENT
    assert raw.startswith("enc:")


def test_create_rejects_content_not_bound_to_approval(store):
    approval = make_approval()

    with pytest.raises(ApprovalError, match="argument hash"):
        store.create(
            approval,
            tool_content='{"command":"different"}',
        )


def test_tampered_persisted_content_fails_closed(store):
    approval = store.create(
        make_approval(),
        tool_content=TOOL_CONTENT,
    )
    bind = store._session_factory.kw["bind"]

    with bind.begin() as conn:
        conn.exec_driver_sql(
            "UPDATE tool_approvals "
            "SET tool_content = ? WHERE id = ?",
            ('{"command":"tampered"}', approval.id),
        )

    with pytest.raises(ApprovalError, match="does not match"):
        store.load_tool_content(
            approval.id,
            owner="marc",
        )


def test_tool_content_is_owner_scoped(store):
    approval = store.create(
        make_approval(),
        tool_content=TOOL_CONTENT,
    )

    with pytest.raises(ApprovalError, match="unavailable"):
        store.load_tool_content(
            approval.id,
            owner="other",
        )


def test_legacy_row_without_content_fails_closed(tmp_path):
    legacy_engine = create_engine(
        f"sqlite:///{tmp_path / 'legacy-row.db'}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )

    try:
        with legacy_engine.begin() as conn:
            conn.exec_driver_sql(
                """
                CREATE TABLE tool_approvals (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    risk TEXT NOT NULL,
                    argument_hash TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at DATETIME NOT NULL,
                    expires_at DATETIME NOT NULL,
                    decided_at DATETIME,
                    consumed_at DATETIME
                )
                """
            )

        migrate_add_tool_approval_content(legacy_engine)

        approval = make_approval()
        with legacy_engine.begin() as conn:
            conn.exec_driver_sql(
                """
                INSERT INTO tool_approvals (
                    id, owner, session_id, run_id, tool_name, risk,
                    argument_hash, fingerprint, status, created_at,
                    expires_at, decided_at, consumed_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval.id,
                    approval.owner,
                    approval.session_id,
                    approval.run_id,
                    approval.tool_name,
                    approval.risk,
                    approval.argument_hash,
                    approval.fingerprint,
                    approval.status.value,
                    approval.created_at.replace(tzinfo=None),
                    approval.expires_at.replace(tzinfo=None),
                    None,
                    None,
                ),
            )

        factory = sessionmaker(
            bind=legacy_engine,
            autocommit=False,
            autoflush=False,
        )
        legacy_store = ToolApprovalStore(factory)

        with pytest.raises(ApprovalError, match="no resumable"):
            legacy_store.load_tool_content(
                approval.id,
                owner="marc",
            )
    finally:
        legacy_engine.dispose()

def test_tool_content_migration_is_idempotent(tmp_path):
    legacy_engine = create_engine(
        f"sqlite:///{tmp_path / 'legacy-approval.db'}",
    )
    try:
        with legacy_engine.begin() as conn:
            conn.exec_driver_sql(
                """
                CREATE TABLE tool_approvals (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL
                )
                """
            )

        migrate_add_tool_approval_content(legacy_engine)
        migrate_add_tool_approval_content(legacy_engine)

        with legacy_engine.connect() as conn:
            columns = {
                row[1]
                for row in conn.exec_driver_sql(
                    "PRAGMA table_info(tool_approvals)"
                ).fetchall()
            }

        assert "tool_content" in columns
    finally:
        legacy_engine.dispose()
