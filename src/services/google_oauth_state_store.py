from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from core.atomic_io import atomic_write_json
from core.platform_compat import safe_chmod
from src.constants import DATA_DIR

STATE_FILE = os.path.join(DATA_DIR, "google_oauth_states.json")


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_parent() -> None:
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)


def _load_all() -> List[Dict[str, Any]]:
    if not os.path.exists(STATE_FILE):
        return []
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        return [row for row in data if isinstance(row, dict)]
    except Exception:
        return []


def _save_all(rows: List[Dict[str, Any]]) -> None:
    _ensure_parent()
    atomic_write_json(STATE_FILE, rows, indent=2)
    safe_chmod(STATE_FILE, 0o600)


def create_state(
    *,
    provider: str,
    owner_id: str,
    state: str,
    code_verifier: str,
    code_challenge: str,
    redirect_uri: str,
    requested_scopes: List[str],
    integration_id: Optional[str] = None,
    ttl_minutes: int = 15,
) -> Dict[str, Any]:
    now = _utcnow()
    row = {
        "provider": provider,
        "owner_id": owner_id,
        "state": state,
        "code_verifier": code_verifier,
        "code_challenge": code_challenge,
        "redirect_uri": redirect_uri,
        "requested_scopes": requested_scopes,
        "integration_id": integration_id,
        "status": "pending",
        "created_at": now.isoformat(),
        "expires_at": (now + timedelta(minutes=ttl_minutes)).isoformat(),
    }
    rows = _load_all()
    rows.append(row)
    _save_all(rows)
    return row


def get_state(state: str) -> Optional[Dict[str, Any]]:
    for row in _load_all():
        if row.get("state") == state:
            return row
    return None


def mark_state(state: str, status: str) -> Optional[Dict[str, Any]]:
    rows = _load_all()
    for row in rows:
        if row.get("state") == state:
            row["status"] = status
            _save_all(rows)
            return row
    return None


def purge_expired_states() -> int:
    now = _utcnow()
    rows = _load_all()
    kept = []
    removed = 0
    for row in rows:
        expires_at = row.get("expires_at")
        try:
            exp = datetime.fromisoformat(expires_at)
        except Exception:
            removed += 1
            continue
        if exp <= now:
            removed += 1
            continue
        kept.append(row)
    if removed:
        _save_all(kept)
    return removed


def is_expired(row: Dict[str, Any]) -> bool:
    try:
        return datetime.fromisoformat(str(row.get("expires_at"))) <= _utcnow()
    except Exception:
        return True
