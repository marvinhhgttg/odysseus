import pytest

from src.model_routing import (
    ROUTE_CODING,
    ROUTE_RESEARCH,
    ROUTE_STANDARD,
    ROUTE_TOOL_UTILITY,
    RouteDecision,
    classify_model_route,
)


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Hallo, wie geht es dir?", ROUTE_STANDARD),
        ("Erkläre mir Photosynthese.", ROUTE_STANDARD),
        ("Schreibe eine freundliche Einladung.", ROUTE_STANDARD),
        ("Fasse diesen Absatz kurz zusammen.", ROUTE_STANDARD),
        ("Was bedeutet Determinismus?", ROUTE_STANDARD),
        ("", ROUTE_STANDARD),
        ("Bitte refactore routes/chat_routes.py.", ROUTE_CODING),
        ("Debugge diesen Python traceback.", ROUTE_CODING),
        ("Schreibe pytest-Tests für diese Funktion.", ROUTE_CODING),
        ("Implementiere einen API endpoint.", ROUTE_CODING),
        ("Prüfe src/agent_loop.py auf Fehler.", ROUTE_CODING),
        ("```python\nprint('hello')\n```", ROUTE_CODING),
        ("Recherchiere aktuelle Nachrichten mit Quellen.", ROUTE_RESEARCH),
        ("Führe eine Tiefenrecherche zu MLX durch.", ROUTE_RESEARCH),
        ("Create a research report with citations.", ROUTE_RESEARCH),
        ("Search the web for the latest release.", ROUTE_RESEARCH),
        ("Untersuche aktuelle Studien und nenne Quellen.", ROUTE_RESEARCH),
        ("Deep research: local language models.", ROUTE_RESEARCH),
        ("Klassifiziere diese E-Mails.", ROUTE_TOOL_UTILITY),
        ("Extrahiere alle Termine als valides JSON.", ROUTE_TOOL_UTILITY),
        ("Categorize these support tickets.", ROUTE_TOOL_UTILITY),
        ("Extract the fields as CSV.", ROUTE_TOOL_UTILITY),
        ("Tagge diese Texte nach Thema.", ROUTE_TOOL_UTILITY),
        ("Classification with structured output.", ROUTE_TOOL_UTILITY),
        ("/route standard Implementiere diese Funktion.", ROUTE_STANDARD),
        ("/route research Erkläre mir das Thema.", ROUTE_RESEARCH),
        ("/route coding Schreibe eine Funktion.", ROUTE_CODING),
        ("/route tool-utility Extrahiere Daten.", ROUTE_TOOL_UTILITY),
        ("route: tool utility Klassifiziere das.", ROUTE_TOOL_UTILITY),
        ("Aktiviere bitte den dunklen Modus.", ROUTE_STANDARD),
    ],
)
def test_route_catalog(message, expected):
    assert classify_model_route(message).route == expected


def test_decision_contains_reason_and_confidence():
    decision = classify_model_route(
        "Recherchiere aktuelle Studien und nenne Quellen."
    )

    assert decision.reason == "deterministic_research"
    assert 0.0 <= decision.confidence <= 1.0


def test_empty_request_has_explicit_reason():
    decision = classify_model_route("   ")

    assert decision == RouteDecision(
        route=ROUTE_STANDARD,
        reason="empty_request",
        confidence=1.0,
    )


def test_route_decision_rejects_unknown_route():
    with pytest.raises(ValueError, match="Unsupported route"):
        RouteDecision(
            route="unknown",
            reason="test",
            confidence=1.0,
        )


def test_same_input_is_deterministic():
    message = "Implementiere Tests für src/endpoint_resolver.py."

    first = classify_model_route(message)
    second = classify_model_route(message)

    assert first == second


def test_standard_target_preserves_session(monkeypatch):
    import src.endpoint_resolver as resolver

    monkeypatch.setattr(
        resolver,
        "resolve_chat_fallback_candidates",
        lambda owner=None: [("fallback-url", "fallback-model", {})],
    )

    from src.model_routing import resolve_model_route_target

    target = resolve_model_route_target(
        "Hallo, wie geht es dir?",
        "session-url",
        "session-model",
        {"Authorization": "session"},
        owner="marc",
    )

    assert target.decision.route == ROUTE_STANDARD
    assert target.endpoint_url == "session-url"
    assert target.model == "session-model"
    assert target.headers == {"Authorization": "session"}
    assert target.fallback_candidates == (
        ("fallback-url", "fallback-model", {}),
    )


