"""End-to-end grounding after web_search inside stream_agent_loop."""

import asyncio
import json

import src.agent_loop as al


SUPPORTED_URL = "https://example.test/eu-ai"
TOPIC_URL = "https://example.test/ki-thema"

SOURCES = [
    {
        "url": SUPPORTED_URL,
        "title": "EU-Kommission veröffentlicht Leitlinien für KI-Modelle",
        "snippet": (
            "Die EU-Kommission veröffentlichte Leitlinien für Anbieter "
            "allgemeiner KI-Modelle."
        ),
        "evidence": (
            "Die Leitlinien erläutern Pflichten für Anbieter allgemeiner "
            "KI-Modelle."
        ),
    },
    {
        "url": TOPIC_URL,
        "title": "Künstliche Intelligenz: aktuelle Nachrichten",
        "snippet": (
            "Nachrichten und Hintergründe zum Thema künstliche Intelligenz."
        ),
    },
]

RAW_ANSWER = (
    "1. **EU-Kommission veröffentlicht KI-Leitlinien.** "
    "Die Leitlinien betreffen Anbieter allgemeiner KI-Modelle. "
    f"[Quelle]({SUPPORTED_URL})\n\n"
    "2. **Anthropic veröffentlicht Mythos 5.** "
    "Das neue Modell Mythos 5 wurde heute vorgestellt. "
    f"[Quelle]({TOPIC_URL})"
)


def _collect(generator):
    async def run():
        return [chunk async for chunk in generator]

    return asyncio.run(run())


def _events(chunks):
    events = []
    for chunk in chunks:
        if not chunk.startswith("data: "):
            continue
        payload = chunk[6:].strip()
        if payload == "[DONE]":
            continue
        try:
            events.append(json.loads(payload))
        except json.JSONDecodeError:
            continue
    return events


