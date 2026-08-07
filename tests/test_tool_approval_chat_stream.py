import json
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from core.models import ChatMessage, Session
from routes import chat_routes
from routes.chat_helpers import (
    ChatContext,
    PreprocessedMessage,
    PresetInfo,
)


RESUME_MESSAGE = "Continue the approved tool call."


class FakeSessionManager:
    def __init__(self):
        self.session = Session(
            id="session-1",
            name="Approval resume",
            endpoint_url="http://model.test/v1",
            model="local-model",
            headers={},
            history=[
                ChatMessage(
                    "user",
                    "Run the approved command.",
                ),
                ChatMessage(
                    "assistant",
                    "Approval required.",
                ),
                ChatMessage(
                    "user",
                    RESUME_MESSAGE,
                ),
            ],
            owner="alice",
        )

    def get_session(self, session_id):
        if session_id != self.session.id:
            raise KeyError(session_id)
        return self.session

    def save_sessions(self):
        return None


class EmptyQuery:
    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def first(self):
        return None

    def all(self):
        return []

    def update(self, *args, **kwargs):
        return 0


class EmptyDatabase:
    def query(self, *args, **kwargs):
        return EmptyQuery()

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None

    def expunge(self, value):
        return None


class OpenToolPolicy:
    def all_disabled_names(self):
        return set()

    def blocks(self, capability):
        return False
    blocks_trigger_research = False
    blocks_manage_research = False
    blocks_generate_image = False
    block_all_tool_calls = False

    def reason_for(self, tool_name):
        return ""


def build_app(monkeypatch):
    session_manager = FakeSessionManager()
    calls = []

    monkeypatch.setattr(
        chat_routes,
        "SessionLocal",
        lambda: EmptyDatabase(),
    )
    monkeypatch.setattr(
        chat_routes,
        "_verify_session_owner",
        lambda request, session_id: None,
    )
    monkeypatch.setattr(
        chat_routes,
        "effective_user",
        lambda request: request.state.current_user,
    )
    monkeypatch.setattr(
        chat_routes,
        "get_current_user",
        lambda request: request.state.current_user,
    )
    monkeypatch.setattr(
        chat_routes,
        "_enforce_chat_privileges",
        lambda request, sess: None,
    )
    monkeypatch.setattr(
        chat_routes,
        "resolve_session_auth",
        lambda sess, session_id, owner=None: None,
    )
    monkeypatch.setattr(
        chat_routes,
        "_clear_orphaned_session_endpoint",
        lambda sess, owner=None: False,
    )
    monkeypatch.setattr(
        chat_routes,
        "_recover_empty_session_model",
        lambda sess, session_id, owner=None: None,
    )
    monkeypatch.setattr(
        chat_routes,
        "set_session_mode",
        lambda session_id, mode: None,
    )
    monkeypatch.setattr(
        chat_routes,
        "get_session_mode",
        lambda session_id: None,
    )
    monkeypatch.setattr(
        chat_routes,
        "build_effective_tool_policy",
        lambda **kwargs: OpenToolPolicy(),
    )

    async def fake_build_chat_context(
        sess,
        request,
        chat_handler,
        chat_processor,
        **kwargs,
    ):
        return ChatContext(
            preface=[],
            rag_sources=[],
            web_sources=[],
            used_memories=[],
            messages=[
                message.to_dict()
                for message in sess.history
            ],
            context_length=8192,
            was_compacted=False,
            user=request.state.current_user,
            uprefs={},
            preset=PresetInfo(
                temperature=0.2,
                max_tokens=256,
                system_prompt=None,
                character_name=None,
            ),
            preprocessed=PreprocessedMessage(
                enhanced_message=kwargs["message"],
                user_content=kwargs["message"],
                text_for_context=kwargs["message"],
                youtube_transcripts=[],
                attachment_meta=[],
            ),
        )

    monkeypatch.setattr(
        chat_routes,
        "build_chat_context",
        fake_build_chat_context,
    )

    route_target = SimpleNamespace(
        endpoint_url="http://model.test/v1",
        model="local-model",
        headers={},
        decision=SimpleNamespace(route="standard"),
        fallback_candidates=[],
    )
    monkeypatch.setattr(
        "src.model_routing.resolve_model_route_target",
        lambda *args, **kwargs: route_target,
    )

    async def fake_stream_agent_loop(
        endpoint_url,
        model,
        messages,
        **kwargs,
    ):
        calls.append(
            {
                "endpoint_url": endpoint_url,
                "model": model,
                "messages": messages,
                **kwargs,
            }
        )
        yield (
            "data: "
            + json.dumps(
                {
                    "type": "approval_error",
                    "approvalId": kwargs["approval_id"],
                    "runId": kwargs["run_id"],
                    "error": "wiring-smoke",
                }
            )
            + "\n\n"
        )
        yield "data: [DONE]\n\n"

    monkeypatch.setattr(
        chat_routes,
        "stream_agent_loop",
        fake_stream_agent_loop,
    )

    router = chat_routes.setup_chat_routes(
        session_manager=session_manager,
        chat_handler=None,
        chat_processor=None,
        memory_manager=None,
        research_handler=None,
        upload_handler=None,
    )

    app = FastAPI()
    app.include_router(router)

    @app.middleware("http")
    async def add_test_identity(request, call_next):
        request.state.current_user = "alice"
        return await call_next(request)

    return app, calls


@pytest.mark.asyncio
async def test_chat_stream_wires_approval_resume_through_http(monkeypatch):
    app, calls = build_app(monkeypatch)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://approval.test",
    ) as client:
        response = await client.post(
            "/api/chat_stream",
            data={
                "message": RESUME_MESSAGE,
                "session": "session-1",
                "mode": "agent",
                "compare_mode": "true",
                "approvalId": "approval-1",
                "runId": "run-1",
            },
        )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith(
        "text/event-stream"
    )
    assert '"type": "approval_error"' in response.text
    assert '"approvalId": "approval-1"' in response.text
    assert '"runId": "run-1"' in response.text
    assert "data: [DONE]" in response.text

    assert len(calls) == 1
    call = calls[0]
    assert call["approval_mode"] is True
    assert call["approval_id"] == "approval-1"
    assert call["run_id"] == "run-1"
    assert call["owner"] == "alice"
    assert call["session_id"] == "session-1"

    user_messages = [
        item["content"]
        for item in call["messages"]
        if item.get("role") == "user"
    ]
    assert user_messages == ["Run the approved command."]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "resume_fields",
    [
        {"approvalId": "approval-1"},
        {"runId": "run-1"},
    ],
)
async def test_chat_stream_requires_complete_approval_pair(
    monkeypatch,
    resume_fields,
):
    app, calls = build_app(monkeypatch)
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://approval.test",
    ) as client:
        response = await client.post(
            "/api/chat_stream",
            data={
                "message": RESUME_MESSAGE,
                "session": "session-1",
                "mode": "agent",
                "compare_mode": "true",
                **resume_fields,
            },
        )

    assert response.status_code == 400
    assert response.json() == {
        "detail": "approvalId and runId must be provided together"
    }
    assert calls == []
