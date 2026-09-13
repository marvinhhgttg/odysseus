from typing import Any, Dict, List, Optional

import requests
from fastapi import HTTPException


class GoogleTasksClient:
    """Read-only client for the Google Tasks v1 API."""

    TASKS_LISTS_PATH = "/tasks/v1/users/@me/lists"
    TASKS_PATH = "/tasks/v1/lists/{list_id}/tasks"

    def __init__(self, base_url: str = "https://www.googleapis.com", access_token: Optional[str] = None):
        self.base_url = (base_url or "https://www.googleapis.com").rstrip("/")
        self.access_token = access_token

    @classmethod
    def from_integration(cls, integration: Any) -> "GoogleTasksClient":
        if isinstance(integration, dict):
            base_url = integration.get("base_url") or "https://www.googleapis.com"
            settings = integration.get("settings") or {}
            if not isinstance(settings, dict):
                settings = {}

            access_token = (
                integration.get("oauth_access_token")
                or settings.get("access_token")
                or integration.get("access_token")
                or integration.get("token")
            )
        else:
            base_url = getattr(integration, "base_url", "https://www.googleapis.com")
            access_token = getattr(integration, "access_token", None)
            settings = getattr(integration, "settings", None)
            if not access_token and isinstance(settings, dict):
                access_token = settings.get("access_token") or access_token

        return cls(base_url=base_url, access_token=access_token)

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def _get(self, path: str, params: Dict[str, Any]) -> Dict[str, Any]:
        resp = requests.get(
            f"{self.base_url}{path}",
            params=params,
            headers=self._headers(),
            timeout=20,
        )
        if resp.status_code == 401:
            raise HTTPException(status_code=401, detail="Google Tasks credentials invalid")
        if resp.status_code == 403:
            raise HTTPException(status_code=403, detail="Google Tasks access forbidden (missing scope?)")
        if resp.status_code >= 400:
            detail = resp.text[:300]
            raise HTTPException(status_code=resp.status_code, detail=f"Google Tasks API error: {detail}")
        return resp.json()

    def list_tasklists(self, page_size: int = 100, page_token: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {"maxResults": page_size, "alt": "json"}
        if page_token:
            params["pageToken"] = page_token
        return self._get(self.TASKS_LISTS_PATH, params)

    def list_tasks(
        self,
        tasklist_id: str,
        page_size: int = 100,
        page_token: Optional[str] = None,
        show_completed: bool = False,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "maxResults": page_size,
            "showCompleted": "true" if show_completed else "false",
            "alt": "json",
        }
        if page_token:
            params["pageToken"] = page_token
        return self._get(self.TASKS_PATH.format(list_id=tasklist_id), params)

    def list_all_tasks(self, tasklist_id: str, show_completed: bool = False, max_pages: int = 5) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        page_token: Optional[str] = None
        for _ in range(max_pages):
            data = self.list_tasks(
                tasklist_id,
                page_token=page_token,
                show_completed=show_completed,
            )
            items.extend(data.get("items") or [])
            page_token = data.get("nextPageToken")
            if not page_token:
                break
        return items