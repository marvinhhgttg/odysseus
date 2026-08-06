"""Deterministic first-pass routing for chat model roles."""

from dataclasses import dataclass
import re
from typing import Final


ROUTE_STANDARD: Final = "standard"
ROUTE_RESEARCH: Final = "research"
ROUTE_CODING: Final = "coding"
ROUTE_TOOL_UTILITY: Final = "tool_utility"

VALID_ROUTES: Final = frozenset(
    {
        ROUTE_STANDARD,
        ROUTE_RESEARCH,
        ROUTE_CODING,
        ROUTE_TOOL_UTILITY,
    }
)

_EXPLICIT_ROUTE_RE = re.compile(
    r"^\s*(?:/route|route(?:\s*:)?)\s+"
    r"(standard|research|coding|tool[_ -]?utility)\b",
    re.IGNORECASE,
)

_CODE_FENCE_RE = re.compile(r"```(?:\w+)?\s", re.IGNORECASE)
_CODE_FILE_RE = re.compile(
    r"\b[\w./-]+\.(?:py|js|ts|tsx|jsx|java|go|rs|rb|php|swift|kt|"
    r"c|cc|cpp|h|hpp|cs|sh|bash|zsh|sql|html|css|vue|svelte)\b",
    re.IGNORECASE,
)
_CODING_RE = re.compile(
    r"\b(?:"
    r"pytest|unittest|stack\s*trace|traceback|syntax\s*error|"
    r"compile(?:r|d)?|debug(?:ge|ging)?|refactor(?:e|ing)?|"
    r"implement(?:iere|ation|ing)?|code review|pull request|"
    r"function|klasse|class|method|methode|repository|codebase|"
    r"endpoint|regex|sql query|migration|exception"
    r")\b",
    re.IGNORECASE,
)

_DEEP_RESEARCH_RE = re.compile(
    r"\b(?:deep research|tiefenrecherche|ausführliche recherche|"
    r"systematische recherche|research report)\b",
    re.IGNORECASE,
)
_RESEARCH_RE = re.compile(
    r"\b(?:"
    r"recherchier(?:e|en)|research|investigat(?:e|ion)|"
    r"quellen|sources|belege|citations?|literatur|studien|"
    r"latest|aktuell(?:e|en|er|es)?|neueste|news|websuche|"
    r"search the web|look up"
    r")\b",
    re.IGNORECASE,
)

_UTILITY_STRONG_RE = re.compile(
    r"\b(?:"
    r"klassifizier(?:e|en)|classif(?:y|ication)|"
    r"kategorisier(?:e|en)|categor(?:ize|ise|ization)|"
    r"extrahier(?:e|en)|extract(?:ion)?|"
    r"tagge|tagging|label(?:n|ing)?"
    r")\b",
    re.IGNORECASE,
)
_STRUCTURED_OUTPUT_RE = re.compile(
    r"\b(?:valid(?:es)? json|json schema|csv|yaml|"
    r"strukturierte ausgabe|structured output)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class RouteDecision:
    route: str
    reason: str
    confidence: float

    def __post_init__(self) -> None:
        if self.route not in VALID_ROUTES:
            raise ValueError(f"Unsupported route: {self.route}")


def _explicit_route(text: str) -> RouteDecision | None:
    match = _EXPLICIT_ROUTE_RE.search(text)
    if not match:
        return None

    route = match.group(1).lower().replace("-", "_").replace(" ", "_")
    return RouteDecision(
        route=route,
        reason="explicit_route",
        confidence=1.0,
    )


def classify_model_route(message: str) -> RouteDecision:
    """Classify a request conservatively into a model role."""
    text = str(message or "").strip()
    if not text:
        return RouteDecision(
            route=ROUTE_STANDARD,
            reason="empty_request",
            confidence=1.0,
        )

    explicit = _explicit_route(text)
    if explicit is not None:
        return explicit

    coding_score = 0
    research_score = 0
    utility_score = 0

    if _CODE_FENCE_RE.search(text):
        coding_score += 3
    if _CODE_FILE_RE.search(text):
        coding_score += 3
    if _CODING_RE.search(text):
        coding_score += 3

    if _DEEP_RESEARCH_RE.search(text):
        research_score += 4
    if _RESEARCH_RE.search(text):
        research_score += 2
    if len(_RESEARCH_RE.findall(text)) >= 2:
        research_score += 1

    if _UTILITY_STRONG_RE.search(text):
        utility_score += 3
    if _STRUCTURED_OUTPUT_RE.search(text):
        utility_score += 2

    scores = {
        ROUTE_CODING: coding_score,
        ROUTE_RESEARCH: research_score,
        ROUTE_TOOL_UTILITY: utility_score,
    }
    route, score = max(
        scores.items(),
        key=lambda item: (
            item[1],
            {
                ROUTE_CODING: 3,
                ROUTE_RESEARCH: 2,
                ROUTE_TOOL_UTILITY: 1,
            }[item[0]],
        ),
    )

    if score < 3:
        return RouteDecision(
            route=ROUTE_STANDARD,
            reason="no_strong_signal",
            confidence=0.6,
        )

    return RouteDecision(
        route=route,
        reason=f"deterministic_{route}",
        confidence=min(0.95, 0.65 + (score * 0.05)),
    )


@dataclass(frozen=True)
class ModelRouteTarget:
    endpoint_url: str
    model: str
    headers: dict
    fallback_candidates: tuple
    decision: RouteDecision


def resolve_model_route_target(
    message: str,
    session_endpoint_url: str,
    session_model: str,
    session_headers: dict | None,
    owner: str | None = None,
) -> ModelRouteTarget:
    """Resolve a temporary per-request model target without mutating a session."""
    decision = classify_model_route(message)
    endpoint_url = session_endpoint_url
    model = session_model
    headers = dict(session_headers or {})

    specialized_prefix = {
        ROUTE_RESEARCH: "research",
        ROUTE_TOOL_UTILITY: "utility",
        ROUTE_CODING: "task",
    }.get(decision.route)

    if specialized_prefix:
        try:
            from src.endpoint_resolver import resolve_endpoint

            endpoint_url, model, headers = resolve_endpoint(
                specialized_prefix,
                fallback_url=session_endpoint_url,
                fallback_model=session_model,
                fallback_headers=headers,
                owner=owner,
            )
        except Exception:
            endpoint_url = session_endpoint_url
            model = session_model
            headers = dict(session_headers or {})

    try:
        from src.endpoint_resolver import (
            resolve_chat_fallback_candidates,
            resolve_utility_fallback_candidates,
        )

        if specialized_prefix:
            fallback_candidates = tuple(
                resolve_utility_fallback_candidates(owner=owner) or ()
            )
        else:
            fallback_candidates = tuple(
                resolve_chat_fallback_candidates(owner=owner) or ()
            )
    except Exception:
        fallback_candidates = ()

    return ModelRouteTarget(
        endpoint_url=endpoint_url or session_endpoint_url,
        model=model or session_model,
        headers=dict(headers or {}),
        fallback_candidates=fallback_candidates,
        decision=decision,
    )
