"""Coverage for the TaskSupervisor: restart/backoff/give-up, per-task
control (pause/resume/stop/restart), status metrics, shutdown grace, and
the admin diagnostics endpoints wired to them."""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.diagnostics_routes import setup_diagnostics_routes
from src.auth_dependencies import require_admin
from src.components import get_task_supervisor
from src.task_supervisor import TaskSpec, TaskSupervisor


# ---------------------------------------------------------------------------
# Task factories
# ---------------------------------------------------------------------------


def _forever():
    async def _loop():
        while True:
            await asyncio.sleep(0.01)

    return _loop


def _crasher():
    async def _crash():
        raise RuntimeError("boom")

    return _crash


def _finisher(delay=0.0):
    async def _finish():
        if delay:
            await asyncio.sleep(delay)

    return _finish


def _flaky(times_to_fail, delay=0.0):
    box = {"left": times_to_fail}

    async def _flaky_run():
        if box["left"] > 0:
            box["left"] -= 1
            raise RuntimeError("flaky failure")
        if delay:
            await asyncio.sleep(delay)

    return _flaky_run


async def _wait_until(predicate, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    return False


def _make_supervisor(specs, tick=0.01):
    sup = TaskSupervisor(tick=tick)
    for spec in specs:
        sup.register(spec)
    return sup


# ---------------------------------------------------------------------------
# Supervisor behavior
# ---------------------------------------------------------------------------


async def test_registration_is_idempotent_and_ops_reject_unknown_names():
    sup = _make_supervisor([TaskSpec("t", _forever())])
    sup.register(TaskSpec("t", _forever()))
    assert list(sup.status()) == ["t"]

    assert await sup.pause("nope") is False
    assert await sup.resume("nope") is False
    assert await sup.stop("nope") is False
    assert await sup.restart("nope") is False


async def test_start_all_spawns_all_and_is_idempotent():
    sup = _make_supervisor(
        [TaskSpec("a", _forever()), TaskSpec("b", _forever())],
    )
    await sup.start_all()
    await sup.start_all()

    assert sup.is_running() is True
    status = sup.status()
    assert status["a"]["phase"] == "running"
    assert status["b"]["phase"] == "running"
    assert status["a"]["start_count"] == 1
    assert status["b"]["start_count"] == 1
    assert status["a"]["paused"] is False
    await sup.stop_all()


async def test_clean_finish_reports_done_and_does_not_restart():
    sup = _make_supervisor([TaskSpec("t", _finisher(), restart=True)])
    await sup.start_all()
    assert await _wait_until(lambda: sup.status()["t"]["phase"] == "done")

    await asyncio.sleep(0.1)
    status = sup.status()["t"]
    assert status["phase"] == "done"
    assert status["start_count"] == 1
    assert status["crash_count"] == 0
    await sup.stop_all()


async def test_crash_is_restarted_with_backoff_and_reports_next_retry():
    sup = _make_supervisor(
        [TaskSpec("t", _crasher(), max_restarts=3, cooldown=0.05, backoff=1.0)],
    )
    await sup.start_all()

    captured = {}

    def _first_retry():
        value = sup.status()["t"].get("next_retry_in_seconds")
        if value is not None:
            captured["value"] = value
            return True
        return False

    assert await _wait_until(_first_retry)
    entry = sup.status()["t"]
    assert entry["last_error"].startswith("RuntimeError: boom")
    assert isinstance(captured["value"], (int, float))
    assert captured["value"] >= 0.0

    seen = {}

    def _two_restarts():
        status = sup.status()["t"]
        if status["restarts"] >= 2:
            seen.update(status)
            return True
        return False

    assert await _wait_until(_two_restarts)
    assert seen["start_count"] == 3
    assert seen["restarts"] == 2
    assert seen["crash_count"] == 3
    await sup.stop_all()


async def test_crash_gives_up_after_max_restarts():
    sup = _make_supervisor(
        [TaskSpec("t", _crasher(), max_restarts=2, cooldown=0.05, backoff=1.0)],
    )
    await sup.start_all()

    assert await _wait_until(lambda: sup.status()["t"]["gave_up"] is True)
    entry = sup.status()["t"]
    assert entry["restarts"] == 2
    assert entry["start_count"] == 3
    assert entry["crash_count"] == 3

    await asyncio.sleep(0.1)
    assert sup.status()["t"]["start_count"] == 3
    await sup.stop_all()


async def test_restart_disabled_tasks_are_not_respawned():
    sup = _make_supervisor([TaskSpec("t", _crasher(), restart=False)])
    await sup.start_all()

    assert await _wait_until(lambda: sup.status()["t"]["phase"] == "crashed")
    await asyncio.sleep(0.1)
    entry = sup.status()["t"]
    assert entry["start_count"] == 1
    assert entry["crash_count"] == 1
    assert entry["restarts"] == 0
    await sup.stop_all()


async def test_flaky_task_recovers_after_restarts():
    sup = _make_supervisor(
        [TaskSpec("t", _flaky(2), max_restarts=5, cooldown=0.05, backoff=1.0)],
    )
    await sup.start_all()

    assert await _wait_until(lambda: sup.status()["t"]["phase"] == "done")
    entry = sup.status()["t"]
    assert entry["start_count"] == 3
    assert entry["crash_count"] == 2
    assert entry["gave_up"] is False
    await sup.stop_all()


async def test_pause_parks_task_and_watchdog_ignores_it():
    sup = _make_supervisor([TaskSpec("t", _forever(), max_restarts=3)])
    await sup.start_all()

    assert await sup.pause("t") is True
    assert await sup.pause("t") is True  # idempotent
    assert sup.status()["t"]["phase"] == "paused"
    assert sup.status()["t"]["paused"] is True

    await asyncio.sleep(0.15)
    entry = sup.status()["t"]
    assert entry["phase"] == "paused"
    assert entry["start_count"] == 1
    await sup.stop_all()


async def test_resume_respawns_paused_task():
    sup = _make_supervisor([TaskSpec("t", _forever(), max_restarts=3)])
    await sup.start_all()

    await sup.pause("t")
    assert await _wait_until(lambda: sup.status()["t"]["phase"] == "paused")
    assert await sup.resume("t") is True
    assert sup.status()["t"]["phase"] == "running"
    assert sup.status()["t"]["start_count"] == 2

    assert await sup.resume("t") is True  # already running: no-op success
    assert sup.status()["t"]["start_count"] == 2
    await sup.stop_all()


async def test_pause_stops_pending_restart_cycle():
    sup = _make_supervisor(
        [TaskSpec("t", _crasher(), max_restarts=3, cooldown=0.05, backoff=1.0)],
    )
    await sup.start_all()

    assert await _wait_until(lambda: sup.status()["t"]["phase"] == "crashed")
    assert await sup.pause("t") is True
    frozen_start_count = sup.status()["t"]["start_count"]
    await asyncio.sleep(0.15)
    entry = sup.status()["t"]
    assert entry["phase"] == "paused"
    assert entry["start_count"] == frozen_start_count
    await sup.stop_all()


async def test_stop_is_permanent():
    sup = _make_supervisor([TaskSpec("t", _crasher(), max_restarts=5, cooldown=0.05)])
    await sup.start_all()

    assert await sup.stop("t") is True
    assert sup.status()["t"]["phase"] == "stopped"
    await asyncio.sleep(0.15)
    assert sup.status()["t"]["start_count"] == 1
    await sup.stop_all()


async def test_pause_before_start_all_is_respected():
    sup = _make_supervisor([TaskSpec("a", _forever()), TaskSpec("b", _forever())])
    await sup.pause("a")
    await sup.start_all()

    assert sup.status()["a"]["phase"] == "paused"
    assert sup.status()["b"]["phase"] == "running"
    assert await sup.resume("a") is True
    assert sup.status()["a"]["phase"] == "running"
    await sup.stop_all()


async def test_restart_overrides_permanent_stop_and_resets_budget():
    sup = _make_supervisor(
        [TaskSpec("t", _crasher(), max_restarts=2, cooldown=0.05, backoff=1.0)],
    )
    await sup.start_all()
    assert await _wait_until(lambda: sup.status()["t"]["gave_up"] is True)

    assert await sup.restart("t") is True
    entry = sup.status()["t"]
    assert entry["phase"] == "running"
    assert entry["restarts"] == 0
    assert entry["gave_up"] is False
    assert entry["start_count"] == 4

    await sup.stop("t")
    assert await sup.restart("t") is True
    assert sup.status()["t"]["phase"] == "running"
    assert sup.status()["t"]["start_count"] == 5
    await sup.stop_all()


async def test_restart_on_running_task_restarts_it():
    sup = _make_supervisor([TaskSpec("t", _forever())])
    await sup.start_all()

    await asyncio.sleep(0.05)
    before = sup.status()["t"]["start_count"]
    assert await sup.restart("t") is True
    assert sup.status()["t"]["start_count"] == before + 1
    assert sup.status()["t"]["phase"] == "running"
    await sup.stop_all()


async def test_stop_all_with_grace_allows_clean_finish():
    sup = _make_supervisor([TaskSpec("t", _finisher(delay=0.05))])
    await sup.start_all()
    assert await _wait_until(lambda: sup.status()["t"]["phase"] == "running")

    await sup.stop_all(grace=0.5)
    assert sup.status()["t"]["phase"] == "done"
    assert sup.is_running() is False


async def test_stop_all_cancels_long_running_and_is_idempotent():
    sup = _make_supervisor([TaskSpec("t", _forever())])
    await sup.start_all()
    await sup.stop_all(grace=0.0)
    assert sup.status()["t"]["phase"] == "stopped"
    await sup.stop_all(grace=0.0)
    await sup.stop_all(grace=0.2)
    assert sup.status()["t"]["phase"] == "stopped"


async def test_status_reports_cumulative_metrics_and_paused_flag():
    sup = _make_supervisor(
        [TaskSpec("t", _flaky(1), max_restarts=3, cooldown=0.05, backoff=1.0)],
    )
    await sup.start_all()
    assert await _wait_until(lambda: sup.status()["t"]["phase"] == "done")

    entry = sup.status()["t"]
    assert entry["start_count"] == 2
    assert entry["crash_count"] == 1
    assert entry["restarts"] == 1
    assert entry["restart_enabled"] is True
    assert entry["paused"] is False
    await sup.stop_all()


# ---------------------------------------------------------------------------
# Admin diagnostics endpoints (wired via dependency overrides)
# ---------------------------------------------------------------------------


class _StubSupervisor:
    """Sync-dispatch stub so endpoint wiring is testable without loop coupling."""

    def __init__(self):
        self.calls = []
        self.phases = {"upload_cleanup": "running", "done_task": "done"}

    def is_running(self):
        return True

    def status(self):
        return {
            name: {
                "name": name,
                "phase": phase,
                "description": "",
                "restarts": 0,
                "max_restarts": 3,
                "restart_enabled": True,
                "gave_up": False,
                "last_error": None,
                "start_count": 1,
                "crash_count": 0,
                "paused": phase == "paused",
                "uptime_seconds": 1.0,
                "next_retry_in_seconds": None,
            }
            for name, phase in self.phases.items()
        }

    async def pause(self, name):
        self.calls.append(("pause", name))
        if name not in self.phases:
            return False
        self.phases[name] = "paused"
        return True

    async def resume(self, name):
        self.calls.append(("resume", name))
        if name not in self.phases:
            return False
        self.phases[name] = "running"
        return True

    async def stop(self, name):
        self.calls.append(("stop", name))
        if name not in self.phases:
            return False
        self.phases[name] = "stopped"
        return True

    async def restart(self, name):
        self.calls.append(("restart", name))
        if name not in self.phases:
            return False
        self.phases[name] = "running"
        return True


def _make_client(supervisor=None, admin_bypass=True):
    app = FastAPI()
    app.include_router(setup_diagnostics_routes(None, False, None))
    if admin_bypass:
        app.dependency_overrides[require_admin] = lambda: None
    app.dependency_overrides[get_task_supervisor] = lambda: supervisor
    return TestClient(app)


def test_status_endpoint_reports_snapshot():
    client = _make_client(_StubSupervisor())
    response = client.get("/api/diagnostics/tasks")
    assert response.status_code == 200
    body = response.json()
    assert body["supervisor"] == "running"
    assert body["total"] == 2
    assert body["tasks"]["upload_cleanup"]["phase"] == "running"


@pytest.mark.parametrize(
    ("path", "action"),
    [
        ("/api/diagnostics/tasks/upload_cleanup/pause", "pause"),
        ("/api/diagnostics/tasks/upload_cleanup/resume", "resume"),
        ("/api/diagnostics/tasks/upload_cleanup/stop", "stop"),
        ("/api/diagnostics/tasks/upload_cleanup/restart", "restart"),
    ],
)
def test_task_actions_dispatch(path, action):
    stub = _StubSupervisor()
    client = _make_client(stub)

    response = client.post(path)
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "name": "upload_cleanup",
        "ok": True,
        "phase": stub.phases["upload_cleanup"],
        "error": None,
    }
    assert stub.calls == [(action, "upload_cleanup")]


def test_task_action_unknown_name():
    stub = _StubSupervisor()
    client = _make_client(stub)
    response = client.post("/api/diagnostics/tasks/ghost/restart")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["phase"] is None
    assert body["error"] == "unknown task"


def test_task_action_supervisor_not_loaded():
    client = _make_client(None)
    response = client.post("/api/diagnostics/tasks/upload_cleanup/pause")
    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is False
    assert body["error"] == "supervisor not loaded"


def test_task_endpoints_require_admin():
    client = _make_client(_StubSupervisor(), admin_bypass=False)
    assert client.get("/api/diagnostics/tasks").status_code == 403
    assert (
        client.post("/api/diagnostics/tasks/upload_cleanup/pause").status_code
        == 403
    )