@pytest.mark.parametrize(
    ("message", "expected_prefix"),
    [
        ("Implementiere diese Funktion.", "task"),
        ("Recherchiere aktuelle Studien mit Quellen.", "research"),
        ("Extrahiere alle Termine als JSON.", "utility"),
    ],
)
def test_specialized_target_uses_role_endpoint(
    monkeypatch,
    message,
    expected_prefix,
):
    import src.endpoint_resolver as resolver

    calls = []

    def fake_resolve_endpoint(
        prefix,
        fallback_url=None,
        fallback_model=None,
        fallback_headers=None,
        owner=None,
    ):
        calls.append((prefix, owner))
        return (
            f"{prefix}-url",
            f"{prefix}-model",
            {"X-Role": prefix},
        )

    monkeypatch.setattr(
        resolver,
        "resolve_endpoint",
        fake_resolve_endpoint,
    )
    monkeypatch.setattr(
        resolver,
        "resolve_chat_fallback_candidates",
        lambda owner=None: [],
    )
    monkeypatch.setattr(
        resolver,
        "resolve_utility_fallback_candidates",
        lambda owner=None: [],
    )

    from src.model_routing import resolve_model_route_target

    target = resolve_model_route_target(
        message,
        "session-url",
        "session-model",
        {"Authorization": "session"},
        owner="marc",
    )

    assert calls == [(expected_prefix, "marc")]
    assert target.endpoint_url == f"{expected_prefix}-url"
    assert target.model == f"{expected_prefix}-model"
    assert target.headers == {"X-Role": expected_prefix}


def test_primary_resolver_failure_preserves_session(monkeypatch):
    import src.endpoint_resolver as resolver

    def fail(*args, **kwargs):
        raise RuntimeError("resolver unavailable")

    monkeypatch.setattr(resolver, "resolve_endpoint", fail)
    monkeypatch.setattr(
        resolver,
        "resolve_utility_fallback_candidates",
        lambda owner=None: [],
    )

    from src.model_routing import resolve_model_route_target

    target = resolve_model_route_target(
        "Implementiere diese Funktion.",
        "session-url",
        "session-model",
        {"Authorization": "session"},
        owner="marc",
    )

    assert target.decision.route == ROUTE_CODING
    assert target.endpoint_url == "session-url"
    assert target.model == "session-model"
    assert target.headers == {"Authorization": "session"}
    assert target.fallback_candidates == ()


def test_fallback_failure_keeps_resolved_primary(monkeypatch):
    import src.endpoint_resolver as resolver

    monkeypatch.setattr(
        resolver,
        "resolve_endpoint",
        lambda prefix, **kwargs: (
            f"{prefix}-url",
            f"{prefix}-model",
            {"X-Role": prefix},
        ),
    )

    def fail(*args, **kwargs):
        raise RuntimeError("fallback resolver unavailable")

    monkeypatch.setattr(
        resolver,
        "resolve_utility_fallback_candidates",
        fail,
    )

    from src.model_routing import resolve_model_route_target

    target = resolve_model_route_target(
        "Implementiere diese Funktion.",
        "session-url",
        "session-model",
        {"Authorization": "session"},
        owner="marc",
    )

    assert target.endpoint_url == "task-url"
    assert target.model == "task-model"
    assert target.headers == {"X-Role": "task"}
    assert target.fallback_candidates == ()


def test_target_does_not_mutate_session_headers(monkeypatch):
    import src.endpoint_resolver as resolver

    monkeypatch.setattr(
        resolver,
        "resolve_chat_fallback_candidates",
        lambda owner=None: [],
    )

    from src.model_routing import resolve_model_route_target

    original = {"Authorization": "session"}

    target = resolve_model_route_target(
        "Hallo",
        "session-url",
        "session-model",
        original,
    )

    target.headers["X-Test"] = "changed"

    assert original == {"Authorization": "session"}
