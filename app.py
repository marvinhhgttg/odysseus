# app.py — slim orchestrator
import mimetypes
import os
import sys
import asyncio
import time

from src.app_helpers import setup_environment, abs_join, serve_html_with_nonce
setup_environment()

from dotenv import load_dotenv
# encoding="utf-8-sig" tolerates a UTF-8 BOM in .env — a common Windows gotcha
# when the file is saved from Notepad. Without this, the first key parses as
# "﻿AUTH_ENABLED" instead of "AUTH_ENABLED", so AUTH_ENABLED=false (etc.)
# is silently ignored and the user is unexpectedly forced to log in (issue #142).
# utf-8-sig reads plain UTF-8 (no BOM) identically, so this is safe everywhere.
load_dotenv(encoding="utf-8-sig")


import logging
from datetime import datetime, timezone
from typing import Dict

from contextlib import asynccontextmanager
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware

# Core imports
from core.constants import (
    BASE_DIR, STATIC_DIR, SESSIONS_FILE,
    REQUEST_TIMEOUT, OPENAI_API_KEY, AUTH_FILE,
)
from core.database import SessionLocal, ApiToken
from core.middleware import SecurityHeadersMiddleware, is_cors_preflight
from core.auth import AuthManager, normalize_known_username
from core.exceptions import (
    SessionNotFoundError, InvalidFileUploadError,
    LLMServiceError, WebSearchError,
)

import bcrypt as _bcrypt

from src.app_helpers import abs_join, serve_html_with_nonce
from src.generated_images import GENERATED_IMAGE_HEADERS, resolve_generated_image_path
from starlette.responses import RedirectResponse

# ========= LOGGING =========
import logging.handlers
from core.constants import DATA_DIR
from src.request_context import RequestIdLogFilter, RequestIdMiddleware

_root_logger = logging.getLogger()
_root_logger.setLevel(logging.INFO)
_formatter = logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - '
    'request_id=%(request_id)s - run_id=%(run_id)s - '
    'task_run_id=%(task_run_id)s - %(message)s'
)
_request_id_filter = RequestIdLogFilter()

# Clear existing handlers to avoid duplicates
for _h in list(_root_logger.handlers):
    _root_logger.removeHandler(_h)

_console_h = logging.StreamHandler()
_console_h.addFilter(_request_id_filter)
_console_h.setFormatter(_formatter)
_root_logger.addHandler(_console_h)

try:
    _log_dir = os.path.join(DATA_DIR, "logs")
    os.makedirs(_log_dir, exist_ok=True)
    _log_file = os.path.join(_log_dir, "app.log")

    # RotatingFileHandler is not multi-process safe (e.g. if uvicorn is run with --workers N).
    # Odysseus is single-process by convention, so this is acceptable, but be aware that
    # concurrent log rotation issues can arise if multiple workers are configured.
    _file_h = logging.handlers.RotatingFileHandler(
        _log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
    )
    _file_h.addFilter(_request_id_filter)
    _file_h.setFormatter(_formatter)
    _root_logger.addHandler(_file_h)
except Exception as e:
    _root_logger.warning(f"Failed to initialize file logging handler (falling back to console-only): {e}")

logger = logging.getLogger(__name__)

# ========= APP =========
# Lifespan is defined below (after all helpers it references are in scope)
# and passed to FastAPI so we can use the modern context-manager lifecycle
# instead of the deprecated @app.on_event("startup"/"shutdown") decorators.
app = FastAPI(
    title="AI Chat Application",
    description="Comprehensive AI chat with memory, research, and multi-modal capabilities",
    version="1.0.0",
)

# ========= CORS =========
CORS_ALLOW_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"]
allowed_origins = os.getenv("ALLOWED_ORIGINS", "http://localhost,http://127.0.0.1").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=CORS_ALLOW_METHODS,
    allow_headers=[
        "Accept",
        "Authorization",
        "Content-Type",
        "X-API-Key",
        "X-Auth-Token",
        "X-Odysseus-Internal-Token",
        "X-Odysseus-Owner",
        "X-Requested-With",
        "X-TZ-Offset",
    ],
)

