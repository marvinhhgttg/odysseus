"""Persistent, atomic storage for one-shot tool approvals."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy import update
from sqlalchemy.orm import Session as OrmSession

from core.database import SessionLocal, ToolApprovalRecord
from src.tool_approval import (
    ApprovalError,
    ApprovalStatus,
    ToolApproval,
    approval_fingerprint,
    argument_hash,
    utcnow,
)


SessionFactory = Callable[[], OrmSession]


def _to_db_time(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ApprovalError("timestamp must be timezone-aware")
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _from_db_time(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _to_domain(row: ToolApprovalRecord) -> ToolApproval:
    try:
        status = ApprovalStatus(row.status)
    except ValueError as exc:
        raise ApprovalError(f"unknown persisted approval state {row.status!r}") from exc

    return ToolApproval(
        id=row.id,
        owner=row.owner,
        session_id=row.session_id,
        run_id=row.run_id,
        tool_name=row.tool_name,
        risk=row.risk,
        argument_hash=row.argument_hash,
        fingerprint=row.fingerprint,
        status=status,
        created_at=_from_db_time(row.created_at),
        expires_at=_from_db_time(row.expires_at),
        decided_at=_from_db_time(row.decided_at),
        consumed_at=_from_db_time(row.consumed_at),
    )


def _validate_tool_content(
    approval: ToolApproval,
    tool_content: str,
) -> str:
    """Verify that persisted content is exactly bound to the approval."""

    if not isinstance(tool_content, str):
        raise ApprovalError("tool content must be a string")

    if argument_hash(tool_content) != approval.argument_hash:
        raise ApprovalError(
            "tool content does not match approval argument hash"
        )

    candidate = approval_fingerprint(
        owner=approval.owner,
        session_id=approval.session_id,
        run_id=approval.run_id,
        tool_name=approval.tool_name,
        risk=approval.risk,
        arguments=tool_content,
    )
    if candidate != approval.fingerprint:
        raise ApprovalError(
            "tool content does not match approval fingerprint"
        )

    return tool_content


class ToolApprovalStore:
    """Store with owner-scoped and atomic lifecycle transitions."""

    def __init__(self, session_factory: SessionFactory | None = None) -> None:
        self._session_factory = session_factory or SessionLocal

    def create(
        self,
        approval: ToolApproval,
        *,
        tool_content: str,
    ) -> ToolApproval:
        tool_content = _validate_tool_content(
            approval,
            tool_content,
        )
        db = self._session_factory()
        try:
            row = ToolApprovalRecord(
                id=approval.id,
                owner=approval.owner,
                session_id=approval.session_id,
                run_id=approval.run_id,
                tool_name=approval.tool_name,
                risk=approval.risk,
                argument_hash=approval.argument_hash,
                fingerprint=approval.fingerprint,
                tool_content=tool_content,
                status=approval.status.value,
                created_at=_to_db_time(approval.created_at),
                expires_at=_to_db_time(approval.expires_at),
                decided_at=_to_db_time(approval.decided_at)
                if approval.decided_at
                else None,
                consumed_at=_to_db_time(approval.consumed_at)
                if approval.consumed_at
                else None,
            )
            db.add(row)
            db.commit()
            db.refresh(row)
            return _to_domain(row)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def load_tool_content(
        self,
        approval_id: str,
        *,
        owner: str,
    ) -> str:
        """Load and verify the exact persisted invocation payload."""

        db = self._session_factory()
        try:
            row = (
                db.query(ToolApprovalRecord)
                .filter(
                    ToolApprovalRecord.id == approval_id,
                    ToolApprovalRecord.owner == owner,
                )
                .first()
            )
            if row is None:
                raise ApprovalError("approval is unavailable")
            if row.tool_content is None:
                raise ApprovalError(
                    "approval has no resumable tool content"
                )

            approval = _to_domain(row)
            return _validate_tool_content(
                approval,
                row.tool_content,
            )
        except ApprovalError:
            raise
        except Exception as exc:
            raise ApprovalError(
                "approval tool content is unavailable or invalid"
            ) from exc
        finally:
            db.close()

    def get(self, approval_id: str, *, owner: str) -> ToolApproval:
        db = self._session_factory()
        try:
            row = (
                db.query(ToolApprovalRecord)
                .filter(
                    ToolApprovalRecord.id == approval_id,
                    ToolApprovalRecord.owner == owner,
                )
                .first()
            )
            if row is None:
                raise ApprovalError("approval is unavailable")
            return _to_domain(row)
        finally:
            db.close()

    def approve(
        self,
        approval_id: str,
        *,
        owner: str,
        now: datetime | None = None,
    ) -> ToolApproval:
        return self._decide(
            approval_id,
            owner=owner,
            target=ApprovalStatus.APPROVED,
            now=now,
        )

    def reject(
        self,
        approval_id: str,
        *,
        owner: str,
        now: datetime | None = None,
    ) -> ToolApproval:
        return self._decide(
            approval_id,
            owner=owner,
            target=ApprovalStatus.REJECTED,
            now=now,
        )

    def _decide(
        self,
        approval_id: str,
        *,
        owner: str,
        target: ApprovalStatus,
        now: datetime | None,
    ) -> ToolApproval:
        checked = now or utcnow()
        checked_db = _to_db_time(checked)
        db = self._session_factory()

        try:
            result = db.execute(
                update(ToolApprovalRecord)
                .where(
                    ToolApprovalRecord.id == approval_id,
                    ToolApprovalRecord.owner == owner,
                    ToolApprovalRecord.status == ApprovalStatus.PENDING.value,
                    ToolApprovalRecord.expires_at > checked_db,
                )
                .values(
                    status=target.value,
                    decided_at=checked_db,
                )
            )

            if result.rowcount != 1:
                self._raise_failed_transition(
                    db,
                    approval_id=approval_id,
                    owner=owner,
                    now_db=checked_db,
                    action=target.value,
                )

            db.commit()
            row = (
                db.query(ToolApprovalRecord)
                .filter(
                    ToolApprovalRecord.id == approval_id,
                    ToolApprovalRecord.owner == owner,
                )
                .one()
            )
            return _to_domain(row)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def consume(
        self,
        approval_id: str,
        *,
        owner: str,
        session_id: str,
        run_id: str,
        tool_name: str,
        risk: str,
        arguments: Any,
        now: datetime | None = None,
    ) -> ToolApproval:
        checked = now or utcnow()
        checked_db = _to_db_time(checked)

        candidate = approval_fingerprint(
            owner=owner,
            session_id=session_id,
            run_id=run_id,
            tool_name=tool_name,
            risk=risk,
            arguments=arguments,
        )
        args_hash = argument_hash(arguments)

        normalized_owner = owner.strip()
        normalized_session = session_id.strip()
        normalized_run = run_id.strip()
        normalized_tool = tool_name.strip().lower()
        normalized_risk = risk.strip().lower()

        db = self._session_factory()
        try:
            result = db.execute(
                update(ToolApprovalRecord)
                .where(
                    ToolApprovalRecord.id == approval_id,
                    ToolApprovalRecord.owner == normalized_owner,
                    ToolApprovalRecord.session_id == normalized_session,
                    ToolApprovalRecord.run_id == normalized_run,
                    ToolApprovalRecord.tool_name == normalized_tool,
                    ToolApprovalRecord.risk == normalized_risk,
                    ToolApprovalRecord.argument_hash == args_hash,
                    ToolApprovalRecord.fingerprint == candidate,
                    ToolApprovalRecord.status == ApprovalStatus.APPROVED.value,
                    ToolApprovalRecord.expires_at > checked_db,
                )
                .values(
                    status=ApprovalStatus.CONSUMED.value,
                    consumed_at=checked_db,
                )
            )

            if result.rowcount != 1:
                self._raise_failed_consumption(
                    db,
                    approval_id=approval_id,
                    owner=normalized_owner,
                    fingerprint=candidate,
                    now_db=checked_db,
                )

            db.commit()
            row = (
                db.query(ToolApprovalRecord)
                .filter(
                    ToolApprovalRecord.id == approval_id,
                    ToolApprovalRecord.owner == normalized_owner,
                )
                .one()
            )
            return _to_domain(row)
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _expire_if_needed(
        db: OrmSession,
        row: ToolApprovalRecord,
        now_db: datetime,
    ) -> bool:
        if (
            row.status
            in (
                ApprovalStatus.PENDING.value,
                ApprovalStatus.APPROVED.value,
            )
            and row.expires_at <= now_db
        ):
            result = db.execute(
                update(ToolApprovalRecord)
                .where(
                    ToolApprovalRecord.id == row.id,
                    ToolApprovalRecord.status == row.status,
                    ToolApprovalRecord.expires_at <= now_db,
                )
                .values(status=ApprovalStatus.EXPIRED.value)
            )
            if result.rowcount == 1:
                db.commit()
            return True
        return row.status == ApprovalStatus.EXPIRED.value

    def _raise_failed_transition(
        self,
        db: OrmSession,
        *,
        approval_id: str,
        owner: str,
        now_db: datetime,
        action: str,
    ) -> None:
        row = (
            db.query(ToolApprovalRecord)
            .filter(
                ToolApprovalRecord.id == approval_id,
                ToolApprovalRecord.owner == owner,
            )
            .first()
        )
        if row is None:
            raise ApprovalError("approval is unavailable")

        if self._expire_if_needed(db, row, now_db):
            raise ApprovalError("approval has expired")

        raise ApprovalError(
            f"cannot {action} approval in state {row.status}"
        )

    def _raise_failed_consumption(
        self,
        db: OrmSession,
        *,
        approval_id: str,
        owner: str,
        fingerprint: str,
        now_db: datetime,
    ) -> None:
        row = (
            db.query(ToolApprovalRecord)
            .filter(
                ToolApprovalRecord.id == approval_id,
                ToolApprovalRecord.owner == owner,
            )
            .first()
        )
        if row is None:
            raise ApprovalError("approval is unavailable")

        if self._expire_if_needed(db, row, now_db):
            raise ApprovalError("approval has expired")

        if row.status != ApprovalStatus.APPROVED.value:
            raise ApprovalError(
                f"cannot consume approval in state {row.status}"
            )

        if row.fingerprint != fingerprint:
            raise ApprovalError(
                "approval binding does not match tool invocation"
            )

        raise ApprovalError(
            "approval binding does not match tool invocation"
        )
