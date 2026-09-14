from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException, Query
from fastapi.responses import RedirectResponse

from src.integrations import get_integration, update_integration
from src.services.google_oauth_service import (
    begin_connect,
    ensure_fresh_google_token,
    handle_callback,
)

router = APIRouter(prefix="/api/auth/integrations/google-drive", tags=["google-oauth"])
google_tasks_router = APIRouter(prefix="/api/auth/integrations/google-tasks", tags=["google-oauth"])
google_calendar_router = APIRouter(prefix="/api/auth/integrations/google-calendar", tags=["google-oauth"])


@router.post("/connect")
async def google_drive_connect(payload: dict = Body(default={})):
    integration_id = payload.get("integration_id")
    mode = payload.get("mode", "metadata_readonly")
    return _begin_google_connect(integration_id=integration_id, mode=mode)


@router.get("/callback")
async def google_drive_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
):
    return await _google_oauth_callback(code=code, state=state, error=error)


def _begin_google_connect(*, integration_id, mode):
    owner_id = "local-user"
    return begin_connect(owner_id=owner_id, integration_id=integration_id, mode=mode)


async def _google_oauth_callback(*, code, state, error):
    if error:
        raise HTTPException(400, f"Google authorization denied: {error}")
    if not code or not state:
        raise HTTPException(400, "Missing Google OAuth code or state")

    result = await handle_callback(code=code, state=state)
    provider = result.get("provider") or "google_drive"
    target = f"/integrations?{provider}_connected=1&integration_id={result['integration_id']}"
    return RedirectResponse(url=target, status_code=303)


def _require_oauth_integration(integration_id: str):
    integration = get_integration(integration_id)
    if not integration:
        raise HTTPException(404, "Integration not found")
    return integration


def _oauth_status_dict(integration: dict) -> dict:
    return {
        "connected": bool(
            integration.get("oauth_access_token")
            or integration.get("settings", {}).get("access_token")
        ),
        "provider": integration.get("provider") or "google_drive",
        "connected_email": integration.get("oauth_connected_email", ""),
        "scope": integration.get("oauth_scope", ""),
        "expires_at": integration.get("oauth_expires_at"),
        "has_refresh_token": bool(
            integration.get("oauth_refresh_token")
            or integration.get("settings", {}).get("refresh_token")
        ),
    }


async def _oauth_refresh(integration: dict, force: bool) -> dict:
    before = integration.get("oauth_expires_at")
    updated = await ensure_fresh_google_token(integration, force_refresh=force)
    after = updated.get("oauth_expires_at")
    return {
        "ok": True,
        "integration_id": integration.get("id"),
        "expires_at": after,
        "refreshed": before != after,
        "forced": force,
    }


def _oauth_disconnect(integration_id: str) -> dict:
    patch = {
        "api_key": "",
        "oauth_access_token": "",
        "oauth_refresh_token": "",
        "oauth_scope": "",
        "oauth_expires_at": None,
        "oauth_connected_email": "",
        "oauth_connected_subject": "",
        "settings": {
            "access_token": "",
            "refresh_token": "",
            "scope": "",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_uri": "https://accounts.google.com/o/oauth2/v2/auth",
            "drive_mode": "metadata_readonly",
        },
    }
    updated = update_integration(integration_id, patch)
    if not updated:
        raise HTTPException(404, "Integration not found during disconnect")
    return {"ok": True, "integration_id": integration_id}


@router.get("/{integration_id}/oauth-status")
async def google_drive_oauth_status(integration_id: str):
    return _oauth_status_dict(_require_oauth_integration(integration_id))


@router.post("/{integration_id}/refresh-token")
async def google_drive_refresh_token(
    integration_id: str,
    force: bool = Query(default=False),
):
    return await _oauth_refresh(_require_oauth_integration(integration_id), force=force)


@router.post("/{integration_id}/disconnect")
async def google_drive_disconnect(integration_id: str):
    _require_oauth_integration(integration_id)
    return _oauth_disconnect(integration_id)


@google_tasks_router.post("/connect")
async def google_tasks_connect(payload: dict = Body(default={})):
    return _begin_google_connect(
        integration_id=payload.get("integration_id"),
        mode=payload.get("mode", "tasks"),
    )


@google_tasks_router.get("/callback")
async def google_tasks_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
):
    return await _google_oauth_callback(code=code, state=state, error=error)


@google_tasks_router.get("/{integration_id}/oauth-status")
async def google_tasks_oauth_status(integration_id: str):
    return _oauth_status_dict(_require_oauth_integration(integration_id))


@google_tasks_router.post("/{integration_id}/refresh-token")
async def google_tasks_refresh_token(
    integration_id: str,
    force: bool = Query(default=False),
):
    return await _oauth_refresh(_require_oauth_integration(integration_id), force=force)


@google_tasks_router.post("/{integration_id}/disconnect")
async def google_tasks_disconnect(integration_id: str):
    _require_oauth_integration(integration_id)
    return _oauth_disconnect(integration_id)


@google_calendar_router.post("/connect")
async def google_calendar_connect(payload: dict = Body(default={})):
    return _begin_google_connect(
        integration_id=payload.get("integration_id"),
        mode=payload.get("mode", "calendar"),
    )


@google_calendar_router.get("/callback")
async def google_calendar_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
):
    return await _google_oauth_callback(code=code, state=state, error=error)


@google_calendar_router.get("/{integration_id}/oauth-status")
async def google_calendar_oauth_status(integration_id: str):
    return _oauth_status_dict(_require_oauth_integration(integration_id))


@google_calendar_router.post("/{integration_id}/refresh-token")
async def google_calendar_refresh_token(
    integration_id: str,
    force: bool = Query(default=False),
):
    return await _oauth_refresh(_require_oauth_integration(integration_id), force=force)


@google_calendar_router.post("/{integration_id}/disconnect")
async def google_calendar_disconnect(integration_id: str):
    _require_oauth_integration(integration_id)
    return _oauth_disconnect(integration_id)
