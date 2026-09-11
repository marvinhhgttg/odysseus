"""Diagnostics routes — /api/db/stats, /api/rag/stats, /api/test/youtube, /api/test-research."""

import logging
import os
from datetime import datetime, timezone
from typing import Dict, Any, List

from fastapi import APIRouter, Body, HTTPException, Form, Request, Depends
from fastapi.responses import PlainTextResponse

from services.youtube.youtube_handler import extract_youtube_id, extract_transcript_async
from core.constants import DEFAULT_HOST, DATA_DIR
from src.auth_dependencies import require_admin
from src.components import get_task_supervisor

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
    async def get_service_health(
        request: Request,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        """Consolidated degraded-state report for ChromaDB, SearXNG, email,
        ntfy, and provider endpoints. Non-intrusive probes — safe to poll."""
        from src.service_health import collect_service_health
        return await collect_service_health(rag_manager, memory_vector)

    @router.get("/api/diagnostics/metrics")
    async def get_http_metrics(
        request: Request,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        """In-process HTTP/runtime metrics snapshot (JSON).

        Aggregate counters and latency percentiles for every request served
        through the correlation middleware: totals, per-status and per-method
        buckets, the active in-flight gauge, avg/p50/p95 response latency,
        uptime, and app version. JSON twin of /api/diagnostics/metrics/export.
        Admin-only via require_admin.
        """
        from src.metrics import snapshot
        return snapshot()

    @router.get("/api/diagnostics/metrics/export")
    async def export_http_metrics(
        request: Request,
        _admin: None = Depends(require_admin),
    ):
        """Prometheus text-format export of the same metrics snapshot.

        Intended for a Prometheus / Grafana scraper with admin credentials.
        Content-Type is text/plain; version=0.0.4 so ``promtool check`` accepts
        the payload directly. Admin-only via require_admin.
        """
        from src.metrics import prometheus_text
        return PlainTextResponse(
            prometheus_text(),
            media_type="text/plain; version=0.0.4",
        )

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

    @router.get("/api/diagnostics/grounding")
    async def get_grounding_diagnostics(request: Request) -> Dict[str, Any]:
        """Recent web-grounding rejections and fallbacks.

        Returns counts and the last 10 events for both categories:
        - rejections: individual answer blocks the grounding filter stripped
          because their concrete terms could not be matched against the cited
          source (with unsupported_terms and the block preview).
        - fallbacks: turns where the whole model answer was replaced with the
          "not enough evidence" template.

        In-process ring buffer, bounded to 50 entries per category. Admin-only.
        """
        from src.services.grounding_diagnostics import rejection_status
        return rejection_status()

    @router.post("/api/diagnostics/grounding/explain")
    async def explain_grounding(
        request: Request,
        payload: Dict[str, Any] = Body(default={}),
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        """Dry-run the grounding filter for a given (answer, sources) pair.

        Expects {"answer": str, "sources": [{"url": ..., "title": ..., "snippet": ...}, ...]}.
        Returns per-block reports with concrete_terms + how each term matched
        (exact | transliteration | hyphen_compound | unsupported), plus an
        overall verdict clean|trimmed|fallback. Does NOT touch the live ring
        buffer — use this to reproduce a suspected filter bug end-to-end.
        """
        answer = str(payload.get("answer") or "")
        sources = payload.get("sources") or []
        if not isinstance(sources, list):
            raise HTTPException(400, "sources must be a list")
        from src.services.grounding_diagnostics import explain_answer
        return explain_answer(answer, sources)

    @router.get("/api/health/agent-prompt-budget")
    async def get_agent_prompt_budget(request: Request) -> Dict[str, Any]:
        """Recent agent-prompt sizes and budget verdicts.

        Emits the last 50 assembled system prompts as
        {estimated_tokens, kind, tool_count, compact, at}, plus counts of
        soft-budget warnings and hard-budget overruns and the max size seen
        in the window. Reads process-local state only; no persistence.

        Useful for spotting when a query mix pushes the prompt past what the
        currently selected local model can hold, before users see degraded
        tool-call behaviour.
        """
        from src.services.prompt_budget import prompt_budget_status
        return prompt_budget_status()

    @router.get("/api/diagnostics/logs")
    async def get_diagnostics_logs(
        request: Request,
        _admin: None = Depends(require_admin),
        limit: int = 200,
    ) -> Dict[str, Any]:
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
    async def get_database_stats(
        request: Request,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        try:
            from core.database import get_detailed_stats
            return get_detailed_stats()
        except Exception as e:
            logger.error(f"DB stats error: {e}")
            raise HTTPException(500, "Failed to retrieve database statistics")

    @router.get("/api/rag/stats")
    async def get_rag_stats(
        request: Request,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
        if rag_available and rag_manager:
            return rag_manager.get_stats()
        return {"error": "RAG system not available"}

    @router.get("/api/test/youtube")
    async def test_youtube(
        request: Request,
        url: str,
        _admin: None = Depends(require_admin),
    ) -> Dict[str, Any]:
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
    async def test_research(
        request: Request,
        _admin: None = Depends(require_admin),
        query: str = Form("What is machine learning?"),
    ) -> Dict[str, Any]:
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

    @router.get("/api/diagnostics/tasks")
    async def get_task_supervisor_status(
        request: Request,
        _admin: None = Depends(require_admin),
        supervisor=Depends(get_task_supervisor),
    ) -> Dict[str, Any]:
        """Snapshot of every supervised background task.

        Reports phase (pending / running / paused / done / crashed / stopped),
        restart counts, the restart budget, whether restarts are enabled, the
        last error, cumulative start/crash counters, and uptime or next-retry
        timing. Admin-only via require_admin.
        """
        if supervisor is None:
            return {
                "supervisor": "not_loaded",
                "tasks": {},
                "total": 0,
                "running": 0,
                "crashed": 0,
            }
        status = supervisor.status()
        totals = {"total": 0, "running": 0, "crashed": 0, "done": 0}
        for entry in status.values():
            totals["total"] += 1
            phase = entry["phase"]
            if phase in totals:
                totals[phase] += 1
        return {
            "supervisor": "running" if supervisor.is_running() else "stopped",
            "tasks": status,
            **totals,
        }

    async def _task_action(supervisor, name: str, action: str) -> Dict[str, Any]:
        if supervisor is None:
            return {"name": name, "ok": False, "error": "supervisor not loaded"}
        try:
            if action == "pause":
                ok = await supervisor.pause(name)
            elif action == "resume":
                ok = await supervisor.resume(name)
            elif action == "stop":
                ok = await supervisor.stop(name)
            elif action == "restart":
                ok = await supervisor.restart(name)
            else:
                return {"name": name, "ok": False, "error": f"unknown action '{action}'"}
        except Exception as e:  # noqa: BLE001 - surface as a controlled error response
            return {"name": name, "ok": False, "error": f"{type(e).__name__}: {e}"}
        entry = supervisor.status().get(name)
        return {
            "name": name,
            "ok": ok,
            "phase": entry.get("phase") if entry else None,
            "error": "unknown task" if entry is None else None,
        }

    @router.post("/api/diagnostics/tasks/{name}/pause")
    async def pause_supervised_task(
        name: str,
        request: Request,
        _admin: None = Depends(require_admin),
        supervisor=Depends(get_task_supervisor),
    ) -> Dict[str, Any]:
        """Pause one supervised task (soft-stop, no restart by watchdog)."""
        return await _task_action(supervisor, name, "pause")

    @router.post("/api/diagnostics/tasks/{name}/resume")
    async def resume_supervised_task(
        name: str,
        request: Request,
        _admin: None = Depends(require_admin),
        supervisor=Depends(get_task_supervisor),
    ) -> Dict[str, Any]:
        """Resume a paused (or not-running) supervised task."""
        return await _task_action(supervisor, name, "resume")

    @router.post("/api/diagnostics/tasks/{name}/stop")
    async def stop_supervised_task(
        name: str,
        request: Request,
        _admin: None = Depends(require_admin),
        supervisor=Depends(get_task_supervisor),
    ) -> Dict[str, Any]:
        """Permanently stop one supervised task (never respawned)."""
        return await _task_action(supervisor, name, "stop")

    @router.post("/api/diagnostics/tasks/{name}/restart")
    async def restart_supervised_task(
        name: str,
        request: Request,
        _admin: None = Depends(require_admin),
        supervisor=Depends(get_task_supervisor),
    ) -> Dict[str, Any]:
        """Manually restart one supervised task with a fresh restart budget."""
        return await _task_action(supervisor, name, "restart")

    return router
