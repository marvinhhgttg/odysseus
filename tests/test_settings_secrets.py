"""Coverage for Punkt-5 secrets-at-rest: settings.json credential keys are
Fernet-encrypted on save, transparently decrypted on read, migrated in place
from legacy plaintext, and reported via the admin diagnostics endpoint."""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from routes.diagnostics_routes import setup_diagnostics_routes
from src import settings
from src.auth_dependencies import require_admin
from services.search.providers import _get_provider_key


@pytest.fixture()
def isolated_store(tmp_path, monkeypatch):
    """Point settings.json AND the Fernet key at a throwaway tmp dir so the
    suite never touches the real data/ key or writes to the user's file.

    Patches the CURRENT src.secret_storage module in sys.modules (never pops /
    reimports — a partial-import cleanup can drop the sys.modules entry and
    break later in-function imports). src.settings resolves encrypt/decrypt
    lazily at call time, so patching the same module object keeps both sides
    consistent even when a previous test replaced the module."""
    settings_file = tmp_path / "settings.json"
    key_file = tmp_path / ".app_key"
    monkeypatch.setattr(settings, "SETTINGS_FILE", str(settings_file))
    monkeypatch.setattr(settings, "APP_KEY_FILE", str(key_file))
    from src import secret_storage
    monkeypatch.setattr(secret_storage, "_KEY_PATH", key_file)
    monkeypatch.setattr(secret_storage, "_fernet", None)
    settings._invalidate_caches()
    return settings_file


def _encrypt(value):
    from src.secret_storage import encrypt
    return encrypt(value)


def _read_raw(settings_file):
    return json.loads(settings_file.read_text(encoding="utf-8"))


def test_save_encrypts_secret_keys_and_read_decrypts(isolated_store):
    settings_file = isolated_store
    settings.save_settings({"brave_api_key": "BRAVE-TOP-SECRET", "search_provider": "searxng"})

    raw = _read_raw(settings_file)
    assert raw["brave_api_key"].startswith("enc:")
    assert "BRAVE-TOP-SECRET" not in raw["brave_api_key"]
    assert raw["search_provider"] == "searxng"

    assert settings.load_settings()["brave_api_key"] == "BRAVE-TOP-SECRET"


def test_provider_key_consumers_get_plaintext(isolated_store):
    settings_file = isolated_store
    settings.save_settings({"brave_api_key": "provider-secret", "search_provider": "brave"})
    assert _get_provider_key("brave") == "provider-secret"


def test_empty_and_non_secret_values_pass_through(isolated_store):
    settings_file = isolated_store
    settings.save_settings({
        "brave_api_key": "",
        "google_pse_key": "",
        "google_pse_cx": "0123456789-public-id",
    })
    raw = _read_raw(settings_file)
    assert raw["brave_api_key"] == ""
    assert raw["google_pse_key"] == ""
    assert raw["google_pse_cx"] == "0123456789-public-id"
    loaded = settings.load_settings()
    assert loaded["brave_api_key"] == ""
    assert loaded["google_pse_cx"] == "0123456789-public-id"


def test_round_trip_never_double_encrypts(isolated_store):
    settings_file = isolated_store
    settings.save_settings({"brave_api_key": "X", "search_provider": "searxng"})
    current = settings.load_settings()
    settings.save_settings(current)

    raw = _read_raw(settings_file)
    assert raw["brave_api_key"].count("enc:") == 1
    assert settings.load_settings()["brave_api_key"] == "X"


def test_load_decrypts_already_encrypted_file(isolated_store):
    settings_file = isolated_store
    settings_file.write_text(
        json.dumps({"brave_api_key": _encrypt("LEGACY"), "search_provider": "searxng"}),
        encoding="utf-8",
    )
    assert settings.load_settings()["brave_api_key"] == "LEGACY"


def test_migrate_encrypts_legacy_plaintext_and_is_idempotent(isolated_store):
    settings_file = isolated_store
    settings_file.write_text(
        json.dumps({"brave_api_key": "PLAIN", "search_provider": "searxng"}),
        encoding="utf-8",
    )

    assert settings.migrate_settings_secrets() is True
    raw = _read_raw(settings_file)
    assert raw["brave_api_key"].startswith("enc:")
    assert "PLAIN" not in raw["brave_api_key"]

    assert settings.migrate_settings_secrets() is False
    assert settings.load_settings()["brave_api_key"] == "PLAIN"


def test_migrate_noop_for_missing_or_clean_file(isolated_store):
    settings_file = isolated_store
    assert settings.migrate_settings_secrets() is False

    settings_file.write_text(
        json.dumps({"search_provider": "searxng", "google_pse_cx": "abc"}),
        encoding="utf-8",
    )
    assert settings.migrate_settings_secrets() is False

    settings_file.write_text(json.dumps([1, 2]), encoding="utf-8")
    assert settings.migrate_settings_secrets() is False


def test_secret_storage_status_reports_state(isolated_store):
    settings_file = isolated_store
    settings_file.write_text(json.dumps({"brave_api_key": "PLAIN"}), encoding="utf-8")
    _encrypt("materialize-key")  # create the tmp key file with 0o600

    status = settings.secret_storage_status()
    assert status["key_file"]["present"] is True
    assert status["key_file"]["mode_0600"] is True
    assert status["secrets"]["brave_api_key"] == {
        "configured": True,
        "encrypted_at_rest": False,
    }
    assert status["legacy_plaintext_remaining"] is True

    settings.migrate_settings_secrets()
    status = settings.secret_storage_status()
    assert status["secrets"]["brave_api_key"] == {
        "configured": True,
        "encrypted_at_rest": True,
    }
    assert status["legacy_plaintext_remaining"] is False

    assert status["secrets"]["google_pse_key"] == {
        "configured": False,
        "encrypted_at_rest": False,
    }


def test_secrets_diagnostics_endpoint_and_admin_gate(isolated_store):
    settings_file = isolated_store
    settings.save_settings({"tavily_api_key": "tvly-123"})
    app = FastAPI()
    app.include_router(setup_diagnostics_routes(None, False, None))
    app.dependency_overrides[require_admin] = lambda: None

    client = TestClient(app)
    response = client.get("/api/diagnostics/secrets")
    assert response.status_code == 200
    body = response.json()
    assert "key_file" in body
    assert "secrets" in body
    assert body["secrets"]["tavily_api_key"]["configured"] is True
    assert body["secrets"]["tavily_api_key"]["encrypted_at_rest"] is True
    assert body["legacy_plaintext_remaining"] is False


def test_secrets_endpoint_requires_admin(isolated_store):
    app = FastAPI()
    app.include_router(setup_diagnostics_routes(None, False, None))
    client = TestClient(app)
    assert client.get("/api/diagnostics/secrets").status_code == 403