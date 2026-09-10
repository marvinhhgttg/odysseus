"""Shared auth helpers used by all route files.

Re-exports the centralised dependencies from :mod:`src.auth_dependencies` so
existing ``from src.auth_helpers import ...`` imports keep working. New code
should import directly from ``src.auth_dependencies``.
"""

from typing import Optional

from fastapi import Request

# Re-export the canonical implementations
from src.auth_dependencies import (  # noqa: F401
    require_user,
    require_admin,
    require_privilege,
    get_effective_user,
    get_effective_user as effective_user,
    _get_current_user as get_current_user,
    _is_api_token_request,
    _auth_disabled,
    get_auth_manager,
    invalidate_token_cache,
)


def require_authenticated_request(request: Request) -> str:
    """Allow either a browser session or a valid bearer API token.

    This is intentionally narrower than :func:`require_user`: use it only for
    routes that need authentication but do not read or mutate owner-scoped
    user data.
    """
    if _is_api_token_request(request):
        return get_effective_user(request) or ""
    return require_user(request)


def owner_filter(query, model_cls, user: str, *, include_shared: bool = True):
    """Filter `query` so only rows owned by `user` (and optionally null-owner
    'shared' rows) come through. No-op when `user` is empty (single-user
    mode). Returns the modified query."""
    if not user:
        return query
    if include_shared:
        return query.filter((model_cls.owner == user) | (model_cls.owner == None))  # noqa: E711
    return query.filter(model_cls.owner == user)
