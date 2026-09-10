"""Supervisor for the application's long-running asyncio background tasks.

Every always-on loop the app starts at boot (upload cleanup, background-job
monitor, nightly skill audit, OAuth token refresh, …) is registered here as a
:class:`TaskSpec`. The supervisor then:

* spawns a fresh task for each spec,
* watches every task so a crash is never silent,
* restarts crashed tasks that opt in (``restart=True``) with exponential
  backoff and a per-task restart budget,
* exposes a uniform :meth:`TaskSupervisor.status` snapshot consumed by the
  ``GET /api/diagnostics/tasks`` endpoint,
* and, on shutdown, cancels everything it owns.

A spec's ``factory`` is a zero-arg callable returning an awaitable, so a
respawn always starts from a fresh, unconsumed coroutine.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Dict, Optional

logger = logging.getLogger("app.task_supervisor")


@dataclass
class TaskSpec:
    """Immutable description of one supervised background task."""

    name: str
    factory: Callable[[], Awaitable]
    description: str = ""
    restart: bool = True
    max_restarts: int = 3
    cooldown: float = 10.0
    backoff: float = 2.0
    max_cooldown: float = 300.0


@dataclass
class _TaskRuntime:
    task: Optional[asyncio.Task] = None
    phase: str = "pending"
    restarts: int = 0
    gave_up: bool = False
    last_error: Optional[str] = None
    first_started: Optional[float] = None
    started_at: Optional[float] = None
    finished_at: Optional[float] = None
    next_attempt: Optional[float] = None


class TaskSupervisor:
    """Runs and supervises the app's background tasks."""

    def __init__(self, tick: float = 5.0):
        self._specs: Dict[str, TaskSpec] = {}
        self._runtime: Dict[str, _TaskRuntime] = {}
        self._tick = tick
        self._watchdog: Optional[asyncio.Task] = None
        self._started = False
        self._stopping = False

    # ------------------------------------------------------------------
    # Registration / lifecycle
    # ------------------------------------------------------------------

    def register(self, spec: TaskSpec) -> None:
        """Register a task spec. Registrations are idempotent per name."""
        self._specs[spec.name] = spec
        self._runtime.setdefault(spec.name, _TaskRuntime())

    async def start_all(self) -> None:
        """Spawn every registered task and begin supervision."""
        if self._started:
            return
        self._stopping = False
        for name in self._specs:
            self._spawn(name)
        self._started = True
        self._watchdog = asyncio.create_task(self._watch())
        logger.info("TaskSupervisor started with %d supervised task(s)", len(self._specs))

    async def stop_all(self, grace: float = 0.0) -> None:
        """Cancel the watchdog, then stop every supervised task (idempotent).

        If ``grace`` is positive, running tasks get that many seconds to finish
        naturally (in-flight one-shot work like MCP connects completes cleanly)
        before the remainder are cancelled.
        """
        if not self._started:
            return
        self._stopping = True
        self._started = False
        if self._watchdog:
            self._watchdog.cancel()
            try:
                await self._watchdog
            except (asyncio.CancelledError, Exception):
                pass
            self._watchdog = None
        if grace > 0:
            deadline = time.monotonic() + grace
            while time.monotonic() < deadline:
                pending = [rt for rt in self._runtime.values()
                           if rt.task and not rt.task.done() and rt.phase not in ("stopped",)]
                if not pending:
                    break
                try:
                    done, _ = await asyncio.wait(
                        [rt.task for rt in pending],
                        timeout=deadline - time.monotonic(),
                        return_when=asyncio.ALL_COMPLETED,
                    )
                    if not done:
                        break
                except Exception:
                    break
        for name, rt in self._runtime.items():
            if rt.task and not rt.task.done():
                rt.task.cancel()
                try:
                    await rt.task
                except (asyncio.CancelledError, Exception):
                    pass
            if rt.phase not in ("crashed", "done"):
                rt.phase = "stopped"
        logger.info("TaskSupervisor stopped")

    def is_running(self) -> bool:
        return self._started and not self._stopping

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, dict]:
        """Snapshot of every supervised task for the diagnostics endpoint."""
        now = time.monotonic()
        out: Dict[str, dict] = {}
        for name, spec in self._specs.items():
            rt = self._runtime[name]
            entry = {
                "name": name,
                "description": spec.description,
                "phase": rt.phase,
                "restarts": rt.restarts,
                "max_restarts": spec.max_restarts,
                "restart_enabled": spec.restart,
                "gave_up": rt.gave_up,
                "last_error": rt.last_error,
                "uptime_seconds": None,
                "next_retry_in_seconds": None,
            }
            if rt.started_at is not None and rt.finished_at is None and rt.phase == "running":
                entry["uptime_seconds"] = round(now - rt.started_at, 1)
            if rt.phase == "crashed" and rt.next_attempt is not None and spec.restart:
                entry["next_retry_in_seconds"] = round(max(0.0, rt.next_attempt - now), 1)
            out[name] = entry
        return out

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _spawn(self, name: str) -> None:
        spec = self._specs[name]
        rt = self._runtime[name]
        rt.task = asyncio.create_task(self._run_guarded(name), name=f"supervised:{name}")
        rt.phase = "running"
        if rt.first_started is None:
            rt.first_started = time.monotonic()
        rt.started_at = time.monotonic()
        rt.finished_at = None
        rt.last_error = None
        rt.next_attempt = None

    async def _run_guarded(self, name: str) -> None:
        """Run one supervised task, converting a crash into a visible FAILED
        phase that the watchdog reacts to (the exception is never swallowed)."""
        rt = self._runtime[name]
        spec = self._specs[name]
        try:
            coro = spec.factory()
            await coro
            rt.phase = "done"
            rt.finished_at = time.monotonic()
        except asyncio.CancelledError:
            rt.phase = "stopped"
            rt.finished_at = time.monotonic()
            raise
        except BaseException as e:  # noqa: BLE001 - supervised tasks report back
            rt.phase = "crashed"
            rt.finished_at = time.monotonic()
            rt.last_error = f"{type(e).__name__}: {e}"
            logger.error("[supervisor] task '%s' crashed: %s", name, rt.last_error)

    async def _watch(self) -> None:
        """Poll finished tasks and respawn crashed restartable ones."""
        while not self._stopping:
            await asyncio.sleep(self._tick)
            now = time.monotonic()
            for name, spec in self._specs.items():
                rt = self._runtime[name]
                if rt.phase == "done":
                    if spec.restart:
                        logger.info("[supervisor] task '%s' finished cleanly", name)
                    continue
                if rt.phase != "crashed":
                    continue
                if not spec.restart:
                    continue
                if rt.restarts >= spec.max_restarts:
                    if not rt.gave_up:
                        rt.gave_up = True
                        logger.error(
                            "[supervisor] task '%s' gave up after %d restart(s), last error: %s",
                            name, spec.max_restarts, rt.last_error,
                        )
                    continue
                if rt.next_attempt is None:
                    delay = min(
                        spec.cooldown * (spec.backoff ** rt.restarts),
                        spec.max_cooldown,
                    )
                    rt.next_attempt = now + delay
                    logger.warning(
                        "[supervisor] scheduling restart of '%s' in %.0fs (attempt %d/%d)",
                        name, delay, rt.restarts + 1, spec.max_restarts,
                    )
                elif now >= rt.next_attempt:
                    rt.restarts += 1
                    logger.warning(
                        "[supervisor] restarting task '%s' (attempt %d/%d)",
                        name, rt.restarts, spec.max_restarts,
                    )
                    self._spawn(name)