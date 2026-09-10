"""FastAPI dependency providers for application components.

Every manager, daemon, and service that the startup sequence stores on
``app.state`` can be fetched through a provider in this module instead of
reaching into ``request.app.state`` directly. This keeps the component wiring
in one place and makes routes testable without booting the whole app.

Providers work both as ``Depends()`` targets and as plain callables::

    from src.components import get_upload_handler

    @router.post("/api/upload")
    async def upload(_request: Request,
                     upload_handler = Depends(get_upload_handler)):
        ...

All providers return ``None`` when the component has not been initialised
(single-user / minimal-mode start).
"""

from fastapi import Request


def get_session_manager(request: Request):
    """Return the SessionManager stored on app.state by the startup sequence."""
    return getattr(request.app.state, "session_manager", None)


def get_upload_handler(request: Request):
    """Return the UploadHandler stored on app.state by the startup sequence."""
    return getattr(request.app.state, "upload_handler", None)


def get_personal_docs_manager(request: Request):
    """Return the personal-docs manager stored on app.state by startup."""
    return getattr(request.app.state, "personal_docs_manager", None)


def get_research_handler(request: Request):
    """Return the ResearchHandler stored on app.state by the startup sequence."""
    return getattr(request.app.state, "research_handler", None)


def get_task_scheduler(request: Request):
    """Return the in-process TaskScheduler stored on app.state by startup."""
    return getattr(request.app.state, "task_scheduler", None)


def get_mcp_manager(request: Request):
    """Return the MCP manager stored on app.state by the startup sequence."""
    return getattr(request.app.state, "mcp_manager", None)


def get_model_discovery(request: Request):
    """Return the model-discovery service stored on app.state by startup."""
    return getattr(request.app.state, "model_discovery", None)


def get_skills_manager(request: Request):
    """Return the SkillsManager stored on app.state by the startup sequence."""
    return getattr(request.app.state, "skills_manager", None)


def get_webhook_manager(request: Request):
    """Return the WebhookManager stored on app.state by the startup sequence."""
    return getattr(request.app.state, "webhook_manager", None)


def get_upload_cleanup_func(request: Request):
    """Return the upload rate-limit/stale-file cleanup callable on app.state."""
    return getattr(request.app.state, "upload_cleanup_func", None)


def get_task_supervisor(request: Request):
    """Return the TaskSupervisor stored on app.state by the lifespan startup.

    ``None`` if the app is not running through ``src.lifespan`` (tests).
    """
    return getattr(request.app.state, "task_supervisor", None)