# ========= RESPONSE COMPRESSION (gzip) =========
# The frontend's text assets (style.css, index.html, the JS bundles) shipped
# uncompressed on every cold load. gzip cuts CSS/JS/HTML by ~75-85% on the wire
# with no behavioural change. Starlette's GZipMiddleware excludes
# `text/event-stream` by default, so the SSE streams (chat, shell, research,
# model-probe — all served with media_type="text/event-stream") are never
# compressed or buffered; only complete bodies over minimum_size are. The
# security-header middleware composes cleanly on top.
app.add_middleware(GZipMiddleware, minimum_size=1024, compresslevel=6)

# ========= SECURITY HEADERS MIDDLEWARE =========
app.add_middleware(SecurityHeadersMiddleware)


# ========= REQUEST TIMEOUT (FALLBACK FOR HUNG HANDLERS) =========
# If a single request takes longer than REQUEST_HARD_TIMEOUT, abort it and
# return 504 instead of holding the event loop hostage. Whitelisted paths
# (streaming, long-running shell exec, research) are exempt because they
# legitimately stay open. Without this, a single hung subprocess.run or
# missing-timeout httpx call locks up the entire server for everyone.
import asyncio as _asyncio
from starlette.middleware.base import BaseHTTPMiddleware as _BaseHTTPMiddleware
from starlette.responses import JSONResponse as _JSONResponse

REQUEST_HARD_TIMEOUT = float(os.getenv("REQUEST_HARD_TIMEOUT", "45"))
_TIMEOUT_EXEMPT_PREFIXES = (
    "/api/chat",            # streaming
    "/api/shell/stream",    # SSE
    "/api/research",        # multi-minute jobs
    "/api/model/download",  # tmux setup may run pip installs
    "/api/model/probe",     # SSE; iterates models with up to 8s timeout each
    "/api/model-endpoints", # /probe sub-route also iterates models
    "/api/cookbook/setup",  # remote pacman/apt installs
    "/api/upload",          # large files
    "/api/image",           # diffusion proxies (inpaint/harmonize/upscale/etc.) — own 120s httpx timeout
    "/api/memory/audit",    # retains own 120s LLM inactivity timeout
)


