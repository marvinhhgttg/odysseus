"""mcp_oauth.py — generic OAuth for remote (Streamable HTTP) MCP servers.

Bridges the mcp SDK's OAuthClientProvider (RFC 9728 discovery, Dynamic Client
Registration, authorization-code + PKCE, token refresh) to Odysseus's web
callback route. Tokens and the dynamic registration persist per-server,
encrypted, so the interactive flow runs only once.
"""
import asyncio
import json
import logging
import os
import time
from typing import Dict, Optional, Tuple
from urllib.parse import urlparse, parse_qs

logger = logging.getLogger(__name__)

# OAuth redirect URI registered with every authorization server via DCR. Loopback
# is allowed for native/desktop clients (RFC 8252); remote users finish via the
# paste-back flow. Deployments not reachable at http://localhost:7000 (custom
# port, reverse proxy, or public domain) must set OAUTH_REDIRECT_BASE_URL (or
# APP_PUBLIC_URL) to their externally reachable origin so the redirect lands back
# on Odysseus. APP_PORT is intentionally not used: it is only the Docker host
# port-map; the app always listens on 7000 inside the container.
_REDIRECT_BASE = (
    os.environ.get("OAUTH_REDIRECT_BASE_URL")
    or os.environ.get("APP_PUBLIC_URL")
    or "http://localhost:7000"
).rstrip("/")
REDIRECT_URI = f"{_REDIRECT_BASE}/api/mcp/oauth/callback"

# How long the background connect waits for the user to authorize before giving up.
AUTH_WAIT_SECONDS = 300

_pending: Dict[str, asyncio.Future] = {}   # state -> Future[(code, state)]
_pending_ts: Dict[str, float] = {}         # state -> monotonic timestamp, for pruning
_auth_urls: Dict[str, str] = {}            # server_id -> authorization URL


def _prune_stale() -> None:
    """Drop abandoned flows whose authorization window has elapsed so the
    module-level registries don't grow unbounded (e.g. a user who never
    finishes the browser step)."""
    now = time.monotonic()
    for state in [s for s, ts in _pending_ts.items() if now - ts > AUTH_WAIT_SECONDS]:
        fut = _pending.pop(state, None)
        _pending_ts.pop(state, None)
        if fut is not None and not fut.done():
            fut.cancel()


def _discard_pending(state: Optional[str]) -> None:
    if state is None:
        return
    _pending.pop(state, None)
    _pending_ts.pop(state, None)


def register_pending(state: str) -> asyncio.Future:
    _prune_stale()
    fut = asyncio.get_running_loop().create_future()
    _pending[state] = fut
    _pending_ts[state] = time.monotonic()
    return fut


def resolve_pending(state: str, code: str) -> bool:
    fut = _pending.get(state)
    if fut is not None and not fut.done():
        fut.set_result((code, state))
        return True
    return False


def pop_auth_url(server_id: str) -> Optional[str]:
    return _auth_urls.get(server_id)


def clear_auth_url(server_id: str) -> None:
    _auth_urls.pop(server_id, None)


class DbTokenStorage:
    """SDK TokenStorage backed by the encrypted McpServer.oauth_tokens column."""

    def __init__(self, server_id: str, session_factory=None):
        self.server_id = server_id
        if session_factory is None:
            from core.database import SessionLocal
            session_factory = SessionLocal
        self._sf = session_factory

    def _load(self) -> dict:
        from core.database import McpServer
        db = self._sf()
        try:
            srv = db.query(McpServer).filter(McpServer.id == self.server_id).first()
            if srv and srv.oauth_tokens:
                return json.loads(srv.oauth_tokens)
        finally:
            db.close()
        return {}

    def _update(self, key: str, value: dict) -> None:
        """Load, set one key, and persist the oauth_tokens JSON in a single
        session/commit (avoids the load+save double round-trip per write)."""
        from core.database import McpServer
        db = self._sf()
        try:
            srv = db.query(McpServer).filter(McpServer.id == self.server_id).first()
            if srv is None:
                return
            data = json.loads(srv.oauth_tokens) if srv.oauth_tokens else {}
            data[key] = value
            srv.oauth_tokens = json.dumps(data)
            db.commit()
        finally:
            db.close()

    async def get_tokens(self):
        from mcp.shared.auth import OAuthToken
        data = self._load().get("tokens")
        return OAuthToken.model_validate(data) if data else None

    async def set_tokens(self, tokens) -> None:
        self._update("tokens", json.loads(tokens.model_dump_json()))
        # Persist an absolute expiry (epoch seconds) next to the tokens so the
        # proactive refresh loop can decide whether a refresh is due without
        # re-parsing `expires_in` (which counts down from issue time).
        try:
            from mcp.client.auth.oauth2 import calculate_token_expiry
            expires_at = calculate_token_expiry(getattr(tokens, "expires_in", None))
        except ImportError:
            expires_at = None
        self._update("expires_at", expires_at)

    async def get_expires_at(self):
        value = self._load().get("expires_at")
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    async def set_meta(self, meta: dict) -> None:
        """Persist a small status/meta dict for observability (last refresh,
        errors, expiry). Stored alongside 'tokens'/'client_info' under 'meta'."""
        self._update("meta", meta)

    async def get_client_info(self):
        from mcp.shared.auth import OAuthClientInformationFull
        data = self._load().get("client_info")
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def set_client_info(self, client_info) -> None:
        self._update("client_info", json.loads(client_info.model_dump_json()))


