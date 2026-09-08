"""Tests for the implicit-citation match in _filter_web_grounded_answer.

Directly exercises the filter (not just explain_answer) so we catch the
whole pipeline including the substantive check that used to force the
fallback whenever the model omitted a visible [N].
"""
from __future__ import annotations

import pytest

from src.agent_loop import _filter_web_grounded_answer, _WEB_GROUNDING_FALLBACK


def _src(url: str, title: str, snippet: str) -> dict:
    return {"url": url, "title": title, "snippet": snippet}


# ── The Turkmenistan reproduction — no explicit citation, single source ────

def test_implicit_citation_matches_single_source():
    answer = "Die Hauptstadt von Turkmenistan ist Aschgabat."
    sources = [_src(
        "https://example.com/ashgabat",
        "Ashgabat",
        "ashgabat is the capital of turkmenistan.",
    )]
    result = _filter_web_grounded_answer(answer, sources)
    assert result != _WEB_GROUNDING_FALLBACK, "must not fallback"
    # Citation must be auto-appended
    assert "[1]" in result
    # Original claim must remain
    assert "Aschgabat" in result


def test_implicit_citation_via_transliteration():
    """Combines both fixes: exonym + implicit citation."""
    answer = "Aschgabat ist die Hauptstadt Turkmenistans."
    sources = [_src(
        "https://en.wikipedia.org/wiki/Ashgabat",
        "Ashgabat - Wikipedia",
        "ashgabat is the capital and largest city of turkmenistan.",
    )]
    result = _filter_web_grounded_answer(answer, sources)
    assert result != _WEB_GROUNDING_FALLBACK
    assert "[1]" in result


# ── Negative cases: filter must still reject unrelated content ─────────────

def test_no_implicit_match_when_content_unrelated():
    answer = "Rom liegt am Rhein und ist die Hauptstadt Deutschlands."
    sources = [_src(
        "https://example.com/berlin",
        "Berlin",
        "berlin is the capital of germany.",
    )]
    result = _filter_web_grounded_answer(answer, sources)
    # This block has zero content overlap with the Berlin evidence, so no
    # implicit match and the filter falls back.
    assert result == _WEB_GROUNDING_FALLBACK


def test_no_implicit_match_when_multiple_candidates():
    """Two sources both match → ambiguous → no implicit citation."""
    answer = "Aschgabat ist die Hauptstadt Turkmenistans."
    sources = [
        _src("https://a.example/", "Ashgabat A",
             "ashgabat is the capital of turkmenistan"),
        _src("https://b.example/", "Ashgabat B",
             "ashgabat is the capital of turkmenistan"),
    ]
    result = _filter_web_grounded_answer(answer, sources)
    # We deliberately do not silently pick one; the filter falls back
    # to protect against wrong attributions.
    assert result == _WEB_GROUNDING_FALLBACK


def test_explicit_citation_still_kept_intact():
    answer = "Aschgabat ist die Hauptstadt Turkmenistans [1]."
    sources = [_src(
        "https://example.com/1", "Ashgabat",
        "ashgabat is the capital of turkmenistan.",
    )]
    result = _filter_web_grounded_answer(answer, sources)
    # Should not double-cite when a citation already exists.
    assert result != _WEB_GROUNDING_FALLBACK
    assert result.count("[1]") == 1
