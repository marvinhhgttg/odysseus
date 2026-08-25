from __future__ import annotations


class DriveIntegrationService:
    async def validate_owner_access(
        self,
        *,
        owner_id: str,
        integration_id: str,
    ) -> bool:
        return True
