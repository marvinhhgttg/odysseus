"""Centralised FastAPI dependency functions for authentication and authorization.

Usage in route files::

    from src.auth_dependencies import (
        require_admin, require_user, get_effective_user,
        require_privilege, get_auth_manager, invalidate_token_cache,
    )

    @router.get("/api/admin/settings")
    async def admin_settings(_admin: None = Depends(require_admin)):
        ...

    @router.get("/api/data")
    async def get_data(user: str = Depends(require_user)):
        ...

    @router.get("/api/data")
    async def get_data(owner: str = Depends(get_effective_user)):
        ...

    @router.post("/api/chat")
    async def chat(user: str = Depends(require_privilege("can_use_agent"))):
        ...

All functions accept ``request: Request`` which FastAPI injects automatically.
They read from ``request.state`` (stamped by the AuthMiddleware in app.py) and
perform authorization checks, raising HTTPException on failure.
"""

import os
from typing import Optional

from fastapi import Depends, Request, HTTPException


# ---------------------------------------------------------------------------
# Low-level helpers (read from request.state set by AuthMiddleware)
# ---------------------------------------------------------------------------

def _get_current_user(request: Request) -> Optional[str]:
    """Read the username stamped by AuthMiddleware. Returns None if absent."""
    return getattr(request.state, "current_user", None)


def _is_api_token_request(request: Request) -> bool:
    """True when middleware authenticated a bearer API token."""
    return bool(getattr(request.state, "api_token", False))


def _auth_disabled() -> bool:
    """True when the operator has explicitly turned off auth via .env."""
    return os.getenv("AUTH_ENABLED", "true").lower() == "false"


# ---------------------------------------------------------------------------
# Manager / service providers
# ---------------------------------------------------------------------------

def get_auth_manager(request: Request):
    """Return the AuthManager stored on app.state by the startup sequence.

    Usable as a FastAPI ``Depends()`` target::

        @router.get("/api/example")
        async def example(request: Request, auth_mgr = Depends(get_auth_manager)):
            ...

    Also callable as a plain helper function when only the request is available.
    Returns ``None`` when no AuthManager has been initialised (single-user mode).
    """
    return getattr(request.app.state, "auth_manager", None)


def invalidate_token_cache(request: Request) -> None:
    """Mark the internal auth-token prefix cache as dirty so the next lookup
    re-reads from the database.  Usable as a Depends target or plain call."""
    inv = getattr(request.app.state, "invalidate_token_cache", None)
    if callable(inv):
        inv()


# ---------------------------------------------------------------------------
# Public dependencies
# ---------------------------------------------------------------------------

def get_effective_user(request: Request) -> Optional[str]:
    """Resolve the real human behind the request, for ownership/attribution.

    Cookie sessions resolve to the logged-in username. Bearer ``ody_`` callers
    come through as the sandboxed pseudo-user "api", but their token belongs to
    a real owner stamped on ``request.state.api_token_owner``. Routes that
    attribute a token's actions to the owner (sessions, chat history) should
    use this instead of :func:`require_user`.
    """
    if getattr(request.state, "api_token", False):
        owner = getattr(request.state, "api_token_owner", None)
        if owner:
            return owner
    return _get_current_user(request)


def require_user(request: Request) -> str:
    """Reject unauthenticated callers (401) unless auth is disabled,
    first-run loopback, or localhost bypass. Returns the resolved username,
    or ``""`` in single-user / anonymous modes."""
    if _is_api_token_request(request):
        raise HTTPException(403, "API tokens must use a scope-aware API route")

    u = _get_current_user(request)
    if u:
        return u
    if _auth_disabled():
        return ""
    auth_mgr = get_auth_manager(request)
    client = getattr(request, "client", None)
    host = (client.host if client else "") or ""
    is_loopback = host in ("127.0.0.1", "::1", "localhost")
    if is_loopback and os.getenv("LOCALHOST_BYPASS", "false").lower() == "true":
        return ""
    if auth_mgr is not None and getattr(auth_mgr, "is_configured", False):
        raise HTTPException(401, "Not authenticated")
    if is_loopback:
        return ""
    raise HTTPException(401, "Not authenticated")


def require_admin(request: Request) -> None:
    """Raise 403 if the current user isn't an admin.

    Allows access when auth is explicitly disabled, or when the request carries
    the in-process internal-tool token used by loopback agent tools.
    Works both as ``Depends(require_admin)`` and as a plain function call.
    """
    from core.middleware import INTERNAL_TOOL_USER
    from src.internal_tool_auth import internal_tool_request_ok

    # In-process bypass for tool-layer loopback calls. Gate re-checked here
    # (token AND trusted loopback) as defence in depth — never header alone.
    if internal_tool_request_ok(request):
        return
    # AuthMiddleware only stamps the reserved pseudo-user after passing the
    # same loopback gate; trust that stamp, not a raw header.
    if getattr(request.state, "current_user", None) == INTERNAL_TOOL_USER:
        return

    auth_mgr = get_auth_manager(request)
    if os.getenv("AUTH_ENABLED", "true").lower() == "false":
        return
    if not auth_mgr or not auth_mgr.is_configured:
        raise HTTPException(403, "Admin only")
    user = getattr(request.state, "current_user", None)
    if not user or not auth_mgr.is_admin(user):
        raise HTTPException(403, "Admin only")


def require_privilege(key_or_request, key=None):
    """Check a privilege for the current caller.

    Two interchangeable forms are supported:

    Plain (request-context) form, returning the username::

        user = require_privilege(request, "can_use_documents")

    Dependency-factory form::

        @router.post("/api/bash/exec")
        async def exec_cmd(_p: None = Depends(require_privilege("can_use_bash"))):
            ...

    Admins always pass. In unauthenticated single-user mode, privileges
    aren't enforced. Returns the username so the route handler can keep
    using it; raises 403 when the caller's privilege flag for *key* is
    explicitly False (missing flags fail open).
    """
    def _check(request, privilege_key: str) -> str:
        user = require_user(request)
        if not user:
            return user
        auth_mgr = get_auth_manager(request)
        if auth_mgr is None:
            return user
        try:
            privs = auth_mgr.get_privileges(user) or {}
        except Exception:
            return user
        if not isinstance(privs, dict):
            privs = {}
        if not privs.get(privilege_key, True):
            raise HTTPException(403, f"Your account is not allowed to {privilege_key.replace('_', ' ')}.")
        return user

    if key is not None:
        return _check(key_or_request, key)

    def _dep(request: Request) -> str:
        return _check(request, key_or_request)
    return _dep
