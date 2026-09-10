"""
Google Drive Organizer agent tool.

Read-only / plan-only bridge from the chat-agent tool layer to Odysseus'
existing Google Drive Organizer API. It intentionally does NOT expose
approve, apply, move, rename, delete, or any other write action.
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

import httpx

from src.tools._common import _INTERNAL_BASE, _internal_headers, _parse_tool_args

# Agent bridge is deliberately read-only / plan-only. Actual Drive mutations
# remain available only through the dedicated organizer UI/API approval flow.
_ALLOWED_ACTIONS = {"scan", "plan", "get_plan", "list_actions"}


def _error(message: str) -> Dict[str, Any]:
    return {"error": message, "exit_code": 1}


def _compact_scan(data: Dict[str, Any]) -> Dict[str, Any]:
    preview = data.get("preview_files") or data.get("previewFiles") or []
    return {
        "scan_run_id": data.get("scan_run_id") or data.get("scanRunId"),
        "status": data.get("status"),
        "file_count_scanned": data.get("file_count_scanned") or data.get("fileCountScanned"),
        "file_count_indexed": data.get("file_count_indexed") or data.get("fileCountIndexed"),
        "next_page_token": data.get("next_page_token") or data.get("nextPageToken"),
        "error_summary": data.get("error_summary") or data.get("errorSummary"),
        "preview_files": preview[:20],
    }


def _compact_plan(data: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "plan_id": data.get("plan_id") or data.get("planId") or data.get("id"),
        "status": data.get("status"),
        "scan_run_id": data.get("scan_run_id") or data.get("scanRunId"),
        "action_count": data.get("action_count") or data.get("actionCount"),
        "summary": data.get("summary"),
        "created_at": data.get("created_at") or data.get("createdAt"),
    }


async def _request(
    method: str,
    path: str,
    *,
    owner: Optional[str],
    payload: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    url = f"{_INTERNAL_BASE.rstrip('/')}{path}"
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            response = await client.request(
                method=method,
                url=url,
                headers=_internal_headers(owner),
                json=payload,
            )
    except httpx.HTTPError as exc:
        return _error(f"Google Drive Organizer is unreachable: {exc}")

    try:
        body = response.json()
    except ValueError:
        body = {"detail": response.text[:1000]}

    if response.status_code >= 400:
        detail = body.get("detail") if isinstance(body, dict) else body
        return _error(f"Google Drive Organizer returned HTTP {response.status_code}: {detail}")

    if not isinstance(body, dict):
        return _error("Google Drive Organizer returned an unexpected response.")
    return body


async def do_manage_google_drive(content: str, owner: Optional[str] = None) -> Dict[str, Any]:
    """
    Handle the manage_google_drive agent tool.

    Allowed actions:
    - scan: Inventory a page of Google Drive files.
    - plan: Build a proposed organization plan from a completed scan.
    - get_plan: Retrieve plan metadata / summary.
    - list_actions: Retrieve proposed actions for a plan.

    This bridge deliberately cannot approve or apply a plan. Real Drive
    mutations stay in the dedicated organizer UI/API approval workflow.
    """
    try:
        args = _parse_tool_args(content)
    except ValueError:
        return _error("Invalid JSON arguments.")

    raw_action = str(args.get("action") or "").replace("-", "_").strip().lower()
    aliases = {
        "inventory": "scan",
        "analyze": "scan",
        "analyse": "scan",
        "create_plan": "plan",
        "build_plan": "plan",
        "show_plan": "get_plan",
        "actions": "list_actions",
    }
    action = aliases.get(raw_action, raw_action)

    if action not in _ALLOWED_ACTIONS:
        allowed = ", ".join(sorted(_ALLOWED_ACTIONS))
        return _error(
            f"Unsupported action '{raw_action}'. Allowed actions: {allowed}."
        )


    if action == "scan":
        integration_id = str(args.get("integration_id") or "").strip()
        if not integration_id:
            return _error("scan requires integration_id.")

        scope = args.get("scope")
        if not isinstance(scope, dict):
            scope = {}

        mode = str(scope.get("mode") or args.get("mode") or "my_drive").strip()
        if mode not in {"my_drive", "folder"}:
            return _error("scope.mode must be 'my_drive' or 'folder'.")

        folder_id = str(scope.get("folder_id") or args.get("folder_id") or "").strip()
        if mode == "folder" and not folder_id:
            return _error("folder mode requires scope.folder_id.")

        try:
            max_files = int(scope.get("max_files") or args.get("max_files") or 25)
        except (TypeError, ValueError):
            return _error("max_files must be a number.")
        max_files = max(1, min(max_files, 100))

        page_token = str(scope.get("page_token") or args.get("page_token") or "").strip()

        request_scope: Dict[str, Any] = {
            "mode": mode,
            "max_files": max_files,
        }
        if folder_id:
            request_scope["folder_id"] = folder_id
        if page_token:
            request_scope["page_token"] = page_token

        data = await _request(
            "POST",
            "/api/google-drive-organizer/scan",
            owner=owner,
            payload={
                "integration_id": integration_id,
                "scope": request_scope,
            },
        )
        if "exit_code" in data:
            return data

        compact = _compact_scan(data)
        return {
            "results": json.dumps(compact, ensure_ascii=False, indent=2),
            "data": compact,
            "exit_code": 0,
        }

    if action == "plan":
        integration_id = str(args.get("integration_id") or "").strip()
        if not integration_id:
            return _error("plan requires integration_id.")

        scan_run_id = str(args.get("scan_run_id") or args.get("scan_id") or "").strip()
        if not scan_run_id:
            return _error("plan requires scan_run_id.")

        payload: Dict[str, Any] = {
            "integration_id": integration_id,
            "scan_run_id": scan_run_id,
        }

        # Optional policy/intent fields are passed through only when provided.
        for field in ("policy_id", "instructions", "strategy"):
            value = args.get(field)
            if value not in (None, ""):
                payload[field] = value

        data = await _request(
            "POST",
            "/api/google-drive-organizer/plans",
            owner=owner,
            payload=payload,
        )
        if "exit_code" in data:
            return data

        compact = _compact_plan(data)
        return {
            "results": json.dumps(compact, ensure_ascii=False, indent=2),
            "data": compact,
            "exit_code": 0,
        }

    plan_id = str(args.get("plan_id") or "").strip()
    if not plan_id:
        return _error(f"{action} requires plan_id.")

    if action == "get_plan":
        data = await _request(
            "GET",
            f"/api/google-drive-organizer/plans/{plan_id}",
            owner=owner,
        )
        if "exit_code" in data:
            return data

        compact = _compact_plan(data)
        compact["raw_plan"] = data
        return {
            "results": json.dumps(compact, ensure_ascii=False, indent=2),
            "data": compact,
            "exit_code": 0,
        }

    # action == "list_actions"
    data = await _request(
        "GET",
        f"/api/google-drive-organizer/plans/{plan_id}/actions",
        owner=owner,
    )
    if "exit_code" in data:
        return data

    actions = data.get("actions", data.get("items", data))
    if isinstance(actions, list):
        compact = {
            "plan_id": plan_id,
            "action_count": len(actions),
            "actions": actions[:100],
            "truncated": len(actions) > 100,
        }
    else:
        compact = {"plan_id": plan_id, "actions": actions}

    return {
        "results": json.dumps(compact, ensure_ascii=False, indent=2),
        "data": compact,
        "exit_code": 0,
    }