def build_provider(server_id: str, url: str, on_redirect=None):
    """Construct an OAuthClientProvider that drives the browser flow via the
    Odysseus callback route.

    on_redirect(authorization_url): optional sync callback invoked the moment
    the authorization URL is known (after discovery + DCR). The manager uses it
    to publish 'needs_auth' + auth_url to connection state regardless of how
    long discovery/DCR took.
    """
    from mcp.client.auth import OAuthClientProvider
    from mcp.shared.auth import OAuthClientMetadata

    client_metadata = OAuthClientMetadata(
        client_name="Odysseus",
        redirect_uris=[REDIRECT_URI],
        grant_types=["authorization_code", "refresh_token"],
        response_types=["code"],
        # Leave scope unset: the SDK applies the MCP scope-selection strategy and
        # overwrites this from the server's WWW-Authenticate / protected-resource
        # metadata before building the auth URL. Hardcoding an OIDC scope here
        # would break the many MCP servers that are not OpenID providers.
        scope=None,
        token_endpoint_auth_method="none",
    )

    async def redirect_handler(authorization_url: str) -> None:
        state = (parse_qs(urlparse(authorization_url).query).get("state") or [None])[0]
        if state:
            register_pending(state)
        _auth_urls[server_id] = authorization_url
        if on_redirect is not None:
            try:
                on_redirect(authorization_url)
            except Exception as e:
                logger.warning(f"MCP OAuth on_redirect callback failed: {e}")
        logger.info(f"MCP OAuth: server {server_id} awaiting authorization (state={state})")

    async def callback_handler() -> Tuple[str, Optional[str]]:
        auth_url = _auth_urls.get(server_id)
        state = (parse_qs(urlparse(auth_url).query).get("state") or [None])[0] if auth_url else None
        fut = _pending.get(state)
        if fut is None:
            raise RuntimeError("No pending OAuth flow for this server")
        try:
            code, ret_state = await asyncio.wait_for(fut, timeout=AUTH_WAIT_SECONDS)
            return code, ret_state
        finally:
            _discard_pending(state)
            _auth_urls.pop(server_id, None)

    return OAuthClientProvider(
        server_url=url,
        client_metadata=client_metadata,
        storage=DbTokenStorage(server_id),
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )


# ── Proactive token refresh ─────────────────────────────────────────────────

async def _discover_token_endpoint(server_url: str) -> Optional[str]:
    """Locate the OAuth token endpoint for an MCP server.

    Mirrors the SDK's discovery chain (Protected Resource Metadata -> OAuth
    Authorization Server Metadata). Returns None when discovery fails so the
    caller falls back to the SDK's `{origin}/token` heuristic.
    """
    try:
        import httpx
        from mcp.client.auth.oauth2 import (
            ProtectedResourceMetadata,
            build_oauth_authorization_server_metadata_discovery_urls,
            build_protected_resource_metadata_discovery_urls,
            handle_auth_metadata_response,
        )
    except ImportError:
        return None
    async with httpx.AsyncClient(timeout=10) as client:
        auth_server_url = None
        for url in build_protected_resource_metadata_discovery_urls(None, server_url):
            try:
                resp = await client.get(url)
            except Exception:
                continue
            if resp.status_code != 200:
                continue
            try:
                prm = ProtectedResourceMetadata.model_validate_json(resp.content)
            except Exception:
                continue
            if prm.authorization_servers:
                auth_server_url = str(prm.authorization_servers[0])
                break
        for url in build_oauth_authorization_server_metadata_discovery_urls(
            auth_server_url, server_url
        ):
            try:
                resp = await client.get(url)
            except Exception:
                continue
            ok, metadata = handle_auth_metadata_response(resp)
            if ok and metadata and metadata.token_endpoint:
                return str(metadata.token_endpoint)
    return None


# How long an access token must still be valid before the loop leaves it alone.
MCP_REFRESH_MIN_TTL_SECONDS = 900


