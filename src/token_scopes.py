"""API-token scopes → effective privilege gate for chat routes.

A bearer API token authenticates as its *owner*, but the owner's stored
privileges were never restricted to what the token's scopes grant. A
``chat``-scoped token minted for an admin owner therefore inherited
``can_use_bash = True`` through the agent: the token holder could drive a
shell-enabled agent with nothing but the default ``chat`` scope.

This module intersects the two. Unless the token explicitly carries the
``admin`` scope, every admin-raisable capability is capped at the non-admin
baseline (what a fresh non-admin user gets). Per-user *restrictions*
(denied models, daily message cap) are preserved as stored — they only ever
take away, never grant.
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, Optional

from core.auth import DEFAULT_PRIVILEGES

# Privilege keys that an admin owner's stored map raises above the non-admin
# baseline. A token caller may only hold these at the owner's (elevated) value
# when the token itself carries the "admin" scope.
_ADMIN_RAISABLE_KEYS = frozenset({
    "can_use_agent",
    "can_use_browser",
    "can_use_bash",
    "can_use_documents",
    "can_use_research",
    "can_generate_images",
    "can_manage_memory",
})


def _scope_set(scopes: Optional[Iterable]) -> set:
    if not scopes:
        return set()
    return {str(s).strip() for s in scopes if str(s).strip()}


def token_carries_admin_scope(scopes: Optional[Iterable]) -> bool:
    """True when the token explicitly carries the ``admin`` scope."""
    return "admin" in _scope_set(scopes)


def effective_privileges_for_token(scopes: Optional[Iterable], owner_privs: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Restrict owner privileges to what an API token's scopes grant.

    With an ``admin`` scope the full owner map passes through unchanged.
    Otherwise every admin-raisable capability key is clamped to the
    non-admin ``DEFAULT_PRIVILEGES`` baseline, while the owner's per-user
    restrictions (``allowed_models``, ``max_messages_per_day``,
    ``block_all_models``, ...) are carried over verbatim.

    The input map is copied; the caller's dict is never mutated.
    """
    result = dict(owner_privs or {})
    if not result:
        return result
    if token_carries_admin_scope(scopes):
        return result
    for key in _ADMIN_RAISABLE_KEYS:
        if key in result:
            result[key] = DEFAULT_PRIVILEGES.get(key, False)
    return result


def _is_token_request(request) -> bool:
    state = getattr(request, "state", None)
    return bool(state is not None and getattr(state, "api_token", False))


def chat_privileges(request, auth_manager, user) -> Dict[str, Any]:
    """Privileges that apply to a chat caller, scoped for API tokens.

    Resolves the caller's stored privileges via ``auth_manager`` (if both user
    and auth_manager are present, mirroring the route logic) and, when the
    request is authenticated by an API token, intersects them with the token's
    scopes via :func:`effective_privileges_for_token`.
    """
    privs: Dict[str, Any] = {}
    if user and auth_manager is not None:
        privs = auth_manager.get_privileges(user) or {}
    if _is_token_request(request):
        privs = effective_privileges_for_token(
            getattr(request.state, "api_token_scopes", []), privs
        )
    return privs