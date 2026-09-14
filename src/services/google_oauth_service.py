from __future__ import annotations

import base64
import hashlib
import os
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

import httpx
from fastapi import HTTPException

from src.integrations import add_integration, get_integration, update_integration
from src.services import google_oauth_state_store as state_store

GOOGLE_AUTH_URI = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URI = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URI = "https://openidconnect.googleapis.com/v1/userinfo"

SCOPE_MAP = {
    "metadata_readonly": ["https://www.googleapis.com/auth/drive.metadata.readonly"],
    "readonly": ["https://www.googleapis.com/auth/drive.readonly"],
    "file": ["https://www.googleapis.com/auth/drive.file"],
    "full": ["https://www.googleapis.com/auth/drive"],
    "tasks": ["https://www.googleapis.com/auth/tasks"],
    "calendar": ["https://www.googleapis.com/auth/calendar"],
}

DEFAULT_MODE = "metadata_readonly"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _pkce_pair() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode("utf-8")).digest())
    return verifier, challenge


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise HTTPException(500, f"Missing required environment variable: {name}")
    return value


def _redirect_uri() -> str:
    return _required_env("GOOGLE_DRIVE_REDIRECT_URI")


def _client_id() -> str:
    return _required_env("GOOGLE_DRIVE_CLIENT_ID")


def _client_secret() -> str:
    return _required_env("GOOGLE_DRIVE_CLIENT_SECRET")


def _scope_list(mode: str) -> List[str]:
    scopes = SCOPE_MAP.get(mode or DEFAULT_MODE)
    if not scopes:
        raise HTTPException(400, f"Unsupported Google OAuth mode: {mode}")
    return scopes + ["openid", "email", "profile"]


# Maps an OAuth flow's granted scopes back to the Google-family integration
# provider that owns them. A flow that asked for the Tasks scope cannot land
# in a "google_drive" integration (the Drive client would try to use a
# tasks-only token); keep the two providers distinct while sharing one code
# path and token plumbing.
GOOGLE_PROVIDER_BY_SCOPE_SIGNATURE: tuple[tuple[str, str], ...] = (
    ("/tasks", "google_tasks"),
    ("/calendar", "google_calendar"),
)


def provider_for_requested_scopes(requested_scopes: Optional[List[str]]) -> str:
    joined = " ".join(requested_scopes or [])
    for signature, provider in GOOGLE_PROVIDER_BY_SCOPE_SIGNATURE:
        if signature in joined:
            return provider
    return "google_drive"


def _provider_label(provider: str) -> str:
    if provider == "google_tasks":
        return "Google Tasks"
    if provider == "google_calendar":
        return "Google Calendar"
    return "Google Drive"


def begin_connect(*, owner_id: str, integration_id: Optional[str], mode: str) -> Dict[str, Any]:
    state = secrets.token_urlsafe(32)
    verifier, challenge = _pkce_pair()
    redirect_uri = _redirect_uri()
    scopes = _scope_list(mode)

    state_store.purge_expired_states()
    state_store.create_state(
        provider="google_drive",
        owner_id=owner_id,
        state=state,
        code_verifier=verifier,
        code_challenge=challenge,
        redirect_uri=redirect_uri,
        requested_scopes=scopes,
        integration_id=integration_id,
    )

    params = {
        "client_id": _client_id(),
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "access_type": "offline",
        "prompt": "consent",
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    }
    return {
        "ok": True,
        "authorize_url": f"{GOOGLE_AUTH_URI}?{urlencode(params)}",
        "state": state,
        "mode": mode or DEFAULT_MODE,
    }


async def _exchange_code(*, code: str, redirect_uri: str, code_verifier: str) -> Dict[str, Any]:
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "client_id": _client_id(),
        "client_secret": _client_secret(),
        "redirect_uri": redirect_uri,
        "code_verifier": code_verifier,
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(GOOGLE_TOKEN_URI, data=payload)
    if response.status_code >= 400:
        raise HTTPException(400, f"Google token exchange failed: {response.text}")
    return response.json()


async def _fetch_userinfo(access_token: str) -> Dict[str, Any]:
    headers = {"Authorization": f"Bearer {access_token}"}
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(GOOGLE_USERINFO_URI, headers=headers)
    if response.status_code >= 400:
        return {}
    data = response.json()
    return data if isinstance(data, dict) else {}


def _compute_expiry(expires_in: Any) -> Optional[str]:
    try:
        seconds = int(expires_in)
    except Exception:
        return None
    return (_utcnow() + timedelta(seconds=seconds)).isoformat()


