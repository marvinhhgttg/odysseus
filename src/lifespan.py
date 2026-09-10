import asyncio
import logging
import os
from datetime import datetime, timezone, timedelta
from contextlib import asynccontextmanager
from fastapi import FastAPI

from core.constants import AUTH_FILE, SESSIONS_FILE
from core.database import SessionLocal, Session as _DbSess, ChatMessage as _DbMsg, ScheduledTask
from src.bg_monitor import start_bg_monitor
from src.builtin_mcp import register_builtin_servers
from src.tool_index import get_tool_index
from src.settings import get_setting
from routes.skills_routes import run_scheduled_skill_audit
from src.cookbook_serve_lifecycle import cookbook_serve_lifecycle_loop
from src.services.google_oauth_maintenance import maintenance_loop as _oauth_sweep
from src.services.google_oauth_service import refresh_access_token as _google_refresh
from src.integrations import load_integrations as _load_integrations
from src.task_supervisor import TaskSupervisor, TaskSpec

logger = logging.getLogger("app.lifespan")

SUPERVISOR_TICK_S = 10.0

async def startup_event(app: FastAPI):
    """Handles the application startup sequence."""
    # Get managers from state or components (they should be on app.state)
    webhook_manager = getattr(app.state, "webhook_manager", None)
    mcp_manager = getattr(app.state, "mcp_manager", None)
    model_discovery = getattr(app.state, "model_discovery", None)
    task_scheduler = getattr(app.state, "task_scheduler", None)
    skills_manager = getattr(app.state, "skills_manager", None)
    upload_cleanup_func = getattr(app.state, "upload_cleanup_func", None)

    logger.info("Application starting up...")
    
    if webhook_manager:
        webhook_manager.set_loop(asyncio.get_running_loop())

    # 1. Incognito Session Purge
    try:
        _db = SessionLocal()
        try:
            _ghosts = _db.query(_DbSess).filter(_DbSess.name.in_(("Nobody", "Incognito"))).all()
            for _g in _ghosts:
                _db.query(_DbMsg).filter(_DbMsg.session_id == _g.id).delete()
                _db.delete(_g)
            if _ghosts:
                _db.commit()
                logger.info(f"Purged {len(_ghosts)} leftover incognito session(s)")
        finally:
            _db.close()
    except Exception as e:
        logger.debug(f"Incognito purge skipped: {e}")

    # 2. Task Supervisor — register every long-running background task
    _supervisor = TaskSupervisor(tick=SUPERVISOR_TICK_S)
    app.state.task_supervisor = _supervisor

    # 2a. Background Job Monitor (completion → agent follow-up)
    if upload_cleanup_func:
        _supervisor.register(TaskSpec(
            "upload_cleanup", upload_cleanup_func,
            description="Hourly upload rate-limit / stale-file cleanup",
            restart=True, max_restarts=5, cooldown=15.0,
        ))
    _supervisor.register(TaskSpec(
        "bg_monitor", lambda: start_bg_monitor(),
        description="Background-job completion monitor (agent follow-ups)",
        restart=True, max_restarts=5, cooldown=15.0,
    ))

    # 2b. MCP Connections (one-shot at startup)
    async def _startup_mcp_connections():
        if not mcp_manager: return
        try:
            await register_builtin_servers(mcp_manager)
        except BaseException as e:
            logger.warning(f"Built-in MCP registration failed (non-critical): {type(e).__name__}: {e}")
        try:
            await asyncio.wait_for(mcp_manager.connect_all_enabled(), timeout=20)
        except asyncio.TimeoutError:
            logger.warning("User MCP startup timed out (non-critical)")
        except BaseException as e:
            logger.warning(f"MCP startup failed (non-critical): {e}")

    _supervisor.register(TaskSpec(
        "mcp_connections", _startup_mcp_connections,
        description="Register built-in + connect enabled MCP servers",
        restart=False,
    ))

    # 2c. Warmups (one-shot, opt-in via ODYSSEUS_STARTUP_WARMUPS=1)
    _startup_warmups_enabled = str(os.getenv("ODYSSEUS_STARTUP_WARMUPS", "")).lower() in {"1", "true", "yes", "on"}
    if _startup_warmups_enabled:
        async def _warmup_tool_index():
            try:
                idx = await asyncio.to_thread(get_tool_index)
                if idx:
                    await asyncio.to_thread(idx.get_tools_for_query, "warmup", 8)
                    logger.info("[startup] Tool index pre-warmed")
            except Exception as e:
                logger.warning(f"Tool index warmup failed (non-critical): {type(e).__name__}: {e}")

        async def _warmup_endpoints():
            if not model_discovery: return
            try:
                import httpx
                urls = await asyncio.to_thread(model_discovery.warmup_ping_urls) if model_discovery else []
                for url in urls:
                    try:
                        async with httpx.AsyncClient(timeout=5.0) as client:
                            await client.get(url)
                        logger.info(f"Warmup ping OK: {url}")
                    except Exception as e:
                        logger.debug(f"Warmup ping failed for endpoint: {e}")
            except Exception as e:
                logger.debug(f"Warmup ping skipped: {e}")

        _supervisor.register(TaskSpec(
            "warmup_tool_index", _warmup_tool_index,
            description="Pre-warm the tool embedding index",
            restart=False,
        ))
        _supervisor.register(TaskSpec(
            "warmup_endpoints", _warmup_endpoints,
            description="Ping configured model endpoints to warm them up",
            restart=False,
        ))
    else:
        logger.info("Startup warmups disabled (set ODYSSEUS_STARTUP_WARMUPS=1 to enable)")

    # 2d. Keep-alive Loop (opt-in via ODYSSEUS_MODEL_KEEPALIVE=1)
    _keepalive_enabled = str(os.getenv("ODYSSEUS_MODEL_KEEPALIVE", "")).lower() in {"1", "true", "yes", "on"}
    if _keepalive_enabled:
        async def _keepalive_loop():
            while True:
                try:
                    await asyncio.sleep(60)
                    await _warmup_endpoints_logic(model_discovery)
                except Exception as e:
                    logger.warning(f"Keepalive loop error: {e}")
                    await asyncio.sleep(300)

        _supervisor.register(TaskSpec(
            "keepalive_loop", _keepalive_loop,
            description="Periodic model-endpoint keepalive pings",
            restart=True, max_restarts=5, cooldown=30.0,
        ))

    # 3. Default Task Reconciliation (awaited once at startup)
    async def _ensure_default_tasks():
        if not task_scheduler: return
        owners = set()
        try:
            import json as _json
            with open(AUTH_FILE, encoding="utf-8") as f:
                users = _json.load(f).get("users", {})
            owners.update(users.keys())
        except Exception as e:
            logger.debug(f"Default task auth-owner scan: {e}")

        try:
            from src.task_scheduler import HOUSEKEEPING_DEFAULTS
            builtin_names = []
            for defs in HOUSEKEEPING_DEFAULTS.values():
                builtin_names.append(defs["name"])
                builtin_names.extend(defs.get("legacy_names") or [])
            db_seed = SessionLocal()
            try:
                rows = db_seed.query(ScheduledTask.owner).filter(
                    (ScheduledTask.action.in_(list(HOUSEKEEPING_DEFAULTS.keys())))
                    | (ScheduledTask.name.in_(builtin_names))
                ).distinct().all()
                owners.update(row[0] for row in rows if row[0])
            finally:
                db_seed.close()
        except Exception as e:
            logger.debug(f"Default task existing-owner scan: {e}")

        try:
            for uname in sorted(owners):
                try:
                    await task_scheduler.ensure_defaults(uname)
                except Exception as e:
                    logger.debug(f"ensure_defaults({uname}): {e}")
        except Exception as e:
            logger.debug(f"Default tasks: {e}")

    await _ensure_default_tasks()

    # 4. Skill Owner Backfill
    try:
        import json as _json
        with open(AUTH_FILE, encoding="utf-8") as f:
            users = _json.load(f).get("users", {})
        primary_owner = None
        for uname, udata in users.items():
            if udata.get("is_admin") is True:
                primary_owner = uname
                break
        if not primary_owner and users:
            primary_owner = next(iter(users))
        if primary_owner and skills_manager:
            changed = skills_manager.backfill_owner(primary_owner, set(users.keys()))
            if changed:
                logger.info("Assigned %s legacy skill file(s) to %s", changed, primary_owner)
    except Exception as e:
        logger.debug(f"Skill owner backfill skipped: {e}")

    # 5. In-process Task Runner
    _tasks_inprocess = os.environ.get("ODYSSEUS_INPROCESS_TASKS", "1").strip().lower()
    if _tasks_inprocess not in ("0", "false", "no", "off", ""):
        if task_scheduler:
            await task_scheduler.start()
    else:
        logger.info("In-process task scheduler disabled (ODYSSEUS_INPROCESS_TASKS=0)")

    # 6. Remaining supervised long-running loops
    async def _null_owner_sweep_loop():
        while True:
            try:
                await asyncio.sleep(3600)
                from core.database import _migrate_assign_legacy_owner
                await asyncio.to_thread(_migrate_assign_legacy_owner)
            except Exception as e:
                logger.debug(f"Null-owner sweep skipped: {e}")
                await asyncio.sleep(3600)

    _supervisor.register(TaskSpec(
        "null_owner_sweep", _null_owner_sweep_loop,
        description="Hourly legacy null-owner row backfill",
        restart=True, max_restarts=5, cooldown=60.0,
    ))

    async def _skill_audit_nightly_loop():
        while True:
            try:
                hour = int(get_setting("skill_audit_hour", 2) or 2)
            except Exception:
                hour = 2
            now = datetime.now()
            nxt = now.replace(hour=hour % 24, minute=0, second=0, microsecond=0)
            if nxt <= now:
                nxt += timedelta(days=1)
            await asyncio.sleep(max(60, (nxt - now).total_seconds()))
            try:
                if not get_setting("skill_audit_nightly", True):
                    continue
                batch = int(get_setting("skill_audit_batch", 8) or 8)
                if skills_manager:
                    await run_scheduled_skill_audit(skills_manager, owner=None, max_skills=batch)
            except Exception as e:
                logger.warning(f"Nightly skill audit failed: {e}")

    _supervisor.register(TaskSpec(
        "skill_audit_nightly", _skill_audit_nightly_loop,
        description="Nightly skill audit (configurable hour/batch)",
        restart=True, max_restarts=5, cooldown=60.0,
    ))

    try:
        cookbook_serve_lifecycle_loop
        _supervisor.register(TaskSpec(
            "cookbook_serve_lifecycle", cookbook_serve_lifecycle_loop,
            description="Manage cookbook 'serve' endpoints lifecycle",
            restart=True, max_restarts=5, cooldown=30.0,
        ))
    except Exception as e:
        logger.debug(f"Cookbook serve lifecycle loop failed to start: {e}")

    try:
        _supervisor.register(TaskSpec(
            "google_oauth_refresh", lambda: _oauth_sweep(
                load_integrations=_load_integrations,
                refresh=_google_refresh,
            ),
            description="Maintain Google OAuth token refresh",
            restart=True, max_restarts=5, cooldown=60.0,
        ))
    except Exception as e:
        logger.warning(f"drive-oauth maintenance loop not started: {e}")

    # 7. Start everything supervised
    await _supervisor.start_all()
    logger.info("Application startup complete")

