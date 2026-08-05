"""Fail-closed lifecycle primitives for one-shot tool approvals."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CONSUMED = "consumed"


class ApprovalError(ValueError):
    """Raised when an approval transition or binding check fails."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _require_text(name: str, value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ApprovalError(f"{name} must be a non-empty string")
    return value.strip()


def canonical_arguments(arguments: Any) -> str:
    """Return a deterministic, type-tagged representation for hashing.

    JSON strings are parsed and canonicalized. Non-JSON strings remain exact,
    which preserves whitespace and quoting for shell-style tool arguments.
    """
    if isinstance(arguments, str):
        try:
            parsed = json.loads(arguments)
        except json.JSONDecodeError:
            return "raw:" + arguments
        return "json:" + json.dumps(
            parsed,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )

    try:
        return "json:" + json.dumps(
            arguments,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise ApprovalError("arguments must be JSON-compatible or a string") from exc


def argument_hash(arguments: Any) -> str:
    canonical = canonical_arguments(arguments)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def approval_fingerprint(
    *,
    owner: str,
    session_id: str,
    run_id: str,
    tool_name: str,
    risk: str,
    arguments: Any,
) -> str:
    binding = {
        "owner": _require_text("owner", owner),
        "session_id": _require_text("session_id", session_id),
        "run_id": _require_text("run_id", run_id),
        "tool_name": _require_text("tool_name", tool_name).lower(),
        "risk": _require_text("risk", risk).lower(),
        "argument_hash": argument_hash(arguments),
    }
    encoded = json.dumps(
        binding,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ToolApproval:
    id: str
    owner: str
    session_id: str
    run_id: str
    tool_name: str
    risk: str
    argument_hash: str
    fingerprint: str
    status: ApprovalStatus
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    consumed_at: datetime | None = None

    @classmethod
    def create(
        cls,
        *,
        owner: str,
        session_id: str,
        run_id: str,
        tool_name: str,
        risk: str,
        arguments: Any,
        now: datetime | None = None,
        ttl: timedelta = timedelta(minutes=10),
    ) -> "ToolApproval":
        if ttl <= timedelta(0):
            raise ApprovalError("ttl must be positive")

        created = now or utcnow()
        if created.tzinfo is None:
            raise ApprovalError("now must be timezone-aware")

        owner = _require_text("owner", owner)
        session_id = _require_text("session_id", session_id)
        run_id = _require_text("run_id", run_id)
        tool_name = _require_text("tool_name", tool_name).lower()
        risk = _require_text("risk", risk).lower()
        args_hash = argument_hash(arguments)

        return cls(
            id=str(uuid.uuid4()),
            owner=owner,
            session_id=session_id,
            run_id=run_id,
            tool_name=tool_name,
            risk=risk,
            argument_hash=args_hash,
            fingerprint=approval_fingerprint(
                owner=owner,
                session_id=session_id,
                run_id=run_id,
                tool_name=tool_name,
                risk=risk,
                arguments=arguments,
            ),
            status=ApprovalStatus.PENDING,
            created_at=created,
            expires_at=created + ttl,
        )

    def effective_status(self, *, now: datetime | None = None) -> ApprovalStatus:
        checked = now or utcnow()
        if checked.tzinfo is None:
            raise ApprovalError("now must be timezone-aware")
        if self.status in (ApprovalStatus.PENDING, ApprovalStatus.APPROVED):
            if checked >= self.expires_at:
                return ApprovalStatus.EXPIRED
        return self.status

    def approve(self, *, now: datetime | None = None) -> "ToolApproval":
        decided = now or utcnow()
        status = self.effective_status(now=decided)
        if status is ApprovalStatus.EXPIRED:
            return replace(self, status=ApprovalStatus.EXPIRED)
        if status is not ApprovalStatus.PENDING:
            raise ApprovalError(f"cannot approve approval in state {status.value}")
        return replace(
            self,
            status=ApprovalStatus.APPROVED,
            decided_at=decided,
        )

    def reject(self, *, now: datetime | None = None) -> "ToolApproval":
        decided = now or utcnow()
        status = self.effective_status(now=decided)
        if status is ApprovalStatus.EXPIRED:
            return replace(self, status=ApprovalStatus.EXPIRED)
        if status is not ApprovalStatus.PENDING:
            raise ApprovalError(f"cannot reject approval in state {status.value}")
        return replace(
            self,
            status=ApprovalStatus.REJECTED,
            decided_at=decided,
        )

    def consume(
        self,
        *,
        owner: str,
        session_id: str,
        run_id: str,
        tool_name: str,
        risk: str,
        arguments: Any,
        now: datetime | None = None,
    ) -> "ToolApproval":
        consumed = now or utcnow()
        status = self.effective_status(now=consumed)
        if status is ApprovalStatus.EXPIRED:
            raise ApprovalError("approval has expired")
        if status is not ApprovalStatus.APPROVED:
            raise ApprovalError(f"cannot consume approval in state {status.value}")

        candidate = approval_fingerprint(
            owner=owner,
            session_id=session_id,
            run_id=run_id,
            tool_name=tool_name,
            risk=risk,
            arguments=arguments,
        )
        if candidate != self.fingerprint:
            raise ApprovalError("approval binding does not match tool invocation")

        return replace(
            self,
            status=ApprovalStatus.CONSUMED,
            consumed_at=consumed,
        )