async def handle_callback(*, code: str, state: str) -> Dict[str, Any]:
    pending = state_store.get_state(state)
    if not pending:
        raise HTTPException(400, "Google OAuth state not found")
    if pending.get("status") != "pending":
        raise HTTPException(409, "Google OAuth state already consumed")
    if state_store.is_expired(pending):
        state_store.mark_state(state, "expired")
        raise HTTPException(409, "Google OAuth state expired")

    token = await _exchange_code(
        code=code,
        redirect_uri=str(pending["redirect_uri"]),
        code_verifier=str(pending["code_verifier"]),
    )
    access_token = token.get("access_token", "")
    refresh_token = token.get("refresh_token", "")
    token_type = token.get("token_type", "Bearer")
    scope = token.get("scope", "")
    expires_at = _compute_expiry(token.get("expires_in"))
    userinfo = await _fetch_userinfo(access_token)

    provider = provider_for_requested_scopes(pending.get("requested_scopes"))
    settings: Dict[str, Any] = {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "scope": scope,
        "token_uri": GOOGLE_TOKEN_URI,
        "auth_uri": GOOGLE_AUTH_URI,
    }
    if provider == "google_tasks":
        settings["mode"] = "tasks"
    elif provider == "google_calendar":
        settings["mode"] = "calendar"
    else:
        settings["drive_mode"] = "metadata_readonly"

    patch = {
        "provider": provider,
        "preset": provider,
        "name": _provider_label(provider),
        "base_url": "https://www.googleapis.com",
        "enabled": True,
        "auth_type": "bearer",
        "api_key": "",
        "oauth_provider": "google",
        "oauth_access_token": access_token,
        "oauth_refresh_token": refresh_token,
        "oauth_token_type": token_type,
        "oauth_scope": scope,
        "oauth_expires_at": expires_at,
        "oauth_client_id": _client_id(),
        "oauth_client_secret": _client_secret(),
        "oauth_connected_email": userinfo.get("email", ""),
        "oauth_connected_subject": userinfo.get("sub", ""),
        "settings": settings,
    }

    integration_id = pending.get("integration_id")
    integration = get_integration(str(integration_id)) if integration_id else None
    if integration:
        result = update_integration(str(integration_id), patch)
    else:
        result = add_integration(patch)

    state_store.mark_state(state, "consumed")
    return {
        "ok": True,
        "integration_id": result["id"],
        "provider": provider,
        "connected_email": userinfo.get("email", ""),
        "scope": scope,
        "expires_at": expires_at,
        "has_refresh_token": bool(refresh_token),
    }


async def refresh_access_token(integration: Dict[str, Any]) -> Dict[str, Any]:
    refresh_token = (
        integration.get("oauth_refresh_token")
        or integration.get("settings", {}).get("refresh_token", "")
    )
    if not refresh_token:
        raise HTTPException(409, "Reconnect required: no refresh token available")

    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": _client_id(),
        "client_secret": _client_secret(),
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(GOOGLE_TOKEN_URI, data=payload)
    if response.status_code >= 400:
        raise HTTPException(409, f"Reconnect required: token refresh failed: {response.text}")

    token = response.json()
    access_token = token.get("access_token", "")
    expires_at = _compute_expiry(token.get("expires_in"))
    scope = token.get("scope") or integration.get("oauth_scope", "")

    settings = dict(integration.get("settings") or {})
    settings["access_token"] = access_token
    settings["refresh_token"] = refresh_token
    settings["scope"] = scope
    settings.setdefault("token_uri", GOOGLE_TOKEN_URI)
    settings.setdefault("auth_uri", GOOGLE_AUTH_URI)

    patch = {
        "oauth_access_token": access_token,
        "oauth_refresh_token": refresh_token,
        "oauth_scope": scope,
        "oauth_expires_at": expires_at,
        "settings": settings,
    }
    updated = update_integration(str(integration["id"]), patch)
    if not updated:
        raise HTTPException(404, "Integration disappeared during token refresh")
    return updated


async def ensure_fresh_google_token(
    integration: Dict[str, Any],
    *,
    force_refresh: bool = False,
) -> Dict[str, Any]:
    """Return a usable Google integration and refresh when required.

    With force_refresh=True, always exchange the stored refresh token for a
    fresh access token. This is useful for diagnostics and explicit UI actions.
    """
    if force_refresh:
        return await refresh_access_token(integration)

    expires_at = integration.get("oauth_expires_at")
    if not expires_at:
        token = integration.get("oauth_access_token") or integration.get("settings", {}).get("access_token")
        if token:
            return integration
        raise HTTPException(409, "Reconnect required: missing Google OAuth token")

    try:
        expiry = datetime.fromisoformat(str(expires_at))
    except Exception:
        return await refresh_access_token(integration)

    if expiry <= (_utcnow() + timedelta(minutes=5)):
        return await refresh_access_token(integration)
    return integration