async def _perform_refresh(provider, server_url: str):
    """Run one token-refresh exchange through the SDK provider.

    Returns (ok, new_expires_at, error-text).
    """
    token_endpoint = await _discover_token_endpoint(server_url)
    if token_endpoint:
        try:
            from mcp.client.auth.oauth2 import OAuthMetadata
            provider.context.oauth_metadata = OAuthMetadata(token_endpoint=token_endpoint)
        except Exception:
            pass
    try:
        import httpx
        refresh_request = await provider._refresh_token()
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.send(refresh_request)
        ok = await provider._handle_refresh_response(response)
        storage = provider.context.storage
        new_expires = await storage.get_expires_at() if ok else None
        return ok, new_expires, ("" if ok else f"HTTP {response.status_code}")
    except Exception as e:
        return False, None, str(e)


async def refresh_mcp_server_access_token(
    server_id: str,
    *,
    min_ttl_seconds: float = MCP_REFRESH_MIN_TTL_SECONDS,
) -> Dict[str, object]:
    """Refresh one MCP server's OAuth access token before it expires.

    Proactive counterpart to the SDK's lazy refresh (which runs on the first
    request after expiry). For idle-but-connected servers this keeps the stored
    token fresh so a 401 never escalates to a full interactive re-auth.

    Returns a status dict; never raises:
      {'status': 'valid'}            token expiry still far away
      {'status': 'refreshed', ...}   token successfully renewed
      {'status': 'no_tokens'|'no_refresh_token'|'load_failed'|'refresh_failed'|...}
    """
    from core.database import McpServer, SessionLocal

    db = SessionLocal()
    try:
        srv = db.query(McpServer).filter(McpServer.id == server_id).first()
    finally:
        db.close()
    if srv is None or not srv.is_enabled:
        return {"status": "disabled_or_missing"}
    if not (srv.url or "").startswith(("http://", "https://")):
        return {"status": "not_http"}

    storage = DbTokenStorage(server_id, session_factory=SessionLocal)
    tokens = await storage.get_tokens()
    if tokens is None or not tokens.access_token:
        return {"status": "no_tokens"}
    if not tokens.refresh_token:
        try:
            await storage.set_meta({"last_status": "no_refresh_token"})
        except Exception:
            pass
        return {"status": "no_refresh_token"}

    expires_at = await storage.get_expires_at()
    now = time.time()
    if expires_at is not None and expires_at - now > min_ttl_seconds:
        try:
            await storage.set_meta({"last_status": "valid", "expires_at": expires_at, "ttl_seconds": int(expires_at - now)})
        except Exception:
            pass
        return {
            "status": "valid",
            "expires_at": expires_at,
            "ttl_seconds": int(expires_at - now),
        }

    provider = build_provider(server_id, srv.url or "")
    try:
        await provider._initialize()
    except Exception as e:
        return {"status": "load_failed", "error": str(e)}

    ok, new_expires, error = await _perform_refresh(provider, srv.url or "")
    if ok:
        try:
            await storage.set_meta({"last_status": "refreshed", "expires_at": new_expires})
        except Exception:
            pass
        return {"status": "refreshed", "expires_at": new_expires}
    try:
        await storage.set_meta({"last_status": "refresh_failed", "error": error})
    except Exception:
        pass
    return {"status": "refresh_failed", "error": error}


async def refresh_mcp_servers_due(
    *,
    min_ttl_seconds: float = MCP_REFRESH_MIN_TTL_SECONDS,
) -> Dict[str, int]:
    """Sweep every enabled HTTP MCP server that stores OAuth tokens.

    Runs on a timer. Skips servers whose access token still has a healthy
    remaining lifetime, so most passes touch nothing.
    """
    from core.database import McpServer, SessionLocal

    db = SessionLocal()
    try:
        servers = (
            db.query(McpServer)
            .filter(McpServer.is_enabled == True)  # noqa: E712
            .all()
        )
        rows = [
            (s.id, s.url)
            for s in servers
            if s.oauth_tokens and (s.url or "").startswith(("http://", "https://"))
        ]
    finally:
        db.close()

    summary: Dict[str, int] = {
        "checked": 0, "refreshed": 0, "valid": 0,
        "no_tokens": 0, "no_refresh_token": 0, "failed": 0,
    }
    for server_id, url in rows:
        summary["checked"] += 1
        result = await refresh_mcp_server_access_token(
            server_id, min_ttl_seconds=min_ttl_seconds
        )
        status = result.get("status")
        if status == "refreshed":
            summary["refreshed"] += 1
        elif status == "valid":
            summary["valid"] += 1
        elif status == "no_tokens":
            summary["no_tokens"] += 1
        elif status == "no_refresh_token":
            summary["no_refresh_token"] += 1
        else:
            summary["failed"] += 1
    return summary
