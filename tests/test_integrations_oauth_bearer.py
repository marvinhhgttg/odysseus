"""Coverage for OAuth-aware bearer-token resolution in integrations."""

import pytest

from src.integrations import (
    _integration_auth_header_value,
    _integration_bearer_token,
    mask_integration_secret,
)


def test_bearer_token_priority_oauth_first():
    integration = {
        "oauth_access_token": "oauth-tok",
        "access_token": "acc-tok",
        "token": "plain-tok",
        "api_key": "key",
        "settings": {"access_token": "settings-tok"},
    }
    assert _integration_bearer_token(integration) == "oauth-tok"


def test_bearer_token_uses_settings_access_token_without_oauth():
    integration = {
        "access_token": "acc-tok",
        "settings": {"access_token": "settings-tok"},
    }
    assert _integration_bearer_token(integration) == "settings-tok"


def test_bearer_token_falls_back_to_legacy_fields():
    assert _integration_bearer_token({"access_token": "acc-tok"}) == "acc-tok"
    assert _integration_bearer_token({"token": "plain-tok"}) == "plain-tok"
    assert _integration_bearer_token({"api_key": "key"}) == "key"


def test_bearer_token_skips_none_and_placeholder_values():
    integration = {
        "oauth_access_token": None,
        "settings": {"access_token": None},
        "access_token": "None",
        "api_key": "",
    }
    assert _integration_bearer_token(integration) == ""

    integration["token"] = "  "
    assert _integration_bearer_token(integration) == ""


def test_bearer_token_non_dict_returns_empty():
    assert _integration_bearer_token(None) == ""
    assert _integration_bearer_token("string") == ""
    assert _integration_bearer_token({}) == ""


def test_auth_header_bearer_type():
    integration = {
        "auth_type": "bearer",
        "oauth_access_token": "oauth-tok",
        "api_key": "key",
    }
    assert _integration_auth_header_value(integration) == "Bearer oauth-tok"


def test_auth_header_basic_returns_api_key():
    integration = {"auth_type": "basic", "api_key": "user:pass"}
    assert _integration_auth_header_value(integration) == "user:pass"


@pytest.mark.parametrize("key,value", [("provider", "google_drive"), ("preset", "google_drive")])
def test_auth_header_google_drive_preset_and_provider(key, value):
    integration = {
        key: value,
        "oauth_access_token": "oauth-tok",
        "api_key": "wrong-key",
    }
    assert _integration_auth_header_value(integration) == "Bearer oauth-tok"


def test_auth_header_google_drive_without_token_is_empty():
    integration = {"provider": "google_drive", "api_key": ""}
    assert _integration_auth_header_value(integration) == ""


def test_auth_header_unknown_returns_empty():
    assert _integration_auth_header_value({"auth_type": "header", "api_key": "x"}) == ""
    assert _integration_auth_header_value({"auth_type": "query", "api_key": "x"}) == ""
    assert _integration_auth_header_value({}) == ""


def test_oauth_access_token_is_masked_in_responses():
    integration = {
        "oauth_access_token": "secret-tok",
        "oauth_refresh_token": "refresh",
        "api_key": "key",
        "preset": "google_drive",
        "settings": {"access_token": "settings-tok", "refresh_token": "r"},
    }
    safe = mask_integration_secret(integration)
    assert safe["oauth_access_token"] != "secret-tok"
    assert safe["oauth_refresh_token"] != "refresh"
    assert safe["api_key"] != "key"
    assert safe["settings"]["access_token"] != "settings-tok"
    assert safe["preset"] == "google_drive"