from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class AgentConfig:
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    llm_temperature: float
    checkpoint_db: Path


def load_agent_config() -> AgentConfig:
    required = (
        "ODYSSEUS_LLM_BASE_URL",
        "ODYSSEUS_LLM_MODEL",
        "ODYSSEUS_LLM_API_KEY",
        "ODYSSEUS_LANGGRAPH_DB",
    )
    missing = [name for name in required if not os.getenv(name)]
    if missing:
        raise RuntimeError(
            "Missing required agent configuration: " + ", ".join(missing)
        )

    checkpoint_db = Path(os.environ["ODYSSEUS_LANGGRAPH_DB"]).expanduser()
    checkpoint_db.parent.mkdir(parents=True, exist_ok=True)

    return AgentConfig(
        llm_base_url=os.environ["ODYSSEUS_LLM_BASE_URL"].rstrip("/"),
        llm_model=os.environ["ODYSSEUS_LLM_MODEL"],
        llm_api_key=os.environ["ODYSSEUS_LLM_API_KEY"],
        llm_temperature=float(os.getenv("ODYSSEUS_LLM_TEMPERATURE", "0")),
        checkpoint_db=checkpoint_db,
    )
