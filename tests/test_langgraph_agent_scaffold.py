"""Coverage for the experimental LangGraph agent scaffold (src/agent, src/api)."""

import importlib
import sys
from uuid import UUID

import pytest
from pydantic import ValidationError

from src.agent.policies import RiskLevel, classify_risk


class _Value:
    def __init__(self, value):
        self.value = value


# ---------------------------------------------------------------------------
# classify_risk (src/agent/policies) is pure and importable without env.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "sende die email",
        "verschiebe die datei",
        "verschieben nach neuem ordner",
        "Lösche verlauf bitte",
        "Loesche daten",
        "bitte umbenennen",
        "Benenne die datei um",
        "erstelle termin um 15 uhr",
        "task anlegen",
        "git commit und git push",
        "ÜBERSCHREIBE die datei",
        "ueberschreibe die notiz",
    ],
)
def test_classify_risk_write_words(text):
    assert classify_risk(text) == RiskLevel.WRITE_CONFIRM


@pytest.mark.parametrize(
    "text",
    [
        "was ist 2+2?",
        "zeig mir meine letzten chats",
        "guten morgen",
        "wie heisst der autor des buches?",
    ],
)
def test_classify_risk_read_only(text):
    assert classify_risk(text) == RiskLevel.READ


# ---------------------------------------------------------------------------
# load_agent_config (src/agent/config)
# ---------------------------------------------------------------------------


def test_load_agent_config_requires_all_env_vars(monkeypatch):
    from src.agent.config import load_agent_config

    monkeypatch.delenv("ODYSSEUS_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("ODYSSEUS_LLM_MODEL", raising=False)
    monkeypatch.delenv("ODYSSEUS_LLM_API_KEY", raising=False)
    monkeypatch.delenv("ODYSSEUS_LANGGRAPH_DB", raising=False)
    monkeypatch.delenv("ODYSSEUS_LLM_TEMPERATURE", raising=False)

    with pytest.raises(RuntimeError) as exc:
        load_agent_config()
    message = str(exc.value)
    assert "ODYSSEUS_LLM_BASE_URL" in message
    assert "ODYSSEUS_LLM_MODEL" in message
    assert "ODYSSEUS_LLM_API_KEY" in message
    assert "ODYSSEUS_LANGGRAPH_DB" in message


def test_load_agent_config_reads_env(monkeypatch, tmp_path):
    from src.agent.config import load_agent_config

    db = tmp_path / "nested" / "checkpoint.db"
    monkeypatch.setenv("ODYSSEUS_LLM_BASE_URL", "http://host:9999/v1/")
    monkeypatch.setenv("ODYSSEUS_LLM_MODEL", "test-model")
    monkeypatch.setenv("ODYSSEUS_LLM_API_KEY", "test-key")
    monkeypatch.setenv("ODYSSEUS_LANGGRAPH_DB", str(db))
    monkeypatch.setenv("ODYSSEUS_LLM_TEMPERATURE", "0.5")

    config = load_agent_config()
    assert config.llm_base_url == "http://host:9999/v1"
    assert config.llm_model == "test-model"
    assert config.llm_api_key == "test-key"
    assert config.llm_temperature == 0.5
    assert config.checkpoint_db == db
    assert db.parent.is_dir()


# ---------------------------------------------------------------------------
# langgraph_agent module is importable only with the four env vars set
# (module-level build_graph -> load_agent_config). Pure helpers tested
# against the real module; no LLM is invoked.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def lg_module(tmp_path_factory):
    import os

    db = tmp_path_factory.mktemp("langgraph") / "checkpoint.db"
    saved = {
        key: os.environ.pop(key, None)
        for key in (
            "ODYSSEUS_LLM_BASE_URL",
            "ODYSSEUS_LLM_MODEL",
            "ODYSSEUS_LLM_API_KEY",
            "ODYSSEUS_LANGGRAPH_DB",
        )
    }
    os.environ["ODYSSEUS_LLM_BASE_URL"] = "http://127.0.0.1:9999/v1"
    os.environ["ODYSSEUS_LLM_MODEL"] = "test-model"
    os.environ["ODYSSEUS_LLM_API_KEY"] = "test-key"
    os.environ["ODYSSEUS_LANGGRAPH_DB"] = str(db)

    sys.modules.pop("src.api.langgraph_agent", None)
    module = importlib.import_module("src.api.langgraph_agent")

    try:
        yield module
    finally:
        sys.modules.pop("src.api.langgraph_agent", None)
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_langgraph_router_metadata(lg_module):
    assert lg_module.router.prefix == "/api/langgraph"
    assert lg_module.router.tags == ["langgraph-agent"]
    paths = {route.path for route in lg_module.router.routes}
    assert "/api/langgraph/{conversation_id}/run" in paths
    assert "/api/langgraph/{conversation_id}/approve" in paths
    assert "/api/langgraph/{conversation_id}/reject" in paths


def test_graph_config_thread_id(lg_module):
    cid = UUID("12345678-1234-5678-1234-567812345678")
    assert lg_module.graph_config(cid) == {
        "configurable": {"thread_id": "odysseus:12345678-1234-5678-1234-567812345678"}
    }


def test_serialize_interrupt_none(lg_module):
    assert lg_module.serialize_interrupt({}) is None


def test_serialize_interrupt_tuple(lg_module):
    result = {"__interrupt__": (_Value("a"), _Value("b"))}
    assert lg_module.serialize_interrupt(result) == ["a", "b"]


def test_serialize_interrupt_single_value(lg_module):
    assert lg_module.serialize_interrupt({"__interrupt__": _Value("x")}) == "x"


def test_serialize_interrupt_plain_passthrough(lg_module):
    assert lg_module.serialize_interrupt({"__interrupt__": {"k": 1}}) == {"k": 1}


def test_run_request_validates_length(lg_module):
    RunRequest = lg_module.RunRequest
    assert RunRequest(message="hi").message == "hi"
    with pytest.raises(ValidationError):
        RunRequest(message="")
    with pytest.raises(ValidationError):
        RunRequest(message="x" * 20_001)


def test_compiled_graph_structure(lg_module):
    nodes = lg_module.graph.get_graph().nodes
    for expected in (
        "__start__",
        "classify_request",
        "answer_read_only",
        "prepare_write_proposal",
        "request_approval",
        "execute_confirmed_placeholder",
        "__end__",
    ):
        assert expected in nodes, f"missing node {expected}"