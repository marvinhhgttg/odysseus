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
