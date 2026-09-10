import hashlib
import json
import uuid
from collections import Counter
from datetime import timedelta
from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from src.services.google_oauth_service import ensure_fresh_google_token
from src.drive.google_drive_client import GoogleDriveClient

from core.database import (
    GoogleDriveOrganizationAction,
    GoogleDriveOrganizationApproval,
    GoogleDriveOrganizationPlan,
    GoogleDriveOrganizerScanRun,
    SessionLocal,
    utcnow_naive,
)

APPROVAL_TTL_MINUTES = 15
TOOL_NAME = "google_drive_apply_plan"


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def _fingerprint(
    *,
    owner: str,
    plan_id: str,
    selection_mode: str,
    action_ids: List[str],
) -> str:
    payload = {
        "owner": owner,
        "plan_id": plan_id,
        "tool_name": TOOL_NAME,
        "selection_mode": selection_mode,
        "action_ids": sorted(action_ids),
    }
    return hashlib.sha256(
        _canonical_json(payload).encode("utf-8")
    ).hexdigest()


def _dt(value):
    return f"{value.isoformat()}Z" if value else None


def _scan_to_dict(run: GoogleDriveOrganizerScanRun) -> Dict[str, Any]:
    return {
        "scan_run_id": run.id,
        "owner_id": run.owner,
        "integration_id": run.integration_id,
        "policy_id": run.policy_id,
        "status": run.status,
        "scope": run.scope or {},
        "file_count_scanned": run.file_count_scanned,
        "file_count_indexed": run.file_count_indexed,
        "error_summary": run.error_summary,
        "started_at": _dt(run.started_at),
        "completed_at": _dt(run.completed_at),
        "created_at": _dt(run.created_at),
    }


def _plan_to_dict(plan: GoogleDriveOrganizationPlan, actions_count: int = 0) -> Dict[str, Any]:
    return {
        "plan_id": plan.id,
        "owner_id": plan.owner,
        "integration_id": plan.integration_id,
        "policy_id": plan.policy_id,
        "scan_run_id": plan.scan_run_id,
        "status": plan.status,
        "summary": plan.summary or {},
        "risk_summary": plan.risk_summary or {},
        "expires_at": _dt(plan.expires_at),
        "actions_count": actions_count,
        "created_at": _dt(plan.created_at),
        "updated_at": _dt(plan.updated_at),
    }


def _action_to_dict(action: GoogleDriveOrganizationAction) -> Dict[str, Any]:
    return {
        "action_id": action.id,
        "plan_id": action.plan_id,
        "remote_file_id": action.remote_file_id,
        "action_type": action.action_type,
        "risk_level": action.risk_level,
        "confidence": action.confidence,
        "reason": action.reason,
        "before": action.before or {},
        "proposed": action.proposed or {},
        "status": action.status,
        "requires_approval": action.requires_approval,
        "approval_fingerprint": action.approval_fingerprint,
        "execution_result": action.execution_result,
        "created_at": _dt(action.created_at),
        "updated_at": _dt(action.updated_at),
    }


def _approval_to_dict(approval: GoogleDriveOrganizationApproval) -> Dict[str, Any]:
    return {
        "approval_id": approval.id,
        "plan_id": approval.plan_id,
        "tool_name": approval.tool_name,
        "selection_mode": approval.selection_mode,
        "action_ids": approval.action_ids or [],
        "fingerprint": approval.invocation_fingerprint,
        "request_preview": approval.request_preview or {},
        "status": approval.status,
        "expires_at": _dt(approval.expires_at),
        "decided_at": _dt(approval.decided_at),
        "consumed_at": _dt(approval.consumed_at),
    }


