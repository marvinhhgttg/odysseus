"""Approval enforcement boundary for exact tool invocations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from src.tool_approval import ToolApproval
from src.tool_approval_store import ToolApprovalStore
from src.tool_risk import ToolRiskAssessment, classify_tool_risk


BlockFactory = Callable[[str, str], Any]


def _default_block_factory(tool_name: str, content: str) -> Any:
    from src.agent_tools import ToolBlock

    return ToolBlock(tool_name, content)


@dataclass(frozen=True)
class ApprovalPrompt:
    """Redacted approval details safe to send to the frontend."""

    approval_id: str
    tool_name: str
    risk: str
    source: str
    expires_at: datetime

    def to_dict(self) -> dict[str, str]:
        return {
            "approvalId": self.approval_id,
            "tool": self.tool_name,
            "risk": self.risk,
            "source": self.source,
            "expiresAt": self.expires_at.isoformat(),
        }


@dataclass(frozen=True)
class EnforcementDecision:
    """Result of checking one proposed tool invocation."""

    block: Optional[Any]
    assessment: ToolRiskAssessment
    approval: Optional[ApprovalPrompt]

    @property
    def requires_approval(self) -> bool:
        return self.approval is not None


class ToolApprovalEnforcer:
    """Create and consume approvals without executing tools."""

    def __init__(
        self,
        store: Optional[ToolApprovalStore] = None,
        *,
        block_factory: Optional[BlockFactory] = None,
        approval_ttl: timedelta = timedelta(minutes=10),
    ) -> None:
        if approval_ttl <= timedelta(0):
            raise ValueError("approval_ttl must be positive")

        self._store = store or ToolApprovalStore()
        self._block_factory = block_factory or _default_block_factory
        self._approval_ttl = approval_ttl

    def check(
        self,
        block: Any,
        *,
        owner: str,
        session_id: str,
        run_id: str,
        annotations: Any = None,
        now: Optional[datetime] = None,
    ) -> EnforcementDecision:
        """Allow a low-risk block or persist an exact pending approval."""

        tool_name = getattr(block, "tool_type", None)
        if tool_name is None:
            tool_name = getattr(block, "tooltype", None)
        if not isinstance(tool_name, str) or not tool_name.strip():
            raise ApprovalError("tool block has no valid tool type")

        tool_content = getattr(block, "content", None)
        if not isinstance(tool_content, str):
            raise ApprovalError("tool block content must be a string")
        assessment = classify_tool_risk(
            tool_name,
            annotations=annotations,
        )

        if not assessment.approval_would_be_required:
            return EnforcementDecision(
                block=block,
                assessment=assessment,
                approval=None,
            )

        approval = ToolApproval.create(
            owner=owner,
            session_id=session_id,
            run_id=run_id,
            tool_name=tool_name,
            risk=assessment.risk.value,
            arguments=tool_content,
            now=now,
            ttl=self._approval_ttl,
        )
        approval = self._store.create(
            approval,
            tool_content=tool_content,
        )

        prompt = ApprovalPrompt(
            approval_id=approval.id,
            tool_name=approval.tool_name,
            risk=approval.risk,
            source=assessment.source,
            expires_at=approval.expires_at,
        )
        return EnforcementDecision(
            block=None,
            assessment=assessment,
            approval=prompt,
        )

    def resume(
        self,
        approval_id: str,
        *,
        owner: str,
        session_id: str,
        run_id: str,
        now: Optional[datetime] = None,
    ) -> Any:
        """Verify, atomically consume, and reconstruct one approved block."""

        approval = self._store.get(
            approval_id,
            owner=owner,
        )
        tool_content = self._store.load_tool_content(
            approval_id,
            owner=owner,
        )

        self._store.consume(
            approval_id,
            owner=owner,
            session_id=session_id,
            run_id=run_id,
            tool_name=approval.tool_name,
            risk=approval.risk,
            arguments=tool_content,
            now=now,
        )

        return self._block_factory(
            approval.tool_name,
            tool_content,
        )
