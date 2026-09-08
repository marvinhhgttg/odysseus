"""Background maintenance for Google OAuth integrations.

Google Drive (and other Google integrations) store an access_token with an
`oauth_expires_at` timestamp plus a long-lived refresh_token. `ensure_fresh_google_token`
already refreshes reactively when a request comes in within 5 minutes of expiry,
but that only helps while the endpoint is being called. If Odysseus sits idle
for several days, the access_token silently expires and the *next* user request
sees an "unexpected" 4xx before the reactive refresh kicks in.

This module adds a proactive sweep: every scheduled run, list all Google OAuth
integrations, and refresh any whose token expires within a configurable
warning window (default 24 h). Idempotent, side-effect only via the existing
`refresh_access_token` writer.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

# Providers whose refresh flow uses `refresh_access_token`. Kept explicit so
# adding another Google-family provider is a conscious change.
_GOOGLE_OAUTH_PROVIDERS = frozenset({"google_drive"})

DEFAULT_WARNING_WINDOW = timedelta(hours=24)
DEFAULT_SWEEP_INTERVAL_SECONDS = 3600  # once per hour


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# In-process last-sweep state, populated by run_sweep() and consumed by the
# /api/health/google-oauth endpoint. This is intentionally process-local and
# not persisted — the endpoint is meant for "is the loop alive and doing
# something useful right now" liveness, not durable audit history. On process
# restart last_sweep_at is None until the first sweep runs (typically ~500 ms
# after startup).
_LAST_SWEEP: Dict[str, Any] = {
    "last_sweep_at": None,        # datetime, UTC
    "last_result": None,          # SweepResult
    "last_refresh_at": {},        # {integration_id: datetime}
}


def sweep_status() -> Dict[str, Any]:
    """Return a JSON-serialisable snapshot of the last sweep, for /health."""
    result = _LAST_SWEEP.get("last_result")
    last_sweep_at = _LAST_SWEEP.get("last_sweep_at")
    return {
        "last_sweep_at": last_sweep_at.isoformat() if last_sweep_at else None,
        "last_result": {
            "checked": result.checked if result else None,
            "refreshed": result.refreshed if result else None,
            "skipped_fresh": result.skipped_fresh if result else None,
            "skipped_no_refresh_token": (
                result.skipped_no_refresh_token if result else None
            ),
            "errors": list(result.errors) if result else None,
        } if result else None,
        "last_refresh_at": {
            k: v.isoformat() for k, v in _LAST_SWEEP.get("last_refresh_at", {}).items()
        },
    }


def _reset_state_for_tests() -> None:
    """Test-only helper: clear the module-level sweep state between tests."""
    _LAST_SWEEP["last_sweep_at"] = None
    _LAST_SWEEP["last_result"] = None
    _LAST_SWEEP["last_refresh_at"] = {}


@dataclass
class SweepResult:
    """Summary of one maintenance sweep. Intended for logging + tests."""

    checked: int = 0
    refreshed: int = 0
    skipped_fresh: int = 0
    skipped_no_refresh_token: int = 0
    errors: List[str] = field(default_factory=list)

    def as_log_line(self) -> str:
        return (
            f"drive-oauth sweep: checked={self.checked} refreshed={self.refreshed} "
            f"skipped_fresh={self.skipped_fresh} "
            f"skipped_no_refresh_token={self.skipped_no_refresh_token} "
            f"errors={len(self.errors)}"
        )


def _parse_expiry(raw: Any) -> Optional[datetime]:
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def needs_refresh(
    integration: Dict[str, Any],
    *,
    now: Optional[datetime] = None,
    warning_window: timedelta = DEFAULT_WARNING_WINDOW,
) -> bool:
    """Return True when the integration's access token expires inside the window.

    Missing `oauth_expires_at` is treated as "needs refresh" because the writer
    always populates it after a successful refresh; a missing value means the
    row is stale or was written by an older code path.
    """
    now = now or _utcnow()
    expiry = _parse_expiry(integration.get("oauth_expires_at"))
    if expiry is None:
        return True
    return expiry <= now + warning_window


def _has_refresh_token(integration: Dict[str, Any]) -> bool:
    settings = integration.get("settings") or {}
    return bool(integration.get("oauth_refresh_token") or settings.get("refresh_token"))


def _is_google_oauth(integration: Dict[str, Any]) -> bool:
    provider = str(integration.get("provider") or "").lower()
    preset = str(integration.get("preset") or "").lower()
    return provider in _GOOGLE_OAUTH_PROVIDERS or preset in _GOOGLE_OAUTH_PROVIDERS


async def run_sweep(
    *,
    load_integrations: Callable[[], List[Dict[str, Any]]],
    refresh: Callable[[Dict[str, Any]], "asyncio.Future[Dict[str, Any]]"],
    now: Optional[datetime] = None,
    warning_window: timedelta = DEFAULT_WARNING_WINDOW,
) -> SweepResult:
    """Refresh Google OAuth integrations whose token expires within window.

    Dependencies are injected so this is trivially unit-testable without
    reaching into the real integrations file or hitting oauth2.googleapis.com.
    """
    now = now or _utcnow()
    result = SweepResult()

    try:
        items = load_integrations() or []
    except Exception as exc:
        result.errors.append(f"load_integrations: {exc}")
        logger.exception("drive-oauth sweep: failed to list integrations")
        return result

    for integration in items:
        if not isinstance(integration, dict) or not integration.get("enabled", True):
            continue
        if not _is_google_oauth(integration):
            continue

        result.checked += 1
        integration_id = str(integration.get("id") or "?")

        if not needs_refresh(integration, now=now, warning_window=warning_window):
            result.skipped_fresh += 1
            continue

        if not _has_refresh_token(integration):
            result.skipped_no_refresh_token += 1
            logger.warning(
                "drive-oauth sweep: integration %s expires soon but has no "
                "refresh_token; reconnect required.",
                integration_id,
            )
            continue

        try:
            await refresh(integration)
            result.refreshed += 1
            _LAST_SWEEP["last_refresh_at"][integration_id] = now
            logger.info("drive-oauth sweep: refreshed integration %s", integration_id)
        except Exception as exc:
            msg = f"{integration_id}: {exc}"
            result.errors.append(msg)
            logger.exception("drive-oauth sweep: refresh failed for %s", integration_id)

    _LAST_SWEEP["last_sweep_at"] = now
    _LAST_SWEEP["last_result"] = result
    logger.info(result.as_log_line())
    return result


async def maintenance_loop(
    *,
    load_integrations: Callable[[], List[Dict[str, Any]]],
    refresh: Callable[[Dict[str, Any]], "asyncio.Future[Dict[str, Any]]"],
    interval_seconds: int = DEFAULT_SWEEP_INTERVAL_SECONDS,
    warning_window: timedelta = DEFAULT_WARNING_WINDOW,
    stop_event: Optional[asyncio.Event] = None,
) -> None:
    """Run `run_sweep` on a fixed interval until `stop_event` fires or task is
    cancelled. Errors inside a single sweep are logged and swallowed; the loop
    itself never dies from a transient network or file failure.
    """
    logger.info(
        "drive-oauth maintenance loop starting: interval=%ss warning_window=%s",
        interval_seconds,
        warning_window,
    )
    while True:
        try:
            await run_sweep(
                load_integrations=load_integrations,
                refresh=refresh,
                warning_window=warning_window,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("drive-oauth sweep raised; continuing loop")

        if stop_event is not None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
                return  # stop_event fired
            except asyncio.TimeoutError:
                continue
        else:
            await asyncio.sleep(interval_seconds)
