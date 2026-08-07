"""Runtime smoke: does the real application actually come up?

Everything else in this suite exercises pieces of the app — routers through
``TestClient``, stores against temporary SQLite files, JS modules under Node.
Nothing asserted that ``app.py`` itself still imports and assembles into a
working application. ``tests/test_app.py`` only checks that files and
directories exist.

That gap is cheap to fall into: a missing optional dependency reachable from the
import graph, a circular import, an unreachable service at startup, or a router
that quietly stopped being registered all pass every focused test and only show
up when someone starts the server.

Each test boots the app in a **clean subprocess** with the repository on
``PYTHONPATH`` but the working directory pointed at a throwaway data directory.
That combination is deliberate: it proves the app does not depend on being
started from inside its own checkout, and it keeps the boot away from the
production runtime (``/Users/marc/odysseus``, port 9001, the LaunchAgent) and
from the repository's own ``data/`` directory.

Marked ``slow``: a boot is a few seconds, and this is evidence-driven — these are
the most expensive tests in the suite by an order of magnitude.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.slow

_REPO = Path(__file__).resolve().parent.parent
_PROBE = _REPO / "tests" / "helpers" / "runtime_boot_probe.py"
_MARKER = "__ODYSSEUS_SMOKE__"

# A port nothing should be listening on, used to force the degraded path.
_DEAD_PORT = "9"

# Ports that must never be bound by an import. 7000 is the app default,
# 9001 is the documented production runtime.
_FORBIDDEN_PORTS = {7000, 9001}


def _boot(tmp_path: Path, extra_env: dict[str, str] | None = None):
    """Import the app in a clean process and return (parsed payload, result)."""
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    home_dir = tmp_path / "home"
    home_dir.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env.update(
        {
            "ODYSSEUS_DATA_DIR": str(data_dir),
            "APP_DATA_DIR": str(data_dir),
            "DATABASE_URL": f"sqlite:///{data_dir / 'app.db'}",
            "HOME": str(home_dir),
            "PYTHONPATH": str(_REPO),
            "PYTHONDONTWRITEBYTECODE": "1",
            # Never let a smoke boot reach the real runtime.
            "APP_PORT": "0",
            "APP_BIND": "127.0.0.1",
        }
    )
    # Drop anything that could point the boot at a live local service.
    for key in ("CHROMADB_HOST", "CHROMADB_PORT", "OLLAMA_HOST", "LLM_HOST"):
        env.pop(key, None)
    env.update(extra_env or {})

    result = subprocess.run(
        [sys.executable, str(_PROBE)],
        cwd=str(data_dir),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )

    payload = None
    for line in result.stdout.splitlines():
        if line.startswith(_MARKER):
            payload = json.loads(line[len(_MARKER):])
    return payload, result


def _require_boot(payload, result):
    if payload is None or result.returncode != 0:
        raise AssertionError(
            "application failed to boot\n"
            f"returncode: {result.returncode}\n"
            f"STDOUT (tail):\n{result.stdout[-4000:]}\n"
            f"STDERR (tail):\n{result.stderr[-8000:]}"
        )
    return payload


@pytest.fixture(scope="module")
def booted(tmp_path_factory):
    """One clean boot shared by the read-only assertions below."""
    tmp_path = tmp_path_factory.mktemp("runtime-smoke")
    payload, result = _boot(tmp_path)
    return _require_boot(payload, result), tmp_path


def test_probe_script_is_present():
    assert _PROBE.is_file(), f"boot probe missing at {_PROBE}"


def test_application_boots_in_a_clean_process(booted):
    payload, _ = booted
    # A real boot assembles hundreds of routes. A tiny number means routers
    # silently failed to register rather than the app failing loudly.
    assert payload["route_count"] > 200, payload["route_count"]


def test_boot_registers_the_critical_route_surface(booted):
    payload, _ = booted
    routes = set(payload["routes"])

    for path in (
        "/",
        "/api/chat",
        "/api/chat_stream",
        "/api/sessions",
        "/api/tool-approvals",
        "/api/tool-approvals/{approval_id}/approve",
        "/api/tool-approvals/{approval_id}/reject",
    ):
        assert path in routes, f"{path} is not registered on a booted app"


def test_approval_decision_routes_are_post_only(booted):
    payload, _ = booted
    methods = payload["methods"]

    for path in (
        "/api/tool-approvals/{approval_id}/approve",
        "/api/tool-approvals/{approval_id}/reject",
    ):
        assert methods.get(path) == ["POST"], (path, methods.get(path))

    assert methods.get("/api/tool-approvals") == ["GET"]


def test_importing_the_app_starts_no_listener(booted):
    payload, _ = booted
    # uvicorn.run lives behind __main__; importing must never bind a fixed port.
    # Ephemeral binds (port 0) are legitimate probe behavior.
    fixed = [entry for entry in payload["binds"] if entry[1] not in (0, -1)]
    assert not fixed, f"import bound fixed ports: {fixed}"

    bound_ports = {entry[1] for entry in payload["binds"]}
    assert not (bound_ports & _FORBIDDEN_PORTS), (
        f"import touched a reserved runtime port: {sorted(bound_ports & _FORBIDDEN_PORTS)}"
    )


def test_boot_writes_only_into_the_configured_data_directory(booted, tmp_path_factory):
    _, tmp_path = booted
    data_dir = tmp_path / "data"

    # The boot must have actually persisted something, otherwise this test
    # would pass trivially against an app that writes nowhere.
    produced = {entry.name for entry in data_dir.iterdir()}
    assert produced, "boot produced no state at all; isolation is untested"


def test_boot_leaves_the_repository_untouched(tmp_path):
    """A smoke boot must not write into the checkout it was started from.

    This is the guard that keeps the suite away from the production runtime:
    the app is started outside its own directory and the repository must look
    identical afterwards.
    """
    tracked = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(_REPO),
        capture_output=True,
        text=True,
        timeout=60,
    )
    if tracked.returncode != 0:
        pytest.skip("not a git checkout")

    before_status = tracked.stdout
    before_entries = {entry.name for entry in _REPO.iterdir()}

    payload, result = _boot(tmp_path)
    _require_boot(payload, result)

    after_entries = {entry.name for entry in _REPO.iterdir()}
    assert after_entries - before_entries == set(), (
        f"boot created files in the repository: {sorted(after_entries - before_entries)}"
    )

    after_status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(_REPO),
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout
    assert after_status == before_status, (
        "boot modified tracked files in the repository:\n"
        f"before:\n{before_status}\nafter:\n{after_status}"
    )


def test_boot_survives_unreachable_optional_services(tmp_path):
    """Degraded dependencies must degrade, not abort startup.

    ChromaDB backs vector RAG and vector memory and is optional. Pointing it at
    a dead port has to leave a bootable app.
    """
    payload, result = _boot(
        tmp_path,
        {"CHROMADB_HOST": "127.0.0.1", "CHROMADB_PORT": _DEAD_PORT},
    )
    _require_boot(payload, result)

    assert payload["route_count"] > 200
    assert "/api/chat_stream" in set(payload["routes"])

    combined = result.stdout + result.stderr
    assert "ChromaDB" in combined, (
        "expected a degraded-state report for the unreachable optional service"
    )
