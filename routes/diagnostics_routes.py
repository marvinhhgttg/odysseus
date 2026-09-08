"""Diagnostics routes — /api/db/stats, /api/rag/stats, /api/test/youtube, /api/test-research."""

import logging
import os
from datetime import datetime, timezone
from typing import Dict, Any, List

from fastapi import APIRouter, HTTPException, Form, Request

from services.youtube.youtube_handler import extract_youtube_id, extract_transcript_async
from core.constants import DEFAULT_HOST, DATA_DIR
from core.middleware import require_admin

logger = logging.getLogger(__name__)


def _google_oauth_integration_status(
    integration: Dict[str, Any],
    *,
    now: datetime,
    last_refresh_at_map: Dict[str, str],
) -> Dict[str, Any]:
    """Per-integration OAuth health snapshot for the /health endpoint.

    Never returns tokens or secrets — only booleans, timestamps, and the
    connected email. Callers still need admin auth (require_admin) upstream.
    """
    integration_id = str(integration.get("id") or "?")
    settings = integration.get("settings") or {}
    expires_at_raw = integration.get("oauth_expires_at")

    expires_at_dt = None
    if expires_at_raw:
        try:
            parsed = datetime.fromisoformat(str(expires_at_raw))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            expires_at_dt = parsed
        except (TypeError, ValueError):
            expires_at_dt = None

    seconds_until_expiry = None
    is_expired = None
    if expires_at_dt is not None:
        delta = (expires_at_dt - now).total_seconds()
        seconds_until_expiry = int(delta)
        is_expired = delta <= 0

    return {
        "integration_id": integration_id,
        "provider": integration.get("provider"),
        "enabled": bool(integration.get("enabled", True)),
        "connected_email": integration.get("oauth_connected_email", ""),
        "expires_at": expires_at_dt.isoformat() if expires_at_dt else None,
        "seconds_until_expiry": seconds_until_expiry,
        "is_expired": is_expired,
        "has_refresh_token": bool(
            integration.get("oauth_refresh_token") or settings.get("refresh_token")
        ),
        "last_refresh_at": last_refresh_at_map.get(integration_id),
    }


def setup_diagnostics_routes(
    rag_manager,
    rag_available: bool,
    research_handler,
    memory_vector=None,
) -> APIRouter:
    router = APIRouter(tags=["diagnostics"])

    @router.get("/api/diagnostics/services")
    async def get_service_health(request: Request) -> Dict[str, Any]:
        """Consolidated degraded-state report for ChromaDB, SearXNG, email,
        ntfy, and provider endpoints. Non-intrusive probes — safe to poll."""
        require_admin(request)
        from src.service_health import collect_service_health
        return await collect_service_health(rag_manager, memory_vector)

    @router.get("/api/health/google-oauth")
    async def get_google_oauth_health(request: Request) -> Dict[str, Any]:
        """Per-integration Google OAuth token health.

        Emits, for each enabled Google integration:
          - integration_id, provider, connected_email
          - expires_at + seconds_until_expiry + is_expired
          - has_refresh_token
          - last_refresh_at (from the in-process maintenance loop state)

        Plus a top-level sweep summary from
        google_oauth_maintenance.sweep_status(): last_sweep_at,
        last_result counts, and any errors.

        Safe to poll from morning briefings and dashboards. Never returns
        tokens, secrets, or IDs beyond the integration_id already visible in
        the UI. Admin-only via require_admin.
        """
        require_admin(request)
        from src.integrations import load_integrations
        from src.services.google_oauth_maintenance import sweep_status

        status = sweep_status()
        now = datetime.now(timezone.utc)

        last_refresh_map = status.get("last_refresh_at") or {}
        integrations_report: List[Dict[str, Any]] = []
        try:
            items = load_integrations() or []
        except Exception as exc:
            logger.exception("google-oauth health: load_integrations failed")
            raise HTTPException(500, f"load_integrations failed: {exc}")

        google_providers = {"google_drive"}
        for integration in items:
            if not isinstance(integration, dict):
                continue
            provider = str(integration.get("provider") or "").lower()
            preset = str(integration.get("preset") or "").lower()
            if provider not in google_providers and preset not in google_providers:
                continue
            integrations_report.append(
                _google_oauth_integration_status(
                    integration, now=now, last_refresh_at_map=last_refresh_map
                )
            )

        # Convenience overall flag for dashboards: "ok" if we have at least
        # one enabled integration that is not expired.
        healthy = any(
            it["enabled"] and it["is_expired"] is False
            for it in integrations_report
        )
        return {
            "status": "ok" if healthy else "degraded",
            "checked_at": now.isoformat(),
            "sweep": status,
            "integrations": integrations_report,
        }

    @router.get("/api/diagnostics/logs")
    async def get_diagnostics_logs(request: Request, limit: int = 200) -> Dict[str, Any]:
        require_admin(request)
        limit = max(1, min(limit, 1000))
        try:
            log_file = os.path.join(DATA_DIR, "logs", "app.log")
            if not os.path.exists(log_file):
                return {"status": "success", "logs": []}

            # Safe tail read of the log file (max 5MB via rotation)
            with open(log_file, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()

            tail_lines = lines[-limit:] if len(lines) > limit else lines
            tail_lines = [line.rstrip('\r\n') for line in tail_lines]

            return {
                "status": "success",
                "logs": tail_lines
            }
        except Exception as e:
            logger.error(f"Diagnostics logs retrieval error: {e}")
            raise HTTPException(500, f"Failed to retrieve logs: {str(e)}")

    @router.get("/api/db/stats")
    async def get_database_stats(request: Request) -> Dict[str, Any]:
        require_admin(request)
        try:
            from core.database import get_detailed_stats
            return get_detailed_stats()
        except Exception as e:
            logger.error(f"DB stats error: {e}")
            raise HTTPException(500, "Failed to retrieve database statistics")

    @router.get("/api/rag/stats")
    async def get_rag_stats(request: Request) -> Dict[str, Any]:
        require_admin(request)
        if rag_available and rag_manager:
            return rag_manager.get_stats()
        return {"error": "RAG system not available"}

    @router.get("/api/test/youtube")
    async def test_youtube(request: Request, url: str) -> Dict[str, Any]:
        require_admin(request)
        try:
            video_id = extract_youtube_id(url)
            if not video_id:
                return {"error": "Invalid YouTube URL"}

            data = await extract_transcript_async(url, video_id)
            return {
                "video_id": video_id,
                "transcript_success": data.get("success", False),
                "transcript_length": len(data.get("transcript", "")) if data.get("success") else 0,
                "transcript_preview": (data.get("transcript", "")[:500] + "...")
                    if data.get("success") and len(data.get("transcript", "")) > 500
                    else data.get("transcript", ""),
                "error": data.get("error") if not data.get("success") else None,
            }
        except Exception as e:
            return {"error": str(e)}

    @router.post("/api/test-research")
    async def test_research(request: Request, query: str = Form("What is machine learning?")) -> Dict[str, Any]:
        require_admin(request)
        try:
            endpoint = f"http://{DEFAULT_HOST}:8000/v1/chat/completions"
            model = "gpt-oss-120b"
            result = await research_handler.call_research_service(query, endpoint, model)
            return {
                "status": "success",
                "query": query,
                "result_preview": result[:200] + "..." if len(result) > 200 else result,
                "result_length": len(result),
            }
        except Exception as e:
            return {"status": "error", "error": str(e), "query": query}

    return router
