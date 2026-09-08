"""Prompt-size budget observability and enforcement for the agent loop.

Odysseus already trims the tool set for each turn via RAG + keyword fallback in
`_assemble_prompt`, but nothing measures the resulting system-prompt size or
warns when it grows past what small local models can hold in context. On a
long multi-domain query, the assembled prompt can silently push past a small
model's context window, degrading tool-call accuracy without any visible
signal.

This module is intentionally tiny and pure:

- estimate_tokens(text): a fast, deterministic size approximation (chars / 4).
  Not a real tokenizer — a stable heuristic so the same string always maps to
  the same number, which is enough for budget decisions and diffing across
  turns.

- BudgetDecision: dataclass with fields the caller uses to log + act.

- evaluate_prompt_budget(prompt_text, *, hard_limit_tokens, soft_limit_tokens):
  Emit an actionable decision without side effects:
    * kind="ok"         when we are inside the soft budget
    * kind="warn"       when soft < size <= hard (log, keep the prompt)
    * kind="over_hard"  when size > hard (caller should re-render compact)

- record_prompt_size(...): pushes the last-observed size into an
  in-process singleton for /api/health/agent-prompt-budget to expose.

Tuning defaults:
- hard limit 4000 "tokens" (~16 000 chars) targets a 4k-context floor with
  headroom for the user message, chat history, and tool outputs.
- soft limit 3000 "tokens" is the warning threshold — 75 % of hard.

Overridable via env: ODYSSEUS_PROMPT_HARD_TOKENS, ODYSSEUS_PROMPT_SOFT_TOKENS.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


DEFAULT_HARD_TOKENS = 4000
DEFAULT_SOFT_TOKENS = 3000
_CHARS_PER_TOKEN = 4  # rough English/German average; stable + deterministic


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
        return value if value > 0 else default
    except (TypeError, ValueError):
        return default


def get_hard_limit() -> int:
    return _env_int("ODYSSEUS_PROMPT_HARD_TOKENS", DEFAULT_HARD_TOKENS)


def get_soft_limit() -> int:
    return _env_int("ODYSSEUS_PROMPT_SOFT_TOKENS", DEFAULT_SOFT_TOKENS)


def estimate_tokens(text: str) -> int:
    """Deterministic char-based token estimate. Not a tokenizer."""
    if not text:
        return 0
    return max(1, len(text) // _CHARS_PER_TOKEN)


@dataclass
class BudgetDecision:
    kind: str                      # "ok" | "warn" | "over_hard"
    estimated_tokens: int
    hard_limit: int
    soft_limit: int
    message: str = ""

    @property
    def is_over_hard(self) -> bool:
        return self.kind == "over_hard"

    @property
    def is_warn(self) -> bool:
        return self.kind == "warn"


def evaluate_prompt_budget(
    prompt_text: str,
    *,
    hard_limit_tokens: Optional[int] = None,
    soft_limit_tokens: Optional[int] = None,
) -> BudgetDecision:
    """Categorise a prompt as ok / warn / over_hard.

    Pure — no side effects, no logging. Callers decide whether to log, retry
    compact, or continue.
    """
    hard = hard_limit_tokens if hard_limit_tokens is not None else get_hard_limit()
    soft = soft_limit_tokens if soft_limit_tokens is not None else get_soft_limit()
    if soft > hard:
        soft = hard  # nonsense config → collapse to hard
    tokens = estimate_tokens(prompt_text)

    if tokens > hard:
        return BudgetDecision(
            kind="over_hard",
            estimated_tokens=tokens,
            hard_limit=hard,
            soft_limit=soft,
            message=(
                f"agent-prompt over hard budget: est_tokens={tokens} "
                f"hard={hard} — retry with compact rendering"
            ),
        )
    if tokens > soft:
        return BudgetDecision(
            kind="warn",
            estimated_tokens=tokens,
            hard_limit=hard,
            soft_limit=soft,
            message=(
                f"agent-prompt over soft budget: est_tokens={tokens} "
                f"soft={soft} hard={hard}"
            ),
        )
    return BudgetDecision(
        kind="ok",
        estimated_tokens=tokens,
        hard_limit=hard,
        soft_limit=soft,
    )


# ── In-process observability state ────────────────────────────────────────────
# Ring buffer of the most-recent decisions; the health endpoint reads this
# without touching disk or the request path. Bounded so a hot loop cannot
# blow up memory.

_MAX_HISTORY = 50
_HISTORY: List[Dict[str, Any]] = []


def record_prompt_size(
    decision: BudgetDecision,
    *,
    tool_count: Optional[int] = None,
    compact: bool = False,
    now: Optional[datetime] = None,
) -> None:
    now = now or datetime.now(timezone.utc)
    entry = {
        "at": now.isoformat(),
        "kind": decision.kind,
        "estimated_tokens": decision.estimated_tokens,
        "hard_limit": decision.hard_limit,
        "soft_limit": decision.soft_limit,
        "tool_count": tool_count,
        "compact": compact,
    }
    _HISTORY.append(entry)
    del _HISTORY[:-_MAX_HISTORY]


def prompt_budget_status() -> Dict[str, Any]:
    """Snapshot for the health endpoint."""
    warns = sum(1 for h in _HISTORY if h["kind"] == "warn")
    overs = sum(1 for h in _HISTORY if h["kind"] == "over_hard")
    last = _HISTORY[-1] if _HISTORY else None
    max_seen = max((h["estimated_tokens"] for h in _HISTORY), default=None)
    return {
        "hard_limit_tokens": get_hard_limit(),
        "soft_limit_tokens": get_soft_limit(),
        "sample_count": len(_HISTORY),
        "warn_count": warns,
        "over_hard_count": overs,
        "max_estimated_tokens": max_seen,
        "last": last,
    }


def _reset_state_for_tests() -> None:
    _HISTORY.clear()