async def _warmup_endpoints_logic(model_discovery):
    """Logic extracted for keepalive use."""
    if not model_discovery: return
    try:
        import httpx
        urls = await asyncio.to_thread(model_discovery.warmup_ping_urls) if model_discovery else []
        for url in urls:
            try:
                async with httpx.AsyncClient(timeout=5.0) as client:
                    await client.get(url)
                logger.info(f"Warmup ping OK: {url}")
            except Exception as e:
                logger.debug(f"Warmup ping failed for endpoint: {e}")
    except Exception as e:
        logger.debug(f"Warmup ping skipped: {e}")

async def shutdown_event(app: FastAPI):
    """Handles the application shutdown sequence."""
    logger.info("Application shutting down...")
    
    # Stop supervised background tasks (watchdog + all registered loops).
    # Give in-flight one-shot work (e.g. MCP connects) a moment to finish
    # cleanly before cancelling the rest.
    supervisor = getattr(app.state, "task_supervisor", None)
    if supervisor:
        try:
            await supervisor.stop_all(grace=5.0)
        except Exception as e:
            logger.warning(f"Task supervisor shutdown error: {e}")

    # Stop task scheduler
    task_scheduler = getattr(app.state, "task_scheduler", None)
    if task_scheduler:
        try:
            await task_scheduler.stop()
        except Exception:
            pass
    
    # Close webhook manager
    webhook_manager = getattr(app.state, "webhook_manager", None)
    if webhook_manager:
        try:
            await webhook_manager.close()
        except Exception as e:
            logger.warning(f"Webhook manager shutdown error: {e}")

    # Disconnect all MCP servers
    mcp_manager = getattr(app.state, "mcp_manager", None)
    if mcp_manager:
        try:
            await mcp_manager.disconnect_all()
        except Exception as e:
            logger.warning(f"MCP shutdown error: {e}")
            
    logger.info("Application shutdown complete")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI Lifespan context manager."""
    await startup_event(app)
    yield
    await shutdown_event(app)