class _RequestTimeoutMiddleware(_BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path or ""
        if any(path.startswith(p) for p in _TIMEOUT_EXEMPT_PREFIXES):
            return await call_next(request)
        try:
            return await _asyncio.wait_for(call_next(request), timeout=REQUEST_HARD_TIMEOUT)
        except _asyncio.TimeoutError:
            return _JSONResponse(
                {"detail": f"Request exceeded {REQUEST_HARD_TIMEOUT:.0f}s timeout"},
                status_code=504,
            )


class _InteractiveActivityMiddleware(_BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        from src.interactive_gate import should_track_interactive_request, track_interactive_request

        path = request.url.path or ""
        if not should_track_interactive_request(path, request.method):
            return await call_next(request)
        async def _stop_background():
            try:
                await task_scheduler.stop_background_tasks_for_foreground(reason=f"foreground request {request.method} {path}")
            except Exception:
                logging.getLogger("app.foreground_gate").debug("foreground task stop failed", exc_info=True)
        asyncio.create_task(_stop_background())
        async with track_interactive_request(path, request.method):
            return await call_next(request)


class _SlowRequestLogMiddleware(_BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = getattr(response, "status_code", 0) or 0
            return response
        finally:
            elapsed = time.perf_counter() - start
            try:
                threshold = float(os.getenv("ODYSSEUS_SLOW_REQUEST_LOG_SECONDS", "0.75") or "0.75")
            except Exception:
                threshold = 0.75
            if elapsed >= threshold:
                logging.getLogger("app.slow_request").warning(
                    "slow_request method=%s path=%s status=%s elapsed=%.3fs",
                    request.method,
                    request.url.path,
                    status,
                    elapsed,
                )


app.add_middleware(_RequestTimeoutMiddleware)
app.add_middleware(_InteractiveActivityMiddleware)
app.add_middleware(_SlowRequestLogMiddleware)

# ========= AUTH =========
from routes.google_oauth_routes import router as google_oauth_router
from routes.auth_routes import setup_auth_routes, SESSION_COOKIE

auth_manager = AuthManager()
app.state.auth_manager = auth_manager
AUTH_ENABLED = os.getenv("AUTH_ENABLED", "true").lower() != "false"
LOCALHOST_BYPASS = os.getenv("LOCALHOST_BYPASS", "false").lower() == "true"
if LOCALHOST_BYPASS:
    logger.warning("LOCALHOST_BYPASS is enabled, loopback requests bypass authentication. Do not expose this instance to a network.")

if AUTH_ENABLED:
    AUTH_EXEMPT_EXACT = {
        "/api/auth/setup",
        "/api/auth/signup",
        "/api/auth/login",
        "/api/auth/logout",
        "/api/auth/status",
        "/api/auth/features",
        "/api/auth/settings",
        "/api/auth/integrations/presets",
        "/api/health",
        "/api/version",
        "/login",
    }
    AUTH_EXEMPT_PREFIXES = ["/static"]
    # Dynamic paths whose own handler proves identity via a path-embedded
    # secret instead of the session/bearer auth. The route handler at
    # routes/task_routes.py validates the per-task `webhook_token` itself
    # and returns 404 on mismatch, so the path is the credential — the
    # UI labels these URLs "no auth needed" precisely because external
    # callers (Zapier, n8n, curl) can't supply a session cookie. Without
    # this exemption AuthMiddleware rejects every POST with 401 before
    # the token is ever checked.
    import re as _re
    AUTH_EXEMPT_PATTERNS = [
        _re.compile(r"^/api/tasks/[^/]+/webhook/[^/]+/?$"),
    ]
    AUTH_EXEMPT_EXACT = tuple(list(AUTH_EXEMPT_EXACT) + [
        "/api/auth/integrations/google-drive/callback",
    ])

    def _is_auth_exempt(path: str) -> bool:
        if path in AUTH_EXEMPT_EXACT:
            return True
        if any(path.startswith(p) for p in AUTH_EXEMPT_PREFIXES):
            return True
        return any(p.match(path) for p in AUTH_EXEMPT_PATTERNS)

    # In-memory token cache: prefix → list[(token_id, token_hash, owner, scopes)]. The DB
    # query was running on every API-bearer request and scanning bcrypt
    # checks linearly. With this cache, we hit the DB only when the cache
    # version bumps (token created/revoked) — see _token_cache_invalidate
    # in app.state, called by routes/api_token_routes.
    _token_cache: dict = {}
    _token_cache_lock = _asyncio.Lock()
    _token_cache_dirty = True

    def _token_cache_invalidate():
        nonlocal_dict = app.state.__dict__
        nonlocal_dict["_token_cache_dirty"] = True
    app.state.invalidate_token_cache = _token_cache_invalidate
    app.state._token_cache = _token_cache
    app.state._token_cache_dirty = True

    def _refresh_token_cache():
        """Rebuild the prefix→[(id,hash)] map from the DB."""
        from collections import defaultdict
        new_map = defaultdict(list)
        db = SessionLocal()
        try:
            rows = db.query(ApiToken).filter(ApiToken.is_active == True).all()
            for r in rows:
                owner_key = normalize_known_username(auth_manager.users, getattr(r, "owner", None))
                if not owner_key:
                    logger.warning(
                        "Ignoring active API token '%s' for unknown auth user '%s'",
                        getattr(r, "id", ""),
                        getattr(r, "owner", None),
                    )
                    continue
                scopes = [s.strip() for s in (getattr(r, "scopes", "") or "chat").split(",") if s.strip()]
                new_map[r.token_prefix].append((r.id, r.token_hash, owner_key, scopes))
        finally:
            db.close()
        _token_cache.clear()
        _token_cache.update(new_map)
        app.state._token_cache_dirty = False

    class AuthMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next):
            path = request.url.path
            # A genuine CORS preflight (OPTIONS + Access-Control-Request-Method)
            # carries no credentials by design and must reach CORSMiddleware to be
            # answered. AuthMiddleware is the outermost middleware, so gating the
            # preflight on auth 401s it before CORS can respond -- which blocks
            # every cross-origin browser/WebView client before the real request
            # is sent. Let real preflights through (only OPTIONS w/ the ACRM
            # header; never a credentialed request).
            if is_cors_preflight(request.method, request.headers):
                return await call_next(request)
            if _is_auth_exempt(path):
                return await call_next(request)
            # In-process internal-tool token bypass. Used by the agent
            # tool layer when it HTTP-loopbacks to admin-gated routes
            # (no admin cookie available in that context). Restricted to
            # loopback clients + matching token to keep it locked down.
            try:
                from src.internal_tool_auth import audit_internal_tool_request
                # Impersonation: when the agent's loopback call sets
                # X-Odysseus-Owner, attribute the request to that user only
                # if they exist. Authorization checks remain separate; this
                # is just owner attribution for notes/calendar/etc.
                _auth_mgr = getattr(request.app.state, "auth_manager", None) or auth_manager
                _intercepted = audit_internal_tool_request(
                    request,
                    getattr(_auth_mgr, "users", {}),
                )
                if _intercepted is not None and _intercepted.get("granted"):
                    request.state.current_user = _intercepted["user"]
                    request.state.api_token = False
                    return await call_next(request)
            except Exception as _e:
                logger.warning("Internal tool auth header check failed", exc_info=_e)
            # Allow DIRECT localhost requests (internal service calls from
            # heartbeats etc.). Tunnel/proxy-forwarded requests are excluded by
            # is_trusted_loopback so LOCALHOST_BYPASS can't be abused over a
            # Cloudflare tunnel / reverse proxy. Keep LOCALHOST_BYPASS=false for
            # network-exposed deployments regardless.
            if LOCALHOST_BYPASS and is_trusted_loopback(request):
                return await call_next(request)
            if not auth_manager.is_configured:
                # No users yet — redirect to login for first-time setup
                if not path.startswith("/api/"):
                    return RedirectResponse(url="/login", status_code=302)
                return JSONResponse(status_code=401, content={"error": "Setup required"})

            # --- Bearer token auth (API tokens for external integrations) ---
            auth_header = request.headers.get("authorization", "")
            if auth_header.startswith("Bearer ody_"):
                raw_token = auth_header[7:]
                # Sanity check: tokens are "ody_" + 43 chars of base64
                if len(raw_token) < 12 or len(raw_token) > 100:
                    return JSONResponse(status_code=401, content={"error": "Invalid API token"})
                prefix = raw_token[:8]
                try:
                    if app.state._token_cache_dirty:
                        async with _token_cache_lock:
                            if app.state._token_cache_dirty:
                                await _asyncio.to_thread(_refresh_token_cache)
                    candidates = list(_token_cache.get(prefix, ()))
                    matched_id = None
                    matched_owner = None
                    matched_scopes = []
                    for tid, thash, owner, scopes in candidates:
                        if _bcrypt.checkpw(raw_token.encode(), thash.encode()):
                            matched_id = tid
                            matched_owner = owner
                            matched_scopes = scopes or []
                            break
                    if matched_id:
                        # Update last_used_at off the hot path. Doing it
                        # inline used to keep the request open across an
                        # extra commit; do it fire-and-forget instead.
                        async def _touch_last_used(tid: str):
                            def _do():
                                _db = SessionLocal()
                                try:
                                    _db.query(ApiToken).filter(ApiToken.id == tid).update(
                                        {"last_used_at": datetime.utcnow()}
                                    )
                                    _db.commit()
                                finally:
                                    _db.close()
                            try:
                                await _asyncio.to_thread(_do)
                            except Exception as _e:
                                logger.debug("Failed to update token last_used_at", exc_info=_e)
                        _asyncio.create_task(_touch_last_used(matched_id))
                        # Keep bearer-token callers out of normal cookie/user
                        request.state.current_user = "api"
                        request.state.api_token = True
                        request.state.api_token_id = matched_id
                        request.state.api_token_owner = matched_owner
                        request.state.api_token_scopes = matched_scopes
                        return await call_next(request)
                except Exception:
                    logger.warning("API token auth error", exc_info=False)
                # Invalid bearer token — reject immediately
                return JSONResponse(status_code=401, content={"error": "Invalid API token"})

            # --- Cookie-based session auth ---
            token = request.cookies.get(SESSION_COOKIE)
            if not auth_manager.validate_token(token):
                if path.startswith("/api/"):
                    return JSONResponse(status_code=401, content={"error": "Not authenticated"})
                return RedirectResponse(url="/login", status_code=302)

            # Attach current username to request state for downstream routes
            request.state.current_user = auth_manager.get_username_for_token(token)
            request.state.api_token = False
            return await call_next(request)

    app.add_middleware(AuthMiddleware)
    logger.info("Auth middleware enabled (AUTH_ENABLED=true)")
else:
    logger.info("Auth middleware disabled (set AUTH_ENABLED=true to enable)")

# Added after authentication so this middleware is the outer correlation
# boundary and its request ID is available to downstream middleware and routes.
app.add_middleware(RequestIdMiddleware)

# ========= STATIC FILES =========
os.makedirs(STATIC_DIR, exist_ok=True)


class _RevalidatingStatic(StaticFiles):
    """Serve static assets normally, but force the browser to REVALIDATE
    source files (.js/.css/.html) on every load instead of serving a stale
    copy from disk cache. The app ships raw ES modules with no build step or
    versioned URLs, so browsers were caching modules across deploys — a code
    change wouldn't appear without a manual hard-refresh. `no-cache` keeps the
    cached bytes but requires a conditional request; unchanged files still
    return a cheap 304 (ETag/Last-Modified are preserved)."""

    async def get_response(self, path, scope):
        resp = await super().get_response(path, scope)
        if path.endswith((".js", ".css", ".html")):
            resp.headers["Cache-Control"] = "no-cache"
        return resp


app.mount("/static", _RevalidatingStatic(directory=STATIC_DIR), name="static")

# ========= GENERATED IMAGES =========
@app.get("/api/generated-image/{filename}")
async def serve_generated_image(filename: str, request: Request):
    """Serve generated images from the data directory."""
    img_path = resolve_generated_image_path(filename)
    # SECURITY: filename is the only key, so anyone who knows / guesses a
    # 12-hex content hash could pull another user's image bytes. Require
    # auth and verify ownership via the gallery row (when one exists).
    try:
        from src.auth_helpers import get_current_user
        from core.database import SessionLocal as _SL, GalleryImage as _GI
        _user = get_current_user(request)
        if _user:
            _db = _SL()
            try:
                _row = _db.query(_GI).filter(_GI.filename == filename).first()
                # Generated-but-not-yet-imported images have no row → allow.
                # Row exists with a different owner → 404 (don't confirm existence).
                if _row is not None and _row.owner and _row.owner != _user:
                    raise HTTPException(status_code=404, detail="Image not found")
            finally:
                _db.close()
    except HTTPException:
        raise
    except Exception as _e:
        logger.warning("Image ownership verification failed for %r", filename, exc_info=_e)
    ext = filename.rsplit('.', 1)[-1].lower()
    mime = {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "webp": "image/webp", "gif": "image/gif",
        "mp4": "video/mp4", "mov": "video/quicktime", "webm": "video/webm",
        "mkv": "video/x-matroska", "m4v": "video/mp4",
    }.get(ext, "application/octet-stream")
    # Generated-image filenames are content hashes → the bytes for a given
    # filename never change. Cache them hard so the gallery doesn't
    # re-download every full-size image each time it's opened. `immutable`
    # tells the browser it never needs to revalidate within the max-age.
    return FileResponse(
        str(img_path),
        media_type=mime,
        headers=GENERATED_IMAGE_HEADERS,
    )

# ========= YOUTUBE INIT =========
from services.youtube import init_youtube
init_youtube()

# ========= RAG (vector document RAG) =========
# VectorRAG (ChromaDB-backed personal-document semantic search). Initialized
# lazily via get_rag_manager() — returns None if ChromaDB isn't reachable
# (no server running on the configured host:port), in which case personal-doc
# routes return a clean 503 instead of busy-retrying every request.
#
# Note: this was previously hardcoded off because chromadb 1.4.1 / pydantic
# 2.12 were mutually incompatible at the time. With the current pins
# (chromadb 1.5.x + pydantic 2.13.x) the init works and Personal Docs
# (POST /api/personal/add_directory etc.) is functional again.
from src.rag_singleton import get_rag_manager
rag_manager = get_rag_manager()
rag_available = rag_manager is not None
if rag_available:
    logger.info("Vector document RAG initialized")
else:
    logger.info(
        "Vector document RAG not available at startup "
        "(ChromaDB may not be reachable yet — routes will retry lazily)"
    )

# ========= IMPORT CONFIG =========
from src.config import config

# ========= COMPONENT INITIALIZATION =========
from src.app_initializer import initialize_managers

components = initialize_managers(BASE_DIR, rag_manager)

session_manager   = components["session_manager"]
from src.assistant_log import set_session_manager as _set_asst_sm
_set_asst_sm(session_manager)
# Set the global session manager singleton (used by core.models.Session.add_message)
from core.models import set_session_manager_instance
set_session_manager_instance(session_manager)
app.state.session_manager = session_manager
memory_manager    = components["memory_manager"]
memory_vector     = components.get("memory_vector")
upload_handler    = components["upload_handler"]
app.state.upload_handler = upload_handler
personal_docs_mgr = components["personal_docs_manager"]
app.state.personal_docs_manager = personal_docs_mgr
api_key_manager   = components["api_key_manager"]
preset_manager    = components["preset_manager"]
chat_processor    = components["chat_processor"]
research_handler  = components["research_handler"]
app.state.research_handler = research_handler
chat_handler      = components["chat_handler"]
model_discovery   = components["model_discovery"]
skills_manager    = components["skills_manager"]

# Additional Managers consolidated for Registry
from src.task_scheduler import TaskScheduler
task_scheduler = TaskScheduler(session_manager)
from src.event_bus import set_task_scheduler
set_task_scheduler(task_scheduler)

from src.mcp_manager import McpManager
mcp_manager = McpManager()
from src.agent_tools import set_mcp_manager
set_mcp_manager(mcp_manager)

from src.services.google_drive_organizer_service import GoogleDriveOrganizerService
from src.integrations import get_integration
class _GoogleDriveIntegrationsStoreAdapter:
    def get_integration(self, owner_id=None, integration_id=None):
        return get_integration(integration_id)
google_drive_organizer_service = GoogleDriveOrganizerService(
    integrations_store=_GoogleDriveIntegrationsStoreAdapter()
)

# Store critical managers on app.state for lifespan access
app.state.task_scheduler = task_scheduler
app.state.mcp_manager = mcp_manager
app.state.model_discovery = model_discovery
app.state.skills_manager = skills_manager

# TTS
from services.tts import get_tts_service
tts_service = get_tts_service()
logger.info("TTS service initialized (provider managed via admin settings)")

# STT
from services.stt import get_stt_service
stt_service = get_stt_service()
logger.info("STT service initialized (provider managed via settings)")


# Additional Managers consolidated for Registry
# Reuse the task_scheduler, mcp_manager and google_drive_organizer_service
# created above instead of shadowing them with duplicate instances - otherwise
# the lifespan (startup connects / availability watchdog) and the route/agent
# handlers would operate on different manager instances and the admin UI would
# always report MCP servers as disconnected.
from src.event_bus import set_task_scheduler
set_task_scheduler(task_scheduler)
from src.agent_tools import set_mcp_manager
set_mcp_manager(mcp_manager)

# Webhook Manager (must be created before registry)
from src.webhook_manager import WebhookManager
webhook_manager = WebhookManager(api_key_manager=api_key_manager)
app.state.webhook_manager = webhook_manager

# Bundle for the Registry
components.update({
    "task_scheduler": task_scheduler,
    "mcp_manager": mcp_manager,
    "google_drive_organizer_service": google_drive_organizer_service,
    "webhook_manager": webhook_manager,
    "rag_manager": rag_manager,
    "rag_available": rag_available,
    "tts_service": tts_service,
    "stt_service": stt_service,
    "config": config,
    "constants": {
        "REQUEST_TIMEOUT": REQUEST_TIMEOUT, 
        "OPENAI_API_KEY": OPENAI_API_KEY, 
        "SESSIONS_FILE": SESSIONS_FILE
    },
    "auth_manager": auth_manager,
})

# ========= EXCEPTION HANDLERS =========
@app.exception_handler(SessionNotFoundError)
async def session_not_found_handler(request: Request, exc: SessionNotFoundError):
    return JSONResponse(status_code=404, content={"error": "SESSION_NOT_FOUND", "message": str(exc)})

@app.exception_handler(InvalidFileUploadError)
async def invalid_file_upload_handler(request: Request, exc: InvalidFileUploadError):
    return JSONResponse(status_code=400, content={"error": "INVALID_FILE_UPLOAD", "message": str(exc)})

@app.exception_handler(LLMServiceError)
async def llm_service_error_handler(request: Request, exc: LLMServiceError):
    return JSONResponse(status_code=502, content={"error": "LLM_SERVICE_ERROR", "message": str(exc)})

@app.exception_handler(WebSearchError)
async def web_search_error_handler(request: Request, exc: WebSearchError):
    return JSONResponse(status_code=502, content={"error": "WEB_SEARCH_ERROR", "message": str(exc)})

# ========= ROUTER REGISTRATION (via registry) =========
from src.router_registry import register_all_routes
registry_extras = register_all_routes(app, components)
upload_cleanup_func = registry_extras["upload_cleanup_func"]
app.state.upload_cleanup_func = upload_cleanup_func

# Inline route: activity heartbeat (not in registry)
@app.post("/api/activity/heartbeat")
async def activity_heartbeat():
    from src.interactive_gate import mark_browser_activity
    await mark_browser_activity()
    async def _stop_background():
        try:
            await task_scheduler.stop_background_tasks_for_foreground(reason="browser heartbeat")
        except Exception:
            logging.getLogger("app.foreground_gate").debug("heartbeat task stop failed", exc_info=True)
    asyncio.create_task(_stop_background())
    return {"ok": True}

# Non-router singleton initializations
from src.event_bus import set_task_scheduler as _set_ts
_set_ts(task_scheduler)
from src.agent_tools import set_mcp_manager as _set_mm
_set_mm(mcp_manager)
from src.ai_interaction import set_session_manager as set_ai_session_manager, set_memory_manager as set_ai_memory_manager, set_rag_manager as set_ai_rag_manager
set_ai_session_manager(session_manager)
set_ai_memory_manager(memory_manager, memory_vector)
set_ai_rag_manager(rag_manager, personal_docs_mgr)
logger.info("AI interaction tools initialized (session, memory, RAG, UI control)")

# ========= ROOT & TOOL ROUTES =========

@app.get("/")
async def serve_index(request: Request):
    static_path = abs_join(BASE_DIR, "static/index.html")
    if os.path.exists(static_path):
        return serve_html_with_nonce(request, static_path)
    return serve_html_with_nonce(request, abs_join(BASE_DIR, "index.html"))

@app.get("/notes")
async def serve_notes(request: Request):
    return await serve_index(request)

@app.get("/calendar")
async def serve_calendar(request: Request):
    return await serve_index(request)

@app.get("/cookbook")
async def serve_cookbook(request: Request):
    return await serve_index(request)

@app.get("/email")
async def serve_email(request: Request):
    return await serve_index(request)

@app.get("/memory")
async def serve_memory(request: Request):
    return await serve_index(request)

@app.get("/gallery")
async def serve_gallery(request: Request):
    return await serve_index(request)

@app.get("/tasks")
async def serve_tasks(request: Request):
    return await serve_index(request)

@app.get("/library")
async def serve_library(request: Request):
    return await serve_index(request)

@app.get("/backgrounds")
async def serve_backgrounds(request: Request):
    """Sandbox page for prototyping background effects. No auth required."""
    return serve_html_with_nonce(request, abs_join(BASE_DIR, "static/backgrounds.html"))

@app.get("/login")
async def serve_login(request: Request):
    if not AUTH_ENABLED:
        return RedirectResponse(url="/", status_code=302)
    return serve_html_with_nonce(request, abs_join(BASE_DIR, "static/login.html"))

@app.get("/api/version")
async def get_version():
    from core.constants import APP_VERSION
    return {"version": APP_VERSION}

@app.get("/api/health")
async def health_check() -> Dict[str, str]:
    return {"status": "healthy", "timestamp": datetime.now(timezone.utc).isoformat()}

@app.post("/api/client-perf")
async def client_perf(request: Request):
    """Low-volume frontend timing reports for stalls that happen before SSE logs."""
    try:
        data = await request.json()
    except Exception:
        data = {}
    try:
        kind = str(data.get("type") or "client").replace("\n", " ")[:80]
        total_ms = float(data.get("total_ms") or 0)
        stages = data.get("stages") if isinstance(data.get("stages"), list) else []
        stage_txt = " ".join(
            f"{str(s.get('name') or '')[:40]}={float(s.get('delta_ms') or 0):.0f}ms"
            for s in stages[:20]
            if isinstance(s, dict)
        )
        extra = str(data.get("extra") or "").replace("\n", " ")[:200]
        logging.getLogger("app.client_perf").warning(
            "client_perf type=%s total=%.0fms %s%s",
            kind,
            total_ms,
            stage_txt,
            f" extra={extra}" if extra else "",
        )
    except Exception:
        logging.getLogger("app.client_perf").debug("client_perf log failed", exc_info=True)
    return {"ok": True}

@app.get("/api/ready")
async def readiness_check() -> JSONResponse:
    """Readiness / integrity self-check — DB, data dir, local-first storage.
    Unlike /api/health (liveness), this returns 503 unless every critical
    subsystem is whole, so an orchestrator can gate traffic on real readiness.
    """
    from src.readiness import check_readiness
    result = check_readiness()
    return JSONResponse(status_code=200 if result.get("ready") else 503, content=result)

@app.get("/api/runtime")
async def runtime_info() -> Dict[str, object]:
    in_docker = os.path.exists("/.dockerenv")
    if not in_docker:
        try:
            with open("/proc/1/cgroup", "r", encoding="utf-8", errors="ignore") as fh:
                cg = fh.read()
            in_docker = any(marker in cg for marker in ("docker", "containerd", "kubepods"))
        except Exception:
            in_docker = False
    ollama_url = (
        os.getenv("OLLAMA_BASE_URL")
        or os.getenv("OLLAMA_URL")
        or ("http://host.docker.internal:11434/v1" if in_docker else "http://127.0.0.1:11434/v1")
    )
    return {
        "in_docker": in_docker,
        "ollama_base_url": ollama_url,
    }

@app.post("/api/activity/heartbeat")
async def activity_heartbeat():
    from src.interactive_gate import mark_browser_activity
    await mark_browser_activity()
    async def _stop_background():
        try:
            await task_scheduler.stop_background_tasks_for_foreground(reason="browser heartbeat")
        except Exception:
            logging.getLogger("app.foreground_gate").debug("heartbeat task stop failed", exc_info=True)
    asyncio.create_task(_stop_background())
    return {"ok": True}


# ========= LIFECYCLE =========
from src.lifespan import lifespan

app.router.lifespan_context = lifespan




if __name__ == "__main__":
    import uvicorn

    bind_host = os.getenv("APP_BIND", "127.0.0.1")
    bind_port = int(os.getenv("APP_PORT", "7000"))

    uvicorn.run(app, host=bind_host, port=bind_port, log_level="info")
