from __future__ import annotations

from typing import Any

from src.services.google_drive_organizer_service import GoogleDriveOrganizerService


class DriveMutationExecutor:
    def __init__(self) -> None:
        self.service = GoogleDriveOrganizerService()

    async def create_approval_request(
        self,
        *,
        owner_id: str,
        plan_id: str,
        selection_mode: str,
        action_ids: list[str],
    ) -> dict[str, Any]:
        return self.service.create_approval_request(
            owner_id=owner_id,
            plan_id=plan_id,
            selection_mode=selection_mode,
            action_ids=action_ids,
        )

    async def apply_plan(
        self,
        *,
        owner_id: str,
        plan_id: str,
        approval_id: str,
        expected_fingerprint: str,
    ) -> dict[str, Any]:
        return self.service.apply_plan(
            owner_id=owner_id,
            plan_id=plan_id,
            approval_id=approval_id,
            expected_fingerprint=expected_fingerprint,
        )
