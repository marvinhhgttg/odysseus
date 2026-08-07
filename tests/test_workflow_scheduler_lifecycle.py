import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import NullPool
from sqlalchemy.orm import sessionmaker

import core.database as cdb
import routes.task_routes as task_routes
from src.task_scheduler import TaskScheduler


def _request(user):
    return SimpleNamespace(
        state=SimpleNamespace(
            user=user,
            username=user,
            current_user=user,
            owner=user,
        ),
        user=user,
        username=user,
        current_user=user,
        owner=user,
    )

def _endpoint(router, method, path):
    for route in router.routes:
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set()):
            return route.endpoint
    raise AssertionError(f"Endpoint {method} {path} not found")


@pytest.mark.asyncio
async def test_real_scheduler_persists_successful_run_lifecycle(
    tmp_path,
    monkeypatch,
):
    database_path = tmp_path / "workflow-scheduler-smoke.db"
    engine = create_engine(
        f"sqlite:///{database_path}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )
    cdb.Base.metadata.create_all(engine)

    session_local = sessionmaker(
        bind=engine,
        autoflush=False,
        autocommit=False,
    )

    monkeypatch.setattr(cdb, "SessionLocal", session_local)
    monkeypatch.setattr(task_routes, "SessionLocal", session_local)

    scheduler = TaskScheduler(MagicMock())
    execute_action = AsyncMock(
        return_value=("Workflow scheduler smoke completed", True),
    )
    deliver_result = AsyncMock()

    monkeypatch.setattr(scheduler, "_execute_action", execute_action)
    monkeypatch.setattr(
        scheduler,
        "_deliver_task_result",
        deliver_result,
    )
    monkeypatch.setattr(
        scheduler,
        "_log_to_assistant",
        MagicMock(),
    )

    router = task_routes.setup_task_routes(scheduler)
    create_task = _endpoint(router, "POST", "/api/tasks")
    run_task = _endpoint(router, "POST", "/api/tasks/{task_id}/run")
    list_runs = _endpoint(
        router,
        "GET",
        "/api/tasks/{task_id}/runs",
    )

    try:
        created = await create_task(
            _request("alice"),
            task_routes.TaskCreate(
                name="Real scheduler smoke",
                task_type="action",
                action="workflow_smoke_action",
                trigger_type="webhook",
                notifications_enabled=False,
            ),
        )

        triggered = await run_task(
            _request("alice"),
            created["id"],
            force=False,
        )
        assert triggered["ok"] is True

        deadline = asyncio.get_running_loop().time() + 3.0

        while asyncio.get_running_loop().time() < deadline:
            with session_local() as db:
                stored_run = (
                    db.query(cdb.TaskRun)
                    .filter(cdb.TaskRun.task_id == created["id"])
                    .order_by(cdb.TaskRun.started_at.desc())
                    .first()
                )
                if (
                    stored_run is not None
                    and stored_run.status not in {"queued", "running"}
                ):
                    run_id = stored_run.id
                    run_status = stored_run.status
                    run_result = stored_run.result
                    run_finished_at = stored_run.finished_at
                    break
            await asyncio.sleep(0.01)
        else:
            pytest.fail("Scheduler run did not finish within 3 seconds")

        assert run_status == "success"
        assert run_result == "Workflow scheduler smoke completed"
        assert run_finished_at is not None

        with session_local() as db:
            stored_task = (
                db.query(cdb.ScheduledTask)
                .filter(cdb.ScheduledTask.id == created["id"])
                .one()
            )
            assert stored_task.owner == "alice"
            assert stored_task.run_count == 1
            assert stored_task.last_run is not None
            assert stored_task.next_run is None
            assert stored_task.status == "active"

        history = await list_runs(
            _request("alice"),
            created["id"],
            limit=20,
            offset=0,
        )

        assert history["total"] == 1
        assert len(history["runs"]) == 1
        assert history["runs"][0]["id"] == run_id
        assert history["runs"][0]["status"] == "success"
        assert (
            history["runs"][0]["result"]
            == "Workflow scheduler smoke completed"
        )

        execute_action.assert_awaited_once()
        called_task = execute_action.await_args.args[0]
        assert called_task.id == created["id"]
        assert called_task.owner == "alice"
        assert execute_action.await_args.kwargs["run_id"] == run_id

        deliver_result.assert_awaited_once()
        assert created["id"] not in scheduler._executing
        assert created["id"] not in scheduler._task_handles
    finally:
        for handle in list(scheduler._task_handles.values()):
            if not handle.done():
                handle.cancel()

        if scheduler._task_handles:
            await asyncio.gather(
                *scheduler._task_handles.values(),
                return_exceptions=True,
            )

        engine.dispose()
