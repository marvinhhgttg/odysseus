from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import HTTPException

from src.integrations import load_integrations
from src.services.google_oauth_service import ensure_fresh_google_token
from src.services.google_tasks_client import GoogleTasksClient

GOOGLE_TASKS_PROVIDER = "google_tasks"


def find_google_tasks_integration() -> Optional[Dict[str, Any]]:
    integrations = load_integrations()
    for integration in integrations:
        provider = (integration.get("provider") or "").strip()
        preset = (integration.get("preset") or "").strip()
        if provider == GOOGLE_TASKS_PROVIDER or preset == GOOGLE_TASKS_PROVIDER:
            if integration.get("enabled", True):
                return integration
    return None


def _task_to_dict(task: Dict[str, Any], list_title: str) -> Dict[str, Any]:
    return {
        "id": task.get("id", ""),
        "title": task.get("title", ""),
        "notes": task.get("notes", ""),
        "due": task.get("due", ""),
        "status": task.get("status", "needsAction"),
        "is_subtask": bool(task.get("parent")),
        "position": task.get("position", ""),
        "list_title": list_title,
    }


async def fetch_google_tasks(owner: str = "") -> Dict[str, Any]:
    """Fetch open tasks from the connected Google Tasks account.

    Best-effort: raises 404 when no integration is connected, otherwise
    returns a dict with `tasklists` and `tasks`. Token refresh happens through
    the shared Google OAuth maintenance path.
    """
    integration = find_google_tasks_integration()
    if not integration:
        raise HTTPException(status_code=404, detail="No Google Tasks integration connected")

    refreshed = await ensure_fresh_google_token(integration, force_refresh=False)
    client = GoogleTasksClient.from_integration(refreshed or integration)

    lists_data = client.list_tasklists()
    tasklists = [
        {
            "id": item.get("id", ""),
            "title": item.get("title", ""),
        }
        for item in (lists_data.get("items") or [])
    ]

    tasks: List[Dict[str, Any]] = []
    for tasklist in tasklists:
        items = client.list_all_tasks(tasklist["id"], show_completed=False)
        for item in items:
            tasks.append(_task_to_dict(item, tasklist["title"]))

    return {
        "connected_email": (refreshed or integration).get("oauth_connected_email", ""),
        "tasklists": tasklists,
        "tasks": tasks,
    }