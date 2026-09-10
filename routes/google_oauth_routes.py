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


@router.post("/connect")
async def google_drive_connect(payload: dict = Body(default={})):
    integration_id = payload.get("integration_id")
    mode = payload.get("mode", "metadata_readonly")
    owner_id = "local-user"
    return begin_connect(owner_id=owner_id, integration_id=integration_id, mode=mode)


@router.get("/callback")
async def google_drive_callback(
    code: str | None = Query(default=None),
    state: str | None = Query(default=None),
    error: str | None = Query(default=None),
):
    if error:
        raise HTTPException(400, f"Google authorization denied: {error}")
    if not code or not state:
        raise HTTPException(400, "Missing Google OAuth code or state")

    result = await handle_callback(code=code, state=state)
    target = f"/integrations?google_drive_connected=1&integration_id={result['integration_id']}"
    return RedirectResponse(url=target, status_code=303)


@router.get("/{integration_id}/oauth-status")
async def google_drive_oauth_status(integration_id: str):
    integration = get_integration(integration_id)
    if not integration:
        raise HTTPException(404, "Integration not found")
    return {
        "connected": bool(
            integration.get("oauth_access_token")
            or integration.get("settings", {}).get("access_token")
        ),
        "provider": "google_drive",
        "connected_email": integration.get("oauth_connected_email", ""),
        "scope": integration.get("oauth_scope", ""),
        "expires_at": integration.get("oauth_expires_at"),
        "has_refresh_token": bool(
            integration.get("oauth_refresh_token")
            or integration.get("settings", {}).get("refresh_token")
        ),
    }


@router.post("/{integration_id}/refresh-token")
async def google_drive_refresh_token(
    integration_id: str,
    force: bool = Query(default=False),
):
    integration = get_integration(integration_id)
    if not integration:
        raise HTTPException(404, "Integration not found")

    before = integration.get("oauth_expires_at")
    updated = await ensure_fresh_google_token(
        integration,
        force_refresh=force,
    )
    after = updated.get("oauth_expires_at")

    return {
        "ok": True,
        "integration_id": integration_id,
        "expires_at": after,
        "refreshed": before != after,
        "forced": force,
    }


@router.post("/{integration_id}/disconnect")
async def google_drive_disconnect(integration_id: str):
    integration = get_integration(integration_id)
    if not integration:
        raise HTTPException(404, "Integration not found")

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
