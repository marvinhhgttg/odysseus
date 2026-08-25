from __future__ import annotations

from typing import Any


class DriveMutationExecutor:
    async def create_approval_request(
        self,
        *,
        owner_id: str,
        plan_id: str,
        selection_mode: str,
        action_ids: list[str],
    ) -> dict[str, Any]:
        return {
            "approval_id": "apr_demo_001",
            "status": "pending",
            "expires_at": "2026-08-24T22:30:00Z",
            "fingerprint": "sha256:demo",
        }

    async def apply_plan(
        self,
        *,
        owner_id: str,
        plan_id: str,
        approval_id: str,
        expected_fingerprint: str,
    ) -> dict[str, Any]:
        return {
            "plan_id": plan_id,
            "status": "partially_applied",
            "applied": 3,
            "failed": 0,
            "skipped": 0,
            "results": {},
        }