def test_web_answer_is_buffered_and_filtered_before_streaming(monkeypatch):
    monkeypatch.setattr(
        al,
        "get_setting",
        lambda key, default=None: default,
        raising=False,
    )
    monkeypatch.setattr(
        al,
        "get_mcp_manager",
        lambda: None,
        raising=False,
    )
    monkeypatch.setattr(
        al,
        "estimate_tokens",
        lambda *args, **kwargs: 10,
        raising=False,
    )

    calls = {"stream": 0, "tool": 0}

    async def fake_stream(_candidates, messages, **kwargs):
        calls["stream"] += 1

        if calls["stream"] == 1:
            yield (
                'data: {"delta": "```web_search\\n'
                'aktuelle KI-Nachrichten\\n```"}\n\n'
            )
            yield "data: [DONE]\n\n"
            return

        assert calls["stream"] == 2
        midpoint = len(RAW_ANSWER) // 2
        for piece in (RAW_ANSWER[:midpoint], RAW_ANSWER[midpoint:]):
            yield f'data: {json.dumps({"delta": piece})}\n\n'
        yield "data: [DONE]\n\n"

    async def fake_execute(block, *args, **kwargs):
        calls["tool"] += 1
        assert block.tool_type == "web_search"

        marker = "<!-- SOURCES:" + json.dumps(SOURCES) + " -->"
        result = {
            "output": "Search results with fetched evidence.\n\n" + marker,
            "exit_code": 0,
        }
        return "web search completed", result

    monkeypatch.setattr(
        al,
        "stream_llm_with_fallback",
        fake_stream,
        raising=False,
    )
    monkeypatch.setattr(
        al,
        "execute_tool_block",
        fake_execute,
        raising=False,
    )

    chunks = _collect(
        al.stream_agent_loop(
            "http://local.test/v1",
            "local-test-model",
            [{
                "role": "user",
                "content": (
                    "Nenne zwei aktuelle KI-Nachrichten. Verwende nur "
                    "ausdrücklich belegte Tatsachen."
                ),
            }],
            max_rounds=2,
            relevant_tools={"web_search"},
        )
    )
    events = _events(chunks)
    deltas = [
        event.get("delta", "")
        for event in events
        if isinstance(event.get("delta"), str)
    ]
    visible_text = "".join(deltas)

    assert calls == {"stream": 2, "tool": 1}

    source_events = [
        event for event in events
        if event.get("type") == "web_sources"
    ]
    assert len(source_events) == 1
    assert source_events[0]["data"] == SOURCES

    assert "EU-Kommission" in visible_text
    assert "KI-Leitlinien" in visible_text
    assert SUPPORTED_URL in visible_text

    assert "Anthropic" not in visible_text
    assert "Mythos 5" not in visible_text
    assert TOPIC_URL not in visible_text

    assert "Weitere Aussagen" in visible_text

    # The raw answer must not have leaked in either streamed fragment.
    assert RAW_ANSWER[:len(RAW_ANSWER) // 2] not in deltas
    assert RAW_ANSWER[len(RAW_ANSWER) // 2:] not in deltas


def test_buffered_intermediate_tool_round_does_not_leak(monkeypatch):
    monkeypatch.setattr(
        al,
        "get_setting",
        lambda key, default=None: default,
        raising=False,
    )
    monkeypatch.setattr(
        al,
        "get_mcp_manager",
        lambda: None,
        raising=False,
    )
    monkeypatch.setattr(
        al,
        "estimate_tokens",
        lambda *args, **kwargs: 10,
        raising=False,
    )

    calls = {"stream": 0, "tool": 0}
    hidden_planning = (
        "Ich habe möglicherweise Mythos 5 gefunden und suche "
        "vorsichtshalber noch einmal nach einer Bestätigung."
    )

    async def fake_stream(_candidates, messages, **kwargs):
        calls["stream"] += 1

        if calls["stream"] == 1:
            yield (
                'data: {"delta": "```web_search\\n'
                'aktuelle KI-Nachrichten\\n```"}\n\n'
            )
        elif calls["stream"] == 2:
            yield f'data: {json.dumps({"delta": hidden_planning})}\n\n'
            yield (
                'data: {"delta": "\\n```web_search\\n'
                'Mythos 5 Anthropic Bestätigung\\n```"}\n\n'
            )
        elif calls["stream"] == 3:
            midpoint = len(RAW_ANSWER) // 2
            for piece in (
                RAW_ANSWER[:midpoint],
                RAW_ANSWER[midpoint:],
            ):
                yield f'data: {json.dumps({"delta": piece})}\n\n'
        else:
            raise AssertionError("unexpected extra model round")

        yield "data: [DONE]\n\n"

    async def fake_execute(block, *args, **kwargs):
        calls["tool"] += 1
        assert block.tool_type == "web_search"

        marker = "<!-- SOURCES:" + json.dumps(SOURCES) + " -->"
        return (
            "web search completed",
            {
                "output": (
                    "Search results with fetched evidence.\n\n"
                    + marker
                ),
                "exit_code": 0,
            },
        )

    monkeypatch.setattr(
        al,
        "stream_llm_with_fallback",
        fake_stream,
        raising=False,
    )
    monkeypatch.setattr(
        al,
        "execute_tool_block",
        fake_execute,
        raising=False,
    )

    chunks = _collect(
        al.stream_agent_loop(
            "http://local.test/v1",
            "local-test-model",
            [{
                "role": "user",
                "content": (
                    "Nenne zwei aktuelle KI-Nachrichten und suche "
                    "bei Unsicherheit erneut."
                ),
            }],
            max_rounds=3,
            relevant_tools={"web_search"},
        )
    )
    events = _events(chunks)
    deltas = [
        event.get("delta", "")
        for event in events
        if isinstance(event.get("delta"), str)
    ]
    visible_text = "".join(deltas)

    assert calls == {"stream": 3, "tool": 2}
    assert len([
        event
        for event in events
        if event.get("type") == "web_sources"
    ]) == 2

    assert hidden_planning not in visible_text
    assert "Mythos 5 Anthropic Bestätigung" not in visible_text
    assert "Anthropic" not in visible_text
    assert "Mythos 5" not in visible_text

    assert "EU-Kommission" in visible_text
    assert SUPPORTED_URL in visible_text
    assert "Weitere Aussagen" in visible_text
