from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest

from src.tool_approval import ApprovalError
from src.tool_approval_enforcer import ToolApprovalEnforcer
from src.tool_risk import ToolRisk


NOW = datetime(2026, 8, 5, 9, 0, tzinfo=timezone.utc)


@dataclass(frozen=True)
class FakeBlock:
    tooltype: str
    content: str


class FakeStore:
    def __init__(self):
        self.approvals = {}
        self.contents = {}
        self.create_calls = 0
        self.consume_calls = 0

    def create(self, approval, *, tool_content):
        self.create_calls += 1
        self.approvals[approval.id] = approval
        self.contents[approval.id] = tool_content
        return approval

    def get(self, approval_id, *, owner):
        approval = self.approvals.get(approval_id)
        if approval is None or approval.owner != owner:
            raise ApprovalError("approval is unavailable")
        return approval

    def load_tool_content(self, approval_id, *, owner):
        self.get(approval_id, owner=owner)
        return self.contents[approval_id]

    def approve(self, approval_id, *, owner, now=None):
        approval = self.get(approval_id, owner=owner)
        approval = approval.approve(now=now)
        self.approvals[approval_id] = approval
        return approval

    def consume(
        self,
        approval_id,
        *,
        owner,
        session_id,
        run_id,
        tool_name,
        risk,
        arguments,
        now=None,
    ):
        self.consume_calls += 1
        approval = self.get(approval_id, owner=owner)
        approval = approval.consume(
            owner=owner,
            session_id=session_id,
            run_id=run_id,
            tool_name=tool_name,
            risk=risk,
            arguments=arguments,
            now=now,
        )
        self.approvals[approval_id] = approval
        return approval


@pytest.fixture
def store():
    return FakeStore()


@pytest.fixture
def enforcer(store):
    return ToolApprovalEnforcer(
        store,
        block_factory=FakeBlock,
    )


def test_readonly_block_is_allowed_without_persistence(enforcer, store):
    block = FakeBlock("read_file", "/tmp/example.txt")

    decision = enforcer.check(
        block,
        owner="marc",
        session_id="session-1",
        run_id="run-1",
        now=NOW,
    )

    assert decision.block is block
    assert decision.approval is None
    assert not decision.requires_approval
    assert decision.assessment.risk is ToolRisk.READ_ONLY
    assert store.create_calls == 0


def test_local_write_is_allowed_without_approval(enforcer, store):
    block = FakeBlock("write_file", '{"path":"note.txt","content":"ok"}')

    decision = enforcer.check(
        block,
        owner="marc",
        session_id="session-1",
        run_id="run-1",
        now=NOW,
    )

    assert decision.block is block
    assert decision.assessment.risk is ToolRisk.LOCAL_WRITE
    assert store.create_calls == 0


def test_host_control_creates_redacted_pending_approval(enforcer, store):
    secret_content = "printf 'secret-value\\n'"
    block = FakeBlock("bash", secret_content)

    decision = enforcer.check(
        block,
        owner="marc",
        session_id="session-1",
        run_id="run-1",
        now=NOW,
    )

    assert decision.block is None
    assert decision.requires_approval
    assert decision.assessment.risk is ToolRisk.HOST_CONTROL
    assert decision.approval.tool_name == "bash"
    assert decision.approval.risk == "host_control"
    assert decision.approval.expires_at == NOW + timedelta(minutes=10)
    assert store.create_calls == 1

    public = decision.approval.to_dict()
    assert secret_content not in str(public)
    assert "content" not in public
    assert "arguments" not in public
    assert "fingerprint" not in public
    assert "argumentHash" not in public


def test_external_write_requires_approval(enforcer):
    decision = enforcer.check(
        FakeBlock("send_email", '{"to":"a@example.test"}'),
        owner="marc",
        session_id="session-1",
        run_id="run-1",
        now=NOW,
    )

    assert decision.requires_approval
    assert decision.assessment.risk is ToolRisk.EXTERNAL_WRITE


def test_unknown_native_tool_fails_closed(enforcer):
    decision = enforcer.check(
        FakeBlock("future_power_tool", '{"action":"go"}'),
        owner="marc",
        session_id="session-1",
        run_id="run-1",
        now=NOW,
    )

    assert decision.requires_approval
    assert decision.assessment.risk is ToolRisk.UNKNOWN


