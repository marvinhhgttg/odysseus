from __future__ import annotations

from typing import Any


class DriveOrganizationPlanner:
    async def start_scan(
        self,
        *,
        owner_id: str,
        integration_id: str,
        policy_id: str | None,
        scope: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "scan_run_id": "scan_demo_001",
            "status": "queued",
        }

    async def build_plan(
        self,
        *,
        owner_id: str,
        integration_id: str,
        scan_run_id: str,
        policy_id: str | None,
    ) -> dict[str, Any]:
        return {
            "plan_id": "plan_demo_001",
            "status": "ready",
            "summary": {
                "total_files": 42,
                "proposed_actions": 8,
                "duplicates": 1,
                "uncertain": 2,
            },
        }

    async def get_plan(
        self,
        *,
        owner_id: str,
        plan_id: str,
    ) -> dict[str, Any]:
        return {
            "plan_id": plan_id,
            "status": "ready",
            "summary": {
                "total_files": 42,
                "proposed_actions": 8,
                "duplicates": 1,
                "uncertain": 2,
            },
        }

    async def list_actions(
        self,
        *,
        owner_id: str,
        plan_id: str,
    ) -> list[dict[str, Any]]:
        return [
            {
                "id": "act_001",
                "remote_file_id": "gdrv_001",
                "action_type": "move_file",
                "risk_level": "low",
                "confidence": 0.98,
                "reason": "Invoice heuristics matched",
                "before": {"name": "Scan_1234.pdf", "parents": ["root"]},
                "proposed": {"target_folder": "Finanzen/2026/Rechnungen"},
                "status": "proposed",
            }
        ]
