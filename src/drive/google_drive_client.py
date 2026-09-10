from typing import Any, Dict, Optional

import requests
from fastapi import HTTPException


class GoogleDriveClient:
    def __init__(self, base_url: str, access_token: Optional[str] = None, api_key: Optional[str] = None):
        self.base_url = (base_url or "https://www.googleapis.com").rstrip("/")
        self.access_token = access_token
        self.api_key = api_key

    @classmethod
    def from_integration(cls, integration: Any) -> "GoogleDriveClient":
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
                or integration.get("api_key")
            )
            api_key = (
                integration.get("api_key")
                or settings.get("api_key")
                or integration.get("google_api_key")
            )
        else:
            base_url = getattr(integration, "base_url", "https://www.googleapis.com")
            access_token = getattr(integration, "access_token", None)
            api_key = getattr(integration, "api_key", None)
            settings = getattr(integration, "settings", None)
            if not access_token and isinstance(settings, dict):
                access_token = settings.get("access_token") or access_token
                api_key = settings.get("api_key") or api_key

        return cls(base_url=base_url, access_token=access_token, api_key=api_key)

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        return headers

    def list_files(
        self,
        *,
        page_size: int = 100,
        page_token: Optional[str] = None,
        q: Optional[str] = None,
        fields: str = "nextPageToken, files(id, name, mimeType, parents, modifiedTime, size)",
    ) -> Dict[str, Any]:
        params: Dict[str, Any] = {
            "pageSize": page_size,
            "fields": fields,
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if page_token:
            params["pageToken"] = page_token
        if q:
            params["q"] = q
        if self.api_key:
            params["key"] = self.api_key

        resp = requests.get(
            f"{self.base_url}/drive/v3/files",
            params=params,
            headers=self._headers(),
            timeout=20,
        )

        if resp.status_code == 401:
            raise HTTPException(status_code=401, detail="Google Drive credentials invalid")
        if resp.status_code == 403:
            raise HTTPException(status_code=403, detail="Google Drive access forbidden")
        if resp.status_code >= 400:
            raise HTTPException(
                status_code=502,
                detail=f"Google Drive API error {resp.status_code}",
            )

        return resp.json()
