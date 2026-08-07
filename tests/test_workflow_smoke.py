from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from tests.helpers.import_state import clear_fake_database_modules


clear_fake_database_modules()

import core.database as cdb
import routes.task_routes as task_routes


def _request(user):
    return SimpleNamespace(
        state=SimpleNamespace(current_user=user),
    )


def _endpoint(router, method, path):
    for route in router.routes:
        if (
            getattr(route, "path", None) == path
            and method in getattr(route, "methods", set())
        ):
            return route.endpoint
    raise RuntimeError(f"{method} {path} not found")


@pytest.fixture
def workflow_env(tmp_path, monkeypatch):
    database_path = tmp_path / "workflow-smoke.db"
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
    monkeypatch.setattr(task_routes, "SessionLocal", session_local)

    scheduler = SimpleNamespace(
        run_task_now=AsyncMock(return_value=True),
    )
    router = task_routes.setup_task_routes(scheduler)

    yield session_local, scheduler, router

    engine.dispose()


@pytest.mark.asyncio
async def test_create_then_run_persists_owner_and_dispatches(workflow_env):
    session_local, scheduler, router = workflow_env
    create_task = _endpoint(router, "POST", "/api/tasks")
    run_task = _endpoint(router, "POST", "/api/tasks/{task_id}/run")

    created = await create_task(
        _request("alice"),
        task_routes.TaskCreate(
            name="Workflow smoke",
            prompt="Return a deterministic smoke result",
            task_type="llm",
            trigger_type="webhook",
        ),
    )

    with session_local() as db:
        stored = (
            db.query(cdb.ScheduledTask)
            .filter(cdb.ScheduledTask.id == created["id"])
            .one()
        )
        assert stored.owner == "alice"
        assert stored.name == "Workflow smoke"
        assert stored.prompt == "Return a deterministic smoke result"
        assert stored.status == "active"

    result = await run_task(_request("alice"), created["id"], force=False)

    assert result["ok"] is True
    scheduler.run_task_now.assert_awaited_once_with(
        created["id"],
        force=False,
    )


@pytest.mark.asyncio
async def test_foreign_owner_cannot_read_or_run_task(workflow_env):
    _, scheduler, router = workflow_env
    create_task = _endpoint(router, "POST", "/api/tasks")
    get_task = _endpoint(router, "GET", "/api/tasks/{task_id}")
    run_task = _endpoint(router, "POST", "/api/tasks/{task_id}/run")

    created = await create_task(
        _request("alice"),
        task_routes.TaskCreate(
            name="Private workflow",
            prompt="Keep this workflow owner-scoped",
            task_type="llm",
            trigger_type="webhook",
        ),
    )

    with pytest.raises(HTTPException) as read_error:
        await get_task(_request("bob"), created["id"])

    assert read_error.value.status_code == 403

    with pytest.raises(HTTPException) as run_error:
        await run_task(_request("bob"), created["id"], force=False)

    assert run_error.value.status_code == 403
    scheduler.run_task_now.assert_not_awaited()
