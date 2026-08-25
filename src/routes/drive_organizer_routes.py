from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from src.models.drive_organizer import (
    DriveScanRequest,
    DriveScanResponse,
    DrivePlanCreateRequest,
    DrivePlanResponse,
    DriveApproveRequest,
    DriveApproveResponse,
    DriveApplyRequest,
    DriveApplyResponse,
    DrivePlannedActionModel,
)
from src.services.drive_organization_planner import DriveOrganizationPlanner
from src.services.drive_mutation_executor import DriveMutationExecutor

router = APIRouter(prefix="/api/drive-organizer", tags=["drive-organizer"])


def require_authenticated_owner() -> str:
    owner_id = "demo-owner"
    if not owner_id:
        raise HTTPException(status_code=401, detail="Authentication required")
    return owner_id


def get_planner() -> DriveOrganizationPlanner:
    return DriveOrganizationPlanner()


def get_mutation_executor() -> DriveMutationExecutor:
    return DriveMutationExecutor()


@router.post("/scan", response_model=DriveScanResponse)
async def start_scan(
    payload: DriveScanRequest,
    owner_id: str = Depends(require_authenticated_owner),
    planner: DriveOrganizationPlanner = Depends(get_planner),
):
    result = await planner.start_scan(
        owner_id=owner_id,
        integration_id=payload.integration_id,
        policy_id=payload.policy_id,
        scope=payload.scope.model_dump(),
    )
    return DriveScanResponse(**result)


@router.post("/plans", response_model=DrivePlanResponse)
async def create_plan(
    payload: DrivePlanCreateRequest,
    owner_id: str = Depends(require_authenticated_owner),
    planner: DriveOrganizationPlanner = Depends(get_planner),
):
    result = await planner.build_plan(
        owner_id=owner_id,
        integration_id=payload.integration_id,
        scan_run_id=payload.scan_run_id,
        policy_id=payload.policy_id,
    )
    return DrivePlanResponse(**result)


@router.get("/plans/{plan_id}", response_model=DrivePlanResponse)
async def get_plan(
    plan_id: str,
    owner_id: str = Depends(require_authenticated_owner),
    planner: DriveOrganizationPlanner = Depends(get_planner),
):
    result = await planner.get_plan(owner_id=owner_id, plan_id=plan_id)
    return DrivePlanResponse(**result)


@router.get("/plans/{plan_id}/actions", response_model=list[DrivePlannedActionModel])
async def list_actions(
    plan_id: str,
    owner_id: str = Depends(require_authenticated_owner),
    planner: DriveOrganizationPlanner = Depends(get_planner),
):
    result = await planner.list_actions(owner_id=owner_id, plan_id=plan_id)
    return [DrivePlannedActionModel(**row) for row in result]


@router.post("/plans/{plan_id}/approve-request", response_model=DriveApproveResponse)
async def approve_request(
    plan_id: str,
    payload: DriveApproveRequest,
    owner_id: str = Depends(require_authenticated_owner),
    executor: DriveMutationExecutor = Depends(get_mutation_executor),
):
    result = await executor.create_approval_request(
        owner_id=owner_id,
        plan_id=plan_id,
        selection_mode=payload.selection_mode,
        action_ids=payload.action_ids,
    )
    return DriveApproveResponse(**result)


@router.post("/plans/{plan_id}/apply", response_model=DriveApplyResponse)
async def apply_plan(
    plan_id: str,
    payload: DriveApplyRequest,
    owner_id: str = Depends(require_authenticated_owner),
    executor: DriveMutationExecutor = Depends(get_mutation_executor),
):
    result = await executor.apply_plan(
        owner_id=owner_id,
        plan_id=plan_id,
        approval_id=payload.approval_id,
        expected_fingerprint=payload.expected_fingerprint,
    )
    return DriveApplyResponse(**result)
