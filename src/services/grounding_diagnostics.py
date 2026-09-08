"""Grounding-filter observability + on-demand dry-run for diagnostics.

The web-grounding filter (`_filter_web_grounded_answer` in agent_loop.py)
silently rewrites model answers whenever it thinks an assertion is not
supported by the cited sources. When it goes wrong — as with the German
"Aschgabat vs Ashgabat" transliteration bug fixed in
fix/grounding-transliteration — the user sees a canned fallback but has no
easy way to see *why* the filter rejected the block.

This module gives operators a single, admin-only place to look:

- record_rejection(reason, block, ...): pushes a bounded record into an
  in-process ring buffer.
- record_fallback_returned(reason, ...): notes when the entire filter
  returned the fallback string (as opposed to trimming individual blocks).
- rejection_status(): snapshot for /api/diagnostics/grounding.
- explain_answer(answer, sources): calls the real filter, records
  per-block decisions using the same helpers _filter_web_grounded_answer
  uses, and returns a structured explanation without side-effects on the
  ring buffer.

Kept isolated so it can be imported both from the filter (for live
recording) and from a route handler (for on-demand introspection) without
creating an import cycle with agent_loop.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Bounded ring buffer. Small enough to be safe on hot paths, large enough
# to catch the last few problematic turns.
_MAX_HISTORY = 50


@dataclass
class _State:
    rejections: List[Dict[str, Any]] = field(default_factory=list)
    fallbacks: List[Dict[str, Any]] = field(default_factory=list)


_STATE = _State()


def _clip(value: Any, limit: int) -> str:
    text = "" if value is None else str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def record_rejection(
    reason: str,
    block: str,
    *,
    citations: Optional[List[int]] = None,
    urls: Optional[List[str]] = None,
    unsupported_terms: Optional[List[str]] = None,
    now: Optional[datetime] = None,
) -> None:
    """Note that one answer block was rejected by the grounding filter."""
    now = now or datetime.now(timezone.utc)
    entry = {
        "at": now.isoformat(),
        "reason": reason,
        "block": _clip(block, 500),
        "citations": list(citations or []),
        "urls": list(urls or []),
        "unsupported_terms": list(unsupported_terms or []),
    }
    _STATE.rejections.append(entry)
    del _STATE.rejections[:-_MAX_HISTORY]


def record_fallback_returned(
    reason: str,
    *,
    sources_count: int = 0,
    answer_preview: str = "",
    now: Optional[datetime] = None,
) -> None:
    """Note that the filter returned the whole-string fallback."""
    now = now or datetime.now(timezone.utc)
    entry = {
        "at": now.isoformat(),
        "reason": reason,
        "sources_count": sources_count,
        "answer_preview": _clip(answer_preview, 500),
    }
    _STATE.fallbacks.append(entry)
    del _STATE.fallbacks[:-_MAX_HISTORY]


def rejection_status() -> Dict[str, Any]:
    """Snapshot for the diagnostics endpoint."""
    return {
        "rejection_count": len(_STATE.rejections),
        "fallback_count": len(_STATE.fallbacks),
        "last_rejection": _STATE.rejections[-1] if _STATE.rejections else None,
        "last_fallback": _STATE.fallbacks[-1] if _STATE.fallbacks else None,
        "recent_rejections": list(_STATE.rejections[-10:]),
        "recent_fallbacks": list(_STATE.fallbacks[-10:]),
    }


def _reset_state_for_tests() -> None:
    _STATE.rejections.clear()
    _STATE.fallbacks.clear()


def explain_answer(answer: str, sources: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Dry-run the grounding filter and return a structured explanation.

    Uses the same helpers as `_filter_web_grounded_answer`: split into
    blocks, extract citations, canonicalise evidence, evaluate every
    concrete term against every matched source. Never mutates the module
    ring buffer — intended for POST /api/diagnostics/grounding.

    Returns:
      {
        "verdict": "clean" | "trimmed" | "fallback",
        "final_output": <str, what the filter would emit>,
        "blocks": [
          {"block": "...", "kept": bool,
           "citations": [1,3], "urls": [...],
           "matched_sources": [source_url, ...],
           "concrete_terms": [{"term": "aschgabat", "supported": True, "matched_via": "exonym"}, ...],
           "reject_reason": "unsupported_terms" | "no_matched_source" | None
          }, ...
        ],
        "sources_count": N
      }
    """
    from src.agent_loop import (
        _canonicalize_grounding_aliases,
        _concrete_grounding_terms,
        _grounding_term_supported,
        _match_named_grounding_source,
        _normalize_grounding_text,
        _source_grounding_text,
        _split_grounded_answer_blocks,
        _WEB_GROUNDING_FALLBACK,
    )
    try:
        from src.services.grounding_transliteration import (
            transliteration_variants,
        )
    except Exception:
        transliteration_variants = lambda name: set()  # noqa: E731

    indexed_sources = [
        s for s in sources
        if isinstance(s, dict) and s.get("url")
    ]
    source_by_url = {
        str(s.get("url") or "").rstrip("/"): s for s in indexed_sources
    }

    if not indexed_sources:
        return {
            "verdict": "fallback",
            "final_output": _WEB_GROUNDING_FALLBACK,
            "blocks": [],
            "sources_count": 0,
            "reason": "no_indexed_sources",
        }

    import re as _re

    block_reports: List[Dict[str, Any]] = []
    for block in _split_grounded_answer_blocks(answer):
        urls = _re.findall(r"https?://[^\s)\]>]+", block)
        citations = [int(n) for n in _re.findall(r"(?<!\!)\[(\d+)\]", block)]
        matched_sources = []
        for u in urls:
            hit = source_by_url.get(u.rstrip("/"))
            if hit is not None and hit not in matched_sources:
                matched_sources.append(hit)
        for c in citations:
            if 1 <= c <= len(indexed_sources):
                s = indexed_sources[c - 1]
                if s not in matched_sources:
                    matched_sources.append(s)
        named = _match_named_grounding_source(block, indexed_sources)
        if named is not None and named not in matched_sources:
            matched_sources.append(named)

        if not matched_sources:
            block_reports.append({
                "block": _clip(block, 500),
                "kept": False,
                "citations": citations,
                "urls": urls,
                "matched_sources": [],
                "concrete_terms": [],
                "reject_reason": "no_matched_source",
            })
            continue

        evidence = " ".join(_source_grounding_text(s) for s in matched_sources)
        canonical_evidence = _canonicalize_grounding_aliases(evidence)
        terms = _concrete_grounding_terms(block)
        term_reports = []
        unsupported: List[str] = []
        for term in terms:
            supported = _grounding_term_supported(term, evidence)
            matched_via = None
            if supported:
                normalized = _canonicalize_grounding_aliases(term)
                if normalized in canonical_evidence:
                    matched_via = "exact"
                elif any(v in canonical_evidence for v in transliteration_variants(normalized)):
                    matched_via = "transliteration"
                else:
                    matched_via = "hyphen_compound"
            else:
                unsupported.append(term)
            term_reports.append({
                "term": term,
                "supported": supported,
                "matched_via": matched_via,
            })

        block_reports.append({
            "block": _clip(block, 500),
            "kept": not unsupported,
            "citations": citations,
            "urls": urls,
            "matched_sources": [s.get("url") for s in matched_sources],
            "concrete_terms": term_reports,
            "reject_reason": "unsupported_terms" if unsupported else None,
        })

    kept_count = sum(1 for b in block_reports if b["kept"])
    if kept_count == 0:
        verdict = "fallback"
    elif kept_count < len(block_reports):
        verdict = "trimmed"
    else:
        verdict = "clean"

    return {
        "verdict": verdict,
        "blocks": block_reports,
        "sources_count": len(indexed_sources),
    }
