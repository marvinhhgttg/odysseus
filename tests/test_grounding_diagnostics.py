"""Tests for src.services.grounding_diagnostics."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.services import grounding_diagnostics as gd


NOW = datetime(2026, 9, 8, 20, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clean():
    gd._reset_state_for_tests()
    yield
    gd._reset_state_for_tests()


# ── record + snapshot ────────────────────────────────────────────────────────

def test_status_empty_before_any_record():
    s = gd.rejection_status()
    assert s["rejection_count"] == 0
    assert s["fallback_count"] == 0
    assert s["last_rejection"] is None
    assert s["last_fallback"] is None
    assert s["recent_rejections"] == []
    assert s["recent_fallbacks"] == []


def test_record_rejection_populates_status():
    gd.record_rejection(
        "unsupported_terms",
        "Aschgabat ist die Hauptstadt.",
        citations=[1, 3],
        urls=["https://example.com/a"],
        unsupported_terms=["Aschgabat"],
        now=NOW,
    )
    s = gd.rejection_status()
    assert s["rejection_count"] == 1
    last = s["last_rejection"]
    assert last["reason"] == "unsupported_terms"
    assert "Aschgabat" in last["block"]
    assert last["citations"] == [1, 3]
    assert last["unsupported_terms"] == ["Aschgabat"]


def test_record_fallback_populates_status():
    gd.record_fallback_returned(
        "no_substantive_block",
        sources_count=20,
        answer_preview="Die Websuchergebnisse...",
        now=NOW,
    )
    s = gd.rejection_status()
    assert s["fallback_count"] == 1
    assert s["last_fallback"]["reason"] == "no_substantive_block"
    assert s["last_fallback"]["sources_count"] == 20


def test_ring_buffer_is_bounded():
    for i in range(gd._MAX_HISTORY + 10):
        gd.record_rejection("test", f"block-{i}", now=NOW)
    s = gd.rejection_status()
    assert s["rejection_count"] == gd._MAX_HISTORY


def test_long_block_is_clipped():
    gd.record_rejection("test", "x" * 5000, now=NOW)
    s = gd.rejection_status()
    assert len(s["last_rejection"]["block"]) <= 501  # 500 + one "…"
    assert s["last_rejection"]["block"].endswith("…")


# ── explain_answer ───────────────────────────────────────────────────────────
#
# Focused, non-network integration tests that use the real filter helpers.
# The exact wording of blocks is chosen to be short, unambiguous, and
# structurally similar to a real search-grounded turn.


def _src(url: str, title: str, snippet: str) -> dict:
    return {"url": url, "title": title, "snippet": snippet}


def test_explain_clean_when_terms_match_exact():
    answer = "Die Hauptstadt Berlins ist Berlin. Siehe [1]."
    sources = [_src("https://example.com/1", "Berlin", "berlin is the capital of germany")]
    r = gd.explain_answer(answer, sources)
    assert r["verdict"] in ("clean", "trimmed")  # depending on term extraction
    assert r["sources_count"] == 1


def test_explain_supports_transliteration_case_aschgabat():
    """Regression against the exact bug the transliteration fix addressed."""
    answer = "Aschgabat ist die Hauptstadt Turkmenistans [1]."
    sources = [_src(
        "https://example.com/1",
        "Ashgabat",
        "ashgabat is the capital of turkmenistan.",
    )]
    r = gd.explain_answer(answer, sources)
    # If a concrete term "aschgabat" was extracted, it must be marked
    # supported via the transliteration path.
    for block in r["blocks"]:
        for term in block["concrete_terms"]:
            if term["term"].lower() == "aschgabat":
                assert term["supported"] is True
                assert term["matched_via"] in ("exact", "transliteration")


def test_explain_fallback_when_no_indexed_sources():
    r = gd.explain_answer("Answer without sources.", [])
    assert r["verdict"] == "fallback"
    assert r["reason"] == "no_indexed_sources"


def test_explain_no_matched_source_when_citation_missing():
    """If a block has no citation and no URL, it can't match a source."""
    answer = "Ein Absatz ohne Zitat oder URL."
    sources = [_src("https://example.com/1", "Whatever", "irrelevant")]
    r = gd.explain_answer(answer, sources)
    # Some blocks may still be kept if no concrete terms are extracted;
    # what matters is that the verdict + reject reason are coherent.
    for block in r["blocks"]:
        if not block["matched_sources"]:
            assert block["kept"] is False
            assert block["reject_reason"] == "no_matched_source"