def _classify_preview_file(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    name = (item.get("name") or "").strip()
    mime = (item.get("mimeType") or "").strip()
    lower = name.lower()

    if not name:
        return None

    if lower.endswith(".pdf") and "invoice" in lower:
        return {
            "action_type": "move_file",
            "risk_level": "low",
            "reason": "Filename suggests invoice PDF",
            "proposed": {"target_folder_hint": "Finanzen/Rechnungen"},
        }

    if "screenshot" in lower or lower.startswith("img_"):
        return {
            "action_type": "move_file",
            "risk_level": "low",
            "reason": "Filename suggests screenshot/image import",
            "proposed": {"target_folder_hint": "Screenshots"},
        }

    if mime == "application/pdf":
        return {
            "action_type": "noop_review",
            "risk_level": "low",
            "reason": "PDF detected but category unclear",
            "proposed": {"review_bucket": "Unklar"},
        }

    return None


class GoogleDriveOrganizerService:
    def __init__(self, approval_service=None, integrations_store=None):
        self.approval_service = approval_service
        self.integrations_store = integrations_store

    def _require_google_drive_integration(self, owner_id: str, integration_id: str) -> Dict[str, Any]:
        if not self.integrations_store:
            raise HTTPException(status_code=503, detail="Integrations store unavailable")

        integration = None
        getter_names = (
            "get_integration",
            "get",
            "fetch_integration",
            "get_by_id",
        )
        for name in getter_names:
            getter = getattr(self.integrations_store, name, None)
            if callable(getter):
                try:
                    integration = getter(owner_id=owner_id, integration_id=integration_id)
                except TypeError:
                    try:
                        integration = getter(owner_id, integration_id)
                    except TypeError:
                        try:
                            integration = getter(integration_id)
                        except Exception:
                            integration = None
                except Exception:
                    integration = None
                if integration:
                    break

        if not integration:
            raise HTTPException(status_code=404, detail="Integration not found")

        provider = integration.get("provider") if isinstance(integration, dict) else getattr(integration, "provider", None)
        if provider != "google_drive":
            raise HTTPException(status_code=404, detail="Integration not found")

        return integration

    async def start_scan(
        self,
        owner_id: str,
        integration_id: str,
        policy_id: Optional[str],
        scope: Dict[str, Any],
    ) -> Dict[str, Any]:
        integration = self._require_google_drive_integration(owner_id, integration_id)
        refreshed = await ensure_fresh_google_token(integration, force_refresh=False)
        integration = refreshed or integration
        client = GoogleDriveClient.from_integration(integration)

        now = utcnow_naive()
        db = SessionLocal()
        try:
            q = None
            mode = (scope or {}).get("mode")
            folder_id = (scope or {}).get("folder_id")
            page_token = (scope or {}).get("page_token")
            max_files = int((scope or {}).get("max_files") or 100)
            if mode == "folder" and folder_id:
                q = f"'{folder_id}' in parents and trashed = false"
            else:
                q = "trashed = false"

            data = client.list_files(page_size=max_files, page_token=page_token, q=q)
            files = data.get("files", []) or []

            run = GoogleDriveOrganizerScanRun(
                id=_new_id("scan"),
                owner=owner_id,
                integration_id=integration_id,
                policy_id=policy_id,
                status="completed",
                scope=scope or {},
                file_count_scanned=len(files),
                file_count_indexed=len(files),
                started_at=now,
                completed_at=utcnow_naive(),
            )
            db.add(run)
            db.commit()
            db.refresh(run)

            run.scope = dict(scope or {})
            run.scope["_preview_files"] = files[:25]
            db.commit()
            db.refresh(run)

            result = _scan_to_dict(run)
            result["preview_files"] = files[:10]
            result["next_page_token"] = data.get("nextPageToken")
            return result
        finally:
            db.close()

    def build_plan(
        self,
        owner_id: str,
        integration_id: str,
        scan_run_id: str,
        policy_id: Optional[str],
    ) -> Dict[str, Any]:
        self._require_google_drive_integration(owner_id, integration_id)
        db = SessionLocal()
        try:
            scan = (
                db.query(GoogleDriveOrganizerScanRun)
                .filter(
                    GoogleDriveOrganizerScanRun.id == scan_run_id,
                    GoogleDriveOrganizerScanRun.owner == owner_id,
                )
                .first()
            )
            if not scan:
                raise HTTPException(status_code=404, detail="Scan run not found")

            if scan.integration_id != integration_id:
                raise HTTPException(status_code=404, detail="Scan run not found")

            summary = {
                "total_files": int(scan.file_count_scanned or 0),
                "proposed_actions": 0,
                "duplicates": 0,
                "uncertain": 0,
            }
            risk_summary = {"low": 0, "medium": 0, "high": 0}

            plan = GoogleDriveOrganizationPlan(
                id=_new_id("plan"),
                owner=owner_id,
                integration_id=integration_id,
                policy_id=policy_id,
                scan_run_id=scan_run_id,
                status="ready",
                summary=summary,
                risk_summary=risk_summary,
            )
            db.add(plan)
            db.flush()

            preview_files = []
            if isinstance(scan.scope, dict):
                preview_files = scan.scope.get("_preview_files", []) or []

            created = 0
            uncertain = 0
            for item in preview_files:
                proposal = _classify_preview_file(item)
                if not proposal:
                    continue

                action = GoogleDriveOrganizationAction(
                    id=_new_id("action"),
                    owner=owner_id,
                    plan_id=plan.id,
                    remote_file_id=item.get("id"),
                    action_type=proposal["action_type"],
                    risk_level=proposal["risk_level"],
                    confidence=90 if proposal["risk_level"] == "low" else 75,
                    reason=proposal["reason"],
                    before={
                        "name": item.get("name"),
                        "mimeType": item.get("mimeType"),
                        "parents": item.get("parents") or [],
                    },
                    proposed=proposal["proposed"],
                    status="proposed",
                    requires_approval=True,
                )
                db.add(action)
                created += 1
                risk_summary[proposal["risk_level"]] += 1
                if proposal["action_type"] == "noop_review":
                    uncertain += 1

            summary["proposed_actions"] = created
            summary["uncertain"] = uncertain
            plan.summary = summary
            plan.risk_summary = risk_summary

            db.commit()
            db.refresh(plan)
            return _plan_to_dict(plan, actions_count=created)
        finally:
            db.close()

    def get_plan(self, owner_id: str, plan_id: str) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            plan = (
                db.query(GoogleDriveOrganizationPlan)
                .filter(
                    GoogleDriveOrganizationPlan.id == plan_id,
                    GoogleDriveOrganizationPlan.owner == owner_id,
                )
                .first()
            )
            if not plan:
                raise HTTPException(status_code=404, detail="Plan not found")

            actions_count = (
                db.query(GoogleDriveOrganizationAction)
                .filter(
                    GoogleDriveOrganizationAction.plan_id == plan.id,
                    GoogleDriveOrganizationAction.owner == owner_id,
                )
                .count()
            )
            return _plan_to_dict(plan, actions_count=actions_count)
        finally:
            db.close()

    def list_actions(self, owner_id: str, plan_id: str) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            plan = (
                db.query(GoogleDriveOrganizationPlan)
                .filter(
                    GoogleDriveOrganizationPlan.id == plan_id,
                    GoogleDriveOrganizationPlan.owner == owner_id,
                )
                .first()
            )
            if not plan:
                raise HTTPException(status_code=404, detail="Plan not found")

            actions = (
                db.query(GoogleDriveOrganizationAction)
                .filter(
                    GoogleDriveOrganizationAction.plan_id == plan_id,
                    GoogleDriveOrganizationAction.owner == owner_id,
                )
                .order_by(
                    GoogleDriveOrganizationAction.created_at.asc(),
                    GoogleDriveOrganizationAction.id.asc(),
                )
                .all()
            )
            return {
                "plan_id": plan_id,
                "actions": [_action_to_dict(action) for action in actions],
            }
        finally:
            db.close()

    def create_approval_request(
        self,
        owner_id: str,
        plan_id: str,
        selection_mode: str,
        action_ids: List[str],
    ) -> Dict[str, Any]:
        if selection_mode not in {"all", "subset"}:
            raise HTTPException(status_code=400, detail="Invalid selection mode")

        db = SessionLocal()
        try:
            plan = (
                db.query(GoogleDriveOrganizationPlan)
                .filter(
                    GoogleDriveOrganizationPlan.id == plan_id,
                    GoogleDriveOrganizationPlan.owner == owner_id,
                )
                .first()
            )
            if not plan:
                raise HTTPException(status_code=404, detail="Plan not found")

            proposed_query = db.query(GoogleDriveOrganizationAction).filter(
                GoogleDriveOrganizationAction.plan_id == plan_id,
                GoogleDriveOrganizationAction.owner == owner_id,
                GoogleDriveOrganizationAction.status == "proposed",
            )

            if selection_mode == "all":
                selected_actions = proposed_query.order_by(
                    GoogleDriveOrganizationAction.created_at.asc(),
                    GoogleDriveOrganizationAction.id.asc(),
                ).all()
            else:
                if not action_ids:
                    raise HTTPException(
                        status_code=400,
                        detail="Action selection cannot be empty",
                    )

                selected_actions = (
                    proposed_query.filter(
                        GoogleDriveOrganizationAction.id.in_(action_ids)
                    )
                    .order_by(
                        GoogleDriveOrganizationAction.created_at.asc(),
                        GoogleDriveOrganizationAction.id.asc(),
                    )
                    .all()
                )
                selected_ids = {action.id for action in selected_actions}
                if selected_ids != set(action_ids):
                    raise HTTPException(status_code=404, detail="Action not found")

            if not selected_actions:
                raise HTTPException(
                    status_code=400,
                    detail="No proposed actions available",
                )

            selected_ids = [action.id for action in selected_actions]
            fingerprint = _fingerprint(
                owner=owner_id,
                plan_id=plan_id,
                selection_mode=selection_mode,
                action_ids=selected_ids,
            )

            types = Counter(action.action_type for action in selected_actions)
            risks = Counter(action.risk_level for action in selected_actions)
            now = utcnow_naive()

            approval = GoogleDriveOrganizationApproval(
                id=_new_id("approval"),
                owner=owner_id,
                plan_id=plan_id,
                tool_name=TOOL_NAME,
                selection_mode=selection_mode,
                action_ids=selected_ids,
                invocation_fingerprint=fingerprint,
                request_preview={
                    "plan_id": plan_id,
                    "action_count": len(selected_actions),
                    "action_types": dict(types),
                    "risk_levels": dict(risks),
                },
                status="pending",
                expires_at=now + timedelta(minutes=APPROVAL_TTL_MINUTES),
            )
            db.add(approval)

            for action in selected_actions:
                action.approval_fingerprint = fingerprint

            db.commit()
            db.refresh(approval)
            return _approval_to_dict(approval)
        finally:
            db.close()

    def approve_request(self, owner_id: str, approval_id: str) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            approval = (
                db.query(GoogleDriveOrganizationApproval)
                .filter(
                    GoogleDriveOrganizationApproval.id == approval_id,
                    GoogleDriveOrganizationApproval.owner == owner_id,
                )
                .first()
            )
            if not approval:
                raise HTTPException(status_code=404, detail="Approval not found")

            now = utcnow_naive()
            if approval.expires_at <= now:
                approval.status = "expired"
                db.commit()
                raise HTTPException(status_code=409, detail="Approval expired")

            if approval.status != "pending":
                raise HTTPException(
                    status_code=409,
                    detail="Approval is not pending",
                )

            actions = (
                db.query(GoogleDriveOrganizationAction)
                .filter(
                    GoogleDriveOrganizationAction.plan_id == approval.plan_id,
                    GoogleDriveOrganizationAction.owner == owner_id,
                    GoogleDriveOrganizationAction.id.in_(approval.action_ids or []),
                    GoogleDriveOrganizationAction.status == "proposed",
                )
                .all()
            )

            if len(actions) != len(approval.action_ids or []):
                raise HTTPException(
                    status_code=409,
                    detail="Approval actions are no longer available",
                )

            approval.status = "approved"
            approval.decided_at = now

            for action in actions:
                action.status = "approved"

            db.commit()
            db.refresh(approval)
            return _approval_to_dict(approval)
        finally:
            db.close()

    def apply_plan(
        self,
        owner_id: str,
        plan_id: str,
        approval_id: str,
        expected_fingerprint: str,
    ) -> Dict[str, Any]:
        db = SessionLocal()
        try:
            approval = (
                db.query(GoogleDriveOrganizationApproval)
                .filter(
                    GoogleDriveOrganizationApproval.id == approval_id,
                    GoogleDriveOrganizationApproval.owner == owner_id,
                    GoogleDriveOrganizationApproval.plan_id == plan_id,
                )
                .first()
            )
            if not approval:
                raise HTTPException(status_code=404, detail="Approval not found")

            if approval.invocation_fingerprint != expected_fingerprint:
                raise HTTPException(
                    status_code=409,
                    detail="Approval fingerprint mismatch",
                )

            now = utcnow_naive()
            if approval.expires_at <= now:
                approval.status = "expired"
                db.commit()
                raise HTTPException(status_code=409, detail="Approval expired")

            if approval.status != "approved":
                raise HTTPException(
                    status_code=409,
                    detail="Approval is not approved",
                )

            plan = (
                db.query(GoogleDriveOrganizationPlan)
                .filter(
                    GoogleDriveOrganizationPlan.id == plan_id,
                    GoogleDriveOrganizationPlan.owner == owner_id,
                )
                .first()
            )
            if not plan:
                raise HTTPException(status_code=404, detail="Plan not found")

            actions = (
                db.query(GoogleDriveOrganizationAction)
                .filter(
                    GoogleDriveOrganizationAction.plan_id == plan_id,
                    GoogleDriveOrganizationAction.owner == owner_id,
                    GoogleDriveOrganizationAction.id.in_(approval.action_ids or []),
                )
                .all()
            )

            if len(actions) != len(approval.action_ids or []):
                raise HTTPException(
                    status_code=409,
                    detail="Approved actions no longer match plan",
                )

            approved_actions = [
                action for action in actions if action.status == "approved"
            ]
            if not approved_actions:
                raise HTTPException(
                    status_code=400,
                    detail="No approved actions available",
                )

            results = []
            for action in approved_actions:
                action.status = "applied"
                action.execution_result = {
                    "mode": "local_stub",
                    "message": "No Google Drive mutation executed in Batch 20",
                }
                results.append(
                    {
                        "action_id": action.id,
                        "status": "applied",
                        "mode": "local_stub",
                    }
                )

            approval.status = "consumed"
            approval.consumed_at = now
            plan.status = "applied"

            db.commit()
            return {
                "plan_id": plan_id,
                "approval_id": approval_id,
                "status": plan.status,
                "applied": len(approved_actions),
                "failed": 0,
                "skipped": 0,
                "results": results,
            }
        finally:
            db.close()
