"""Shared internal-tool auth: header token + trusted-loopback gate.

Odysseus' agent tool layer reaches admin-gated routes over HTTP loopback
(builtin actions, MCP, skill webhooks, task actions) without a browser
session cookie. Those calls authenticate with a per-process bearer-style
header (``X-Odysseus-Internal-Token``). This module is the single source of
truth for deciding which requests are legitimately "internal":

- the token must match (constant-time compare); AND
- the request must come from the trusted loopback peer (127.0.0.1 / ::1) with
  no proxy/tunnel forwarding headers — cloudflared/nginx connect from
  loopback, so a bare host check alone would let a remote visitor inherit
  local trust over a tunnel.

AuthMiddleware (app.py) applies this gate when stamping
``request.state.current_user`` with the reserved pseudo-user; route-level
``require_admin`` re-checks the SAME gate as defence in depth, so the bypass
never depends on middleware-ordering alone.
"""

from __future__ import annotations

import os
import secrets

from fastapi import Request

from core.middleware import (
    INTERNAL_TOOL_HEADER,
    INTERNAL_TOOL_TOKEN,
    INTERNAL_TOOL_USER,
)

# Headers that prove a request was forwarded by a proxy/tunnel (cloudflared,
# nginx, Caddy, Tailscale Funnel, …). Odysseus's own in-process agent loopback
# calls carry none of them.
_PROXY_FWD_HEADERS = (
    "cf-connecting-ip", "cf-ray", "cf-visitor",
    "x-forwarded-for", "x-forwarded-host", "x-real-ip", "forwarded",
)


def is_trusted_loopback(request: Request) -> bool:
    """True ONLY for a DIRECT loopback connection with no proxy/tunnel
    forwarding headers.

    A bare ``client.host in ("127.0.0.1", "::1")`` check is unsafe behind a
    Cloudflare tunnel / reverse proxy: those connect from loopback, so a remote
    visitor would otherwise inherit local trust and slip past LOCALHOST_BYPASS
    or spoof the internal-tool path.
    """
    client = getattr(request, "client", None)
    host = (client.host if client else None) or ""
    if host not in ("127.0.0.1", "::1"):
        return False
    for header in _PROXY_FWD_HEADERS:
        if request.headers.get(header):
            return False
    return True


def _token_matches(request: Request) -> bool:
    try:
        supplied = (request.headers.get(INTERNAL_TOOL_HEADER) or "").encode("utf-8")
    except Exception:
        return False
    try:
        return secrets.compare_digest(supplied, INTERNAL_TOOL_TOKEN.encode("utf-8"))
    except Exception:
        return False


def internal_tool_request_ok(request: Request) -> bool:
    """True when the request is a legitimate in-process tool-loopback call.

    Requires BOTH the matching token header AND a trusted loopback peer. The
    header alone (even with a correct token) does not qualify — a remote
    client that somehow possesses the token must still fail closed.
    """
    return _token_matches(request) and is_trusted_loopback(request)


def resolve_internal_tool_user(request: Request, known_users) -> str:
    """Resolve the pseudo-user for an internal-tool request.

    ``X-Odysseus-Owner`` impersonation is honoured ONLY for users that actually
    exist; otherwise the reserved ``INTERNAL_TOOL_USER`` is used. Authorization
    checks stay separate from this owner attribution.
    """
    impersonate = (request.headers.get("X-Odysseus-Owner") or "").strip()
    if impersonate and impersonate in known_users:
        return impersonate
    return INTERNAL_TOOL_USER


def internal_tool_status() -> dict:
    """Masked configuration facts for diagnostics. Never the token itself."""
    return {
        "enabled": True,
        "header": INTERNAL_TOOL_HEADER,
        "token_source": (
            "env" if os.environ.get("ODYSSEUS_INTERNAL_TOKEN")
            else "ephemeral_per_process"
        ),
        "reserved_user": INTERNAL_TOOL_USER,
        "loopback_only": True,
    }