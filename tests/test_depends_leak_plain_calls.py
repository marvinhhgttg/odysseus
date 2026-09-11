"""Regression tests for the 5135fdee 'Depends leak' bug class.

Refactor 5135fdee wired helper functions that are *also* invoked as plain
Python calls (not through FastAPI's dependency-injection machinery) up to
``Depends(...)`` defaults. Calling such a helper directly then leaves the raw
``fastapi.params.Depends`` object in the parameter, so:

  - ``_verify_session_owner`` compared ``row.owner != user`` against a Depends
    object and falsely 404'd every chat request (`Session ... not found`).
  - ``_require_auth`` returned the Depends object as the authenticated user,
    which then blew up route bodies that iterate the username (e.g.
    ``email_urgency_state`` ``TypeError: 'Depends' object is not iterable``).

The contract: a helper that may be called directly MUST NOT default a param to
``Depends(...)``; it must resolve the user from the request itself.
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException

import routes.email_helpers as EH
import routes.session_routes as SR


def _req(**state):
    # AuthMiddleware stamps request.state for dependency functions.
    defaults = {
        "api_token": False,
        "api_token_owner": None,
        "current_user": "alice",
    }
    defaults.update(state)
    app = SimpleNamespace(state=SimpleNamespace(auth_manager=None))
    return SimpleNamespace(app=app, state=SimpleNamespace(**defaults))


# --- routes/session_routes._verify_session_owner ----------------------------

def _session_local_returning(owner_value):
    """SessionLocal whose query returns a row with `owner` (or no row)."""
    missing = object()
    if owner_value is missing:
        row = None
    else:
        row = SimpleNamespace(owner=owner_value)
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = row
    return MagicMock(return_value=db)


def test_verify_session_owner_plain_call_resolves_user_internally(monkeypatch):
    """Direct call (as chat_routes does) must not leak a Depends object.

    Regression: with a `user: str = Depends(...)` default, `effective_user`
    resolution was removed and the raw Depends object was compared against the
    session owner — every plain call raised a false 404.
    """
    monkeypatch.setattr(SR, "SessionLocal", _session_local_returning("alice"))
    req = _req(api_token=False, current_user="alice")
    SR._verify_session_owner(req, "sid-owned-by-alice")


def test_verify_session_owner_plain_call_rejects_foreign_session(monkeypatch):
    monkeypatch.setattr(SR, "SessionLocal", _session_local_returning("bob"))
    req = _req(api_token=False, current_user="alice")
    with pytest.raises(HTTPException) as exc:
        SR._verify_session_owner(req, "sid-owned-by-bob")
    assert exc.value.status_code == 404


def test_verify_session_owner_missing_session_is_404(monkeypatch):
    monkeypatch.setattr(SR, "SessionLocal", _session_local_returning(object()))
    req = _req(api_token=False, current_user="alice")
    with pytest.raises(HTTPException) as exc:
        SR._verify_session_owner(req, "nope")
    assert exc.value.status_code == 404


def test_verify_session_owner_explicit_user_still_supported(monkeypatch):
    """Existing callers that pass `user=` explicitly keep working."""
    monkeypatch.setattr(SR, "SessionLocal", _session_local_returning("alice"))
    req = _req(api_token=False, current_user=None)
    SR._verify_session_owner(req, "sid-owned-by-alice", user="alice")


def test_verify_session_owner_signature_has_no_depends_default():
    """Guardrail: the helper must never reintroduce a Depends default."""
    import inspect
    param = inspect.signature(SR._verify_session_owner).parameters["user"]
    assert param.default is None


# --- routes/email_helpers._require_auth chain -------------------------------

def test_email_require_user_returns_str_not_depends_object():
    """`require_user` (the email-route dependency) must resolve to a string.

    Regression: it forwarded to `_require_auth(request)` whose `u` default was
    `Depends(user)` — the raw Depends object came back and route bodies that
    iterate the username crashed with ``TypeError: 'Depends' object is not
    iterable``.
    """
    got = EH.require_user(_req(api_token=False, current_user="alice"))
    assert isinstance(got, str)
    assert got == "alice"


def test_email_require_user_unauthenticated_config_missing_raises(monkeypatch):
    monkeypatch.setenv("AUTH_ENABLED", "true")
    req = _req(api_token=False, current_user=None)
    with pytest.raises(HTTPException) as exc:
        EH.require_user(req)
    assert exc.value.status_code == 401


def test_email_require_owner_resolves_without_depends_leak():
    got = EH.require_owner(
        _req(api_token=False, current_user="alice"),
        account_id=None,
    )
    assert isinstance(got, str)
    assert got == "alice"


# --- routes/chat_helpers.fire_message_event ---------------------------------

def test_fire_message_event_plain_call_fires_with_resolved_user(monkeypatch):
    """`fire_message_event` is called directly (chat_helpers.build_chat_context);
    the user must be resolved from the request — not a Depends leak fired onto
    the event bus."""
    import routes.chat_helpers as CH

    fired = {}
    webhook_calls = []

    class WM:
        def fire_and_forget(self, name, payload):
            webhook_calls.append(name)

    def fake_fire_event(name, data):
        fired["name"] = name
        fired["data"] = data

    monkeypatch.setattr("src.event_bus.fire_event", fake_fire_event)
    req = _req(api_token=False, current_user="alice")
    sess = SimpleNamespace(model="m")
    CH.fire_message_event(req, WM(), "s1", sess, "hello", compare_mode=False)

    assert webhook_calls == ["chat.message"]
    assert fired == {"name": "message_sent", "data": "alice"}


def test_fire_message_event_plain_call_no_user_sends_empty(monkeypatch):
    import routes.chat_helpers as CH

    fired = {}

    def fake_fire_event(name, data):
        fired["data"] = data

    monkeypatch.setattr("src.event_bus.fire_event", fake_fire_event)
    req = _req(api_token=False, current_user=None)
    CH.fire_message_event(req, None, "s1", SimpleNamespace(model="m"), "hi", compare_mode=True)
    assert fired == {"data": ""}