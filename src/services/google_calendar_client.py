from typing import Any, Dict, List, Optional

import requests
from fastapi import HTTPException


class GoogleCalendarClient:
    """Client for the Google Calendar v3 API (read + write)."""

    CALENDAR_LIST_PATH = "/calendar/v3/users/me/calendarList"
    CALENDARS_PATH = "/calendar/v3/calendars"
    EVENTS_PATH = "/calendar/v3/calendars/{calendar_id}/events"
    EVENT_PATH = "/calendar/v3/calendars/{calendar_id}/events/{event_id}"

    def __init__(self, base_url: str = "https://www.googleapis.com", access_token: Optional[str] = None):
        self.base_url = (base_url or "https://www.googleapis.com").rstrip("/")
        self.access_token = access_token

    @classmethod
    def from_integration(cls, integration: Any) -> "GoogleCalendarClient":
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
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def _urlencode(self, part: str) -> str:
        from urllib.parse import quote

        return quote(str(part), safe="")

    def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        resp = requests.request(
            method,
            f"{self.base_url}{path}",
            params=params,
            json=json,
            headers=self._headers(),
            timeout=20,
        )
        service = "Google Calendar"
        if resp.status_code == 401:
            raise HTTPException(status_code=401, detail=f"{service} credentials invalid")
        if resp.status_code == 403:
            raise HTTPException(status_code=403, detail=f"{service} access forbidden (missing scope?)")
        if resp.status_code >= 400:
            detail = resp.text[:300]
            raise HTTPException(status_code=resp.status_code, detail=f"{service} API error: {detail}")
        if not resp.content:
            return {}
        return resp.json()

    def list_calendars(self, page_size: int = 250, page_token: Optional[str] = None) -> Dict[str, Any]:
        params: Dict[str, Any] = {"maxResults": page_size, "alt": "json"}
        if page_token:
            params["pageToken"] = page_token
        return self._request("GET", self.CALENDAR_LIST_PATH, params=params)

    def create_calendar(self, summary: str) -> Dict[str, Any]:
        return self._request("POST", self.CALENDARS_PATH, json={"summary": summary})

    def insert_event(self, calendar_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        path = self.EVENTS_PATH.format(calendar_id=self._urlencode(calendar_id))
        return self._request("POST", path, params={"alt": "json"}, json=payload)

    def patch_event(self, calendar_id: str, event_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
        path = self.EVENT_PATH.format(
            calendar_id=self._urlencode(calendar_id),
            event_id=self._urlencode(event_id),
        )
        return self._request("PATCH", path, params={"alt": "json"}, json=payload)

    def delete_event(self, calendar_id: str, event_id: str) -> Dict[str, Any]:
        path = self.EVENT_PATH.format(
            calendar_id=self._urlencode(calendar_id),
            event_id=self._urlencode(event_id),
        )
        return self._request("DELETE", path, params={"alt": "json"})

    def list_events(
        self,
        calendar_id: str,
        page_size: int = 250,
        page_token: Optional[str] = None,
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "maxResults": page_size,
            "alt": "json",
            "singleEvents": "false",
        }
        if page_token:
            params["pageToken"] = page_token
        path = self.EVENTS_PATH.format(calendar_id=self._urlencode(calendar_id))
        return self._request("GET", path, params=params)