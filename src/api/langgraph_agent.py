from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, HTTPException
from langchain_core.messages import HumanMessage
from langgraph.types import Command
from pydantic import BaseModel, Field

from src.agent.graph import build_graph


router = APIRouter(
    prefix="/api/langgraph",
    tags=["langgraph-agent"],
)

graph = build_graph()


class RunRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)


def graph_config(conversation_id: UUID) -> dict:
    return {
        "configurable": {
            "thread_id": f"odysseus:{conversation_id}",
        }
    }


def serialize_interrupt(result: dict) -> object | None:
    value = result.get("__interrupt__")
    if value is None:
        return None

    if isinstance(value, tuple):
        return [
            item.value if hasattr(item, "value") else str(item)
            for item in value
        ]

    if hasattr(value, "value"):
        return value.value

    return value


@router.post("/{conversation_id}/run")
async def run_agent(
    conversation_id: UUID,
    payload: RunRequest,
):
    result = graph.invoke(
        {
            "user_text": payload.message,
            "messages": [HumanMessage(content=payload.message)],
        },
        config=graph_config(conversation_id),
    )

    approval = serialize_interrupt(result)
    if approval is not None:
        return {
            "status": "approval_required",
            "approval": approval,
        }

    return {
        "status": "completed",
        "response": result.get("response"),
    }


@router.post("/{conversation_id}/approve")
async def approve_action(conversation_id: UUID):
    result = graph.invoke(
        Command(resume={"approved": True}),
        config=graph_config(conversation_id),
    )

    approval = serialize_interrupt(result)
    if approval is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Eine weitere Freigabe ist erforderlich.",
                "approval": approval,
            },
        )

    return {
        "status": "completed",
        "response": result.get("response"),
    }


@router.post("/{conversation_id}/reject")
async def reject_action(conversation_id: UUID):
    result = graph.invoke(
        Command(resume={"approved": False}),
        config=graph_config(conversation_id),
    )

    approval = serialize_interrupt(result)
    if approval is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "message": "Eine weitere Freigabe ist erforderlich.",
                "approval": approval,
            },
        )

    return {
        "status": "completed",
        "response": result.get("response"),
    }
