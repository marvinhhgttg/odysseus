from __future__ import annotations

import sqlite3
from typing import Annotated, TypedDict

from langchain_core.messages import AIMessage, BaseMessage
from langchain_openai import ChatOpenAI
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph, add_messages
from langgraph.types import interrupt

from src.agent.config import load_agent_config
from src.agent.policies import RiskLevel, classify_risk


class AgentState(TypedDict, total=False):
    messages: Annotated[list[BaseMessage], add_messages]
    user_text: str
    risk_level: str
    proposed_action: dict
    approval_granted: bool
    response: str


def build_graph():
    config = load_agent_config()

    llm = ChatOpenAI(
        model=config.llm_model,
        base_url=config.llm_base_url,
        api_key=config.llm_api_key,
        temperature=config.llm_temperature,
        max_retries=0,
    )

    def classify_request(state: AgentState) -> dict:
        risk = classify_risk(state["user_text"])
        return {"risk_level": risk.value}

    def answer_read_only(state: AgentState) -> dict:
        prompt = f"""Du bist Odysseus, ein lokaler persönlicher Assistent auf macOS.

Beantworte die Nutzeranfrage hilfreich und präzise.
Du hast in diesem Schritt KEINEN Zugriff auf Dateien, Kalender, Shell,
Browser oder externe Dienste. Behaupte niemals, eine Aktion ausgeführt zu haben.

Nutzeranfrage:
{state["user_text"]}
"""
        answer = llm.invoke(prompt).content
        return {
            "response": answer,
            "messages": [AIMessage(content=answer)],
        }

    def prepare_write_proposal(state: AgentState) -> dict:
        proposal = {
            "kind": "unimplemented_write_request",
            "request": state["user_text"],
            "scope": "No real tool is connected yet.",
            "effect": "No data will be changed in this phase.",
        }
        return {"proposed_action": proposal}

    def request_approval(state: AgentState) -> dict:
        decision = interrupt(
            {
                "type": "approval_required",
                "question": (
                    "Soll Odysseus diesen späteren Schreibvorgang "
                    "ausführen?"
                ),
                "proposed_action": state["proposed_action"],
            }
        )

        approved = (
            isinstance(decision, dict)
            and decision.get("approved") is True
        )
        return {"approval_granted": approved}

    def execute_confirmed_placeholder(state: AgentState) -> dict:
        if state.get("approval_granted"):
            answer = (
                "Freigabe registriert. In diesem Installationsschritt ist "
                "noch kein reales Schreib-Tool angebunden; es wurde deshalb "
                "nichts verändert."
            )
        else:
            answer = "Aktion abgelehnt. Es wurde nichts verändert."

        return {
            "response": answer,
            "messages": [AIMessage(content=answer)],
        }

    def route_by_risk(state: AgentState) -> str:
        if state["risk_level"] == RiskLevel.READ.value:
            return "answer_read_only"
        return "prepare_write_proposal"

    builder = StateGraph(AgentState)

    builder.add_node("classify_request", classify_request)
    builder.add_node("answer_read_only", answer_read_only)
    builder.add_node("prepare_write_proposal", prepare_write_proposal)
    builder.add_node("request_approval", request_approval)
    builder.add_node(
        "execute_confirmed_placeholder",
        execute_confirmed_placeholder,
    )

    builder.add_edge(START, "classify_request")
    builder.add_conditional_edges(
        "classify_request",
        route_by_risk,
        {
            "answer_read_only": "answer_read_only",
            "prepare_write_proposal": "prepare_write_proposal",
        },
    )
    builder.add_edge("answer_read_only", END)
    builder.add_edge("prepare_write_proposal", "request_approval")
    builder.add_edge("request_approval", "execute_confirmed_placeholder")
    builder.add_edge("execute_confirmed_placeholder", END)

    connection = sqlite3.connect(
        str(config.checkpoint_db),
        check_same_thread=False,
    )
    checkpointer = SqliteSaver(connection)

    return builder.compile(checkpointer=checkpointer)
