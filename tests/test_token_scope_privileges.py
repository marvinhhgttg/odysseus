"""Tests for API-token scope → effective privilege gating.

A bearer API token authenticates as its owner, but must only inherit the
privileges its scopes grant. Without this gate a default ``chat``-scoped token
of an admin owner would reach the agent's shell (``can_use_bash``). These tests
pin the intersection semantics in src/token_scopes.py.
"""

from types import SimpleNamespace

from core.auth import ADMIN_PRIVILEGES, DEFAULT_PRIVILEGES
from src.token_scopes import chat_privileges, effective_privileges_for_token

_ADMIN_OWNER_PRIVS = dict(ADMIN_PRIVILEGES)
_ADMIN_OWNER_PRIVS.update({
    "can_use_bash": True,
    "can_use_agent": True,
})


def _token_request(scopes, is_token=True):
    return SimpleNamespace(
        state=SimpleNamespace(
            api_token=is_token,
            api_token_scopes=scopes,
        )
    )


def _fake_auth_manager(privs):
    return SimpleNamespace(get_privileges=lambda user: dict(privs))


# ── effective_privileges_for_token ──────────────────────────────────────

def test_admin_scope_passes_owner_privileges_through():
    privs = effective_privileges_for_token(["admin"], _ADMIN_OWNER_PRIVS)
    assert privs is not _ADMIN_OWNER_PRIVS  # never mutate the caller's map
    assert privs["can_use_bash"] is True
    assert privs["can_use_agent"] is True


def test_chat_scope_caps_admin_raised_capabilities():
    privs = effective_privileges_for_token(["chat"], _ADMIN_OWNER_PRIVS)
    # The capability an admin stores that a plain chat token must NOT inherit.
    assert privs["can_use_bash"] is False
    # Default non-admin baseline capabilities stay available.
    assert privs["can_use_agent"] is True
    assert privs["can_use_documents"] is True


def test_restrictions_are_preserved_not_granted():
    owner = dict(DEFAULT_PRIVILEGES)
    owner.update({
        "allowed_models": ["gpt-4o"],
        "allowed_models_restricted": True,
        "max_messages_per_day": 42,
        "block_all_models": True,
    })
    privs = effective_privileges_for_token(["todos:read"], owner)
    assert privs["allowed_models"] == ["gpt-4o"]
    assert privs["allowed_models_restricted"] is True
    assert privs["max_messages_per_day"] == 42
    assert privs["block_all_models"] is True
    assert privs["can_use_bash"] is False


def test_empty_scopes_cap_like_default_token():
    privs = effective_privileges_for_token([], _ADMIN_OWNER_PRIVS)
    assert privs["can_use_bash"] is False
    assert privs["can_use_agent"] is True


def test_non_admin_owner_unchanged_by_scope_cap():
    # A normal user's map is already at (or below) the baseline, so the cap is
    # a no-op for every key.
    owner = dict(DEFAULT_PRIVILEGES)
    privs = effective_privileges_for_token(["chat"], owner)
    assert privs == owner


def test_unknown_scopes_do_not_raise():
    privs = effective_privileges_for_token(["bogus:scope"], _ADMIN_OWNER_PRIVS)
    assert privs["can_use_bash"] is False


# ── chat_privileges ─────────────────────────────────────────────────────

def test_chat_privileges_non_token_callers_unrestricted():
    privs = chat_privileges(
        _token_request([], is_token=False),
        _fake_auth_manager(_ADMIN_OWNER_PRIVS),
        "admin-user",
    )
    assert privs["can_use_bash"] is True


def test_chat_privileges_token_caps_bash_without_shell_scope():
    privs = chat_privileges(
        _token_request(["chat"]),
        _fake_auth_manager(_ADMIN_OWNER_PRIVS),
        "admin-user",
    )
    assert privs["can_use_bash"] is False


def test_chat_privileges_token_with_admin_scope_keeps_elevation():
    privs = chat_privileges(
        _token_request(["admin"]),
        _fake_auth_manager(_ADMIN_OWNER_PRIVS),
        "admin-user",
    )
    assert privs["can_use_bash"] is True


def test_chat_privileges_no_auth_manager_or_user_stays_empty():
    assert chat_privileges(_token_request(["chat"]), None, None) == {}
    assert chat_privileges(_token_request(["chat"]), _fake_auth_manager(_ADMIN_OWNER_PRIVS), None) == {}