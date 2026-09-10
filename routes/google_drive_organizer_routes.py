from fastapi import APIRouter, HTTPException, Request
from src.auth_helpers import require_user
from pydantic import BaseModel, Field
from typing import Optional, Literal, List, Dict, Any


class DriveScanRequest(BaseModel):
    integration_id: str = Field(..., min_length=1)
    policy_id: Optional[str] = None
    scope: Dict[str, Any]


class DrivePlanRequest(BaseModel):
    integration_id: str = Field(..., min_length=1)
    scan_run_id: str = Field(..., min_length=1)
    policy_id: Optional[str] = None


class DriveApproveRequest(BaseModel):
    selection_mode: Literal["all", "subset"] = "all"
    action_ids: List[str] = Field(default_factory=list)


class DriveApplyRequest(BaseModel):
    approval_id: str = Field(..., min_length=1)
    expected_fingerprint: str = Field(..., min_length=1)


def _owner_id_from_request(request: Request) -> str:
    return require_user(request)


def setup_google_drive_organizer_routes(drive_organizer_service) -> APIRouter:
    router = APIRouter(prefix="/api/google-drive-organizer", tags=["google-drive-organizer"])

    @router.post("/scan")
    async def start_scan(payload: DriveScanRequest, request: Request):
        owner_id = _owner_id_from_request(request)
        if not owner_id:
            raise HTTPException(status_code=401, detail="Authentication required")
        return await drive_organizer_service.start_scan(
            owner_id=owner_id,
            integration_id=payload.integration_id,
            policy_id=payload.policy_id,
            scope=payload.scope,
        )

    @router.post("/plans")
    async def build_plan(payload: DrivePlanRequest, request: Request):
        owner_id = _owner_id_from_request(request)
        if not owner_id:
            raise HTTPException(status_code=401, detail="Authentication required")
        return drive_organizer_service.build_plan(
            owner_id=owner_id,
            integration_id=payload.integration_id,
            scan_run_id=payload.scan_run_id,
            policy_id=payload.policy_id,
        )

    @router.get("/plans/{plan_id}")
    async def get_plan(plan_id: str, request: Request):
        owner_id = _owner_id_from_request(request)
        if not owner_id:
            raise HTTPException(status_code=401, detail="Authentication required")
        return drive_organizer_service.get_plan(owner_id=owner_id, plan_id=plan_id)

    @router.get("/plans/{plan_id}/actions")
    async def list_actions(plan_id: str, request: Request):
        owner_id = _owner_id_from_request(request)
        if not owner_id:
            raise HTTPException(status_code=401, detail="Authentication required")
        return drive_organizer_service.list_actions(owner_id=owner_id, plan_id=plan_id)

    @router.post("/plans/{plan_id}/approve-request")
    async def create_approval(plan_id: str, payload: DriveApproveRequest, request: Request):
        owner_id = _owner_id_from_request(request)
        if not owner_id:
            raise HTTPException(status_code=401, detail="Authentication required")
        return drive_organizer_service.create_approval_request(
            owner_id=owner_id,
            plan_id=plan_id,
            selection_mode=payload.selection_mode,
            action_ids=payload.action_ids,
        )

    @router.post("/plans/{plan_id}/apply")
    async def apply_plan(plan_id: str, payload: DriveApplyRequest, request: Request):
        owner_id = _owner_id_from_request(request)
        if not owner_id:
            raise HTTPException(status_code=401, detail="Authentication required")
        return drive_organizer_service.apply_plan(
            owner_id=owner_id,
            plan_id=plan_id,
            approval_id=payload.approval_id,
            expected_fingerprint=payload.expected_fingerprint,
        )

    @router.post("/approvals/{approval_id}/approve")
    async def approve_request(approval_id: str, request: Request):
        owner_id = _owner_id_from_request(request)
        return drive_organizer_service.approve_request(
            owner_id=owner_id,
            approval_id=approval_id,
        )

    return router