def test_mcp_readonly_annotation_bypasses_approval(enforcer, store):
    block = FakeBlock("mcp__server__lookup", '{"id":"42"}')

    decision = enforcer.check(
        block,
        owner="marc",
        session_id="session-1",
        run_id="run-1",
        annotations={"readOnlyHint": True},
        now=NOW,
    )

    assert decision.block is block
    assert decision.assessment.risk is ToolRisk.READ_ONLY
    assert decision.assessment.source == "mcp_read_only_hint"
    assert store.create_calls == 0


def test_mcp_destructive_annotation_requires_approval(enforcer):
    decision = enforcer.check(
        FakeBlock("mcp__server__remove", '{"id":"42"}'),
        owner="marc",
        session_id="session-1",
        run_id="run-1",
        annotations={"destructiveHint": True},
        now=NOW,
    )

    assert decision.requires_approval
    assert decision.assessment.risk is ToolRisk.DESTRUCTIVE


def test_approved_invocation_is_consumed_and_reconstructed(enforcer, store):
    original = FakeBlock("bash", "printf 'ok\\n'")
    decision = enforcer.check(
        original,
        owner="marc",
        session_id="session-1",
        run_id="run-1",
        now=NOW,
    )
    approval_id = decision.approval.approval_id

    store.approve(
        approval_id,
        owner="marc",
        now=NOW + timedelta(seconds=1),
    )

    resumed = enforcer.resume(
        approval_id,
        owner="marc",
        session_id="session-1",
        run_id="run-1",
        now=NOW + timedelta(seconds=2),
    )

    assert resumed == original
    assert store.consume_calls == 1
    assert store.approvals[approval_id].status.value == "consumed"


def test_pending_invocation_cannot_resume(enforcer, store):
    decision = enforcer.check(
        FakeBlock("bash", "printf 'no\\n'"),
        owner="marc",
        session_id="session-1",
        run_id="run-1",
        now=NOW,
    )

    with pytest.raises(ApprovalError, match="cannot consume"):
        enforcer.resume(
            decision.approval.approval_id,
            owner="marc",
            session_id="session-1",
            run_id="run-1",
            now=NOW + timedelta(seconds=1),
        )


def test_resume_rejects_wrong_owner(enforcer, store):
    decision = enforcer.check(
        FakeBlock("bash", "printf 'no\\n'"),
        owner="marc",
        session_id="session-1",
        run_id="run-1",
        now=NOW,
    )
    store.approve(
        decision.approval.approval_id,
        owner="marc",
        now=NOW + timedelta(seconds=1),
    )

    with pytest.raises(ApprovalError, match="unavailable"):
        enforcer.resume(
            decision.approval.approval_id,
            owner="other",
            session_id="session-1",
            run_id="run-1",
            now=NOW + timedelta(seconds=2),
        )


def test_resume_rejects_wrong_run_binding(enforcer, store):
    decision = enforcer.check(
        FakeBlock("bash", "printf 'no\\n'"),
        owner="marc",
        session_id="session-1",
        run_id="run-1",
        now=NOW,
    )
    store.approve(
        decision.approval.approval_id,
        owner="marc",
        now=NOW + timedelta(seconds=1),
    )

    with pytest.raises(ApprovalError, match="binding"):
        enforcer.resume(
            decision.approval.approval_id,
            owner="marc",
            session_id="session-1",
            run_id="different-run",
            now=NOW + timedelta(seconds=2),
        )


def test_consumed_invocation_cannot_resume_twice(enforcer, store):
    decision = enforcer.check(
        FakeBlock("bash", "printf 'once\\n'"),
        owner="marc",
        session_id="session-1",
        run_id="run-1",
        now=NOW,
    )
    approval_id = decision.approval.approval_id
    store.approve(
        approval_id,
        owner="marc",
        now=NOW + timedelta(seconds=1),
    )

    enforcer.resume(
        approval_id,
        owner="marc",
        session_id="session-1",
        run_id="run-1",
        now=NOW + timedelta(seconds=2),
    )

    with pytest.raises(ApprovalError, match="cannot consume"):
        enforcer.resume(
            approval_id,
            owner="marc",
            session_id="session-1",
            run_id="run-1",
            now=NOW + timedelta(seconds=3),
        )


def test_non_positive_ttl_is_rejected(store):
    with pytest.raises(ValueError, match="positive"):
        ToolApprovalEnforcer(
            store,
            block_factory=FakeBlock,
            approval_ttl=timedelta(0),
        )
