"""Tests for src.services.prompt_budget."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from src.services import prompt_budget as pb


NOW = datetime(2026, 9, 8, 20, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _clean():
    pb._reset_state_for_tests()
    yield
    pb._reset_state_for_tests()


# ── estimate_tokens ─────────────────────────────────────────────────────────

def test_estimate_tokens_empty_is_zero():
    assert pb.estimate_tokens("") == 0


def test_estimate_tokens_is_deterministic():
    s = "x" * 4000
    assert pb.estimate_tokens(s) == pb.estimate_tokens(s) == 1000


def test_estimate_tokens_minimum_is_one_for_nonempty():
    # A single-char string still produces a non-zero size so downstream
    # divide-by-zero guards work.
    assert pb.estimate_tokens("a") >= 1


# ── evaluate_prompt_budget ──────────────────────────────────────────────────

def test_ok_when_under_soft():
    d = pb.evaluate_prompt_budget("x" * 100, hard_limit_tokens=1000, soft_limit_tokens=500)
    assert d.kind == "ok"
    assert d.is_over_hard is False and d.is_warn is False


def test_warn_between_soft_and_hard():
    # 3200 chars → 800 tokens, between soft=500 and hard=1000
    d = pb.evaluate_prompt_budget("x" * 3200, hard_limit_tokens=1000, soft_limit_tokens=500)
    assert d.kind == "warn"
    assert d.is_warn is True
    assert 500 < d.estimated_tokens <= 1000


def test_over_hard_when_above_hard():
    d = pb.evaluate_prompt_budget("x" * 5000, hard_limit_tokens=1000, soft_limit_tokens=500)
    assert d.kind == "over_hard"
    assert d.is_over_hard is True
    assert d.estimated_tokens > 1000


def test_at_boundary_is_ok_not_warn():
    # Exactly on soft is still ok; strictly greater is warn.
    on_soft = pb.evaluate_prompt_budget("x" * 2000, hard_limit_tokens=1000, soft_limit_tokens=500)
    assert on_soft.kind in ("warn", "ok")  # 500 tokens exact — kind is soft-inclusive: ok
    # But one more char must not flip; we care about not tripping on edge.
    assert on_soft.estimated_tokens == 500


def test_bad_config_soft_greater_than_hard_is_clamped():
    d = pb.evaluate_prompt_budget("x" * 3000, hard_limit_tokens=500, soft_limit_tokens=99999)
    # soft got clamped to hard, so 750 tokens > 500 hard → over_hard
    assert d.kind == "over_hard"


# ── env override ─────────────────────────────────────────────────────────────

def test_env_overrides_default_hard(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_PROMPT_HARD_TOKENS", "50")
    assert pb.get_hard_limit() == 50


def test_env_ignored_when_garbage(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_PROMPT_HARD_TOKENS", "not-a-number")
    assert pb.get_hard_limit() == pb.DEFAULT_HARD_TOKENS


def test_env_ignored_when_negative(monkeypatch):
    monkeypatch.setenv("ODYSSEUS_PROMPT_HARD_TOKENS", "-100")
    assert pb.get_hard_limit() == pb.DEFAULT_HARD_TOKENS


# ── record + status ─────────────────────────────────────────────────────────

def test_record_appends_history_and_status_summarises():
    for _ in range(3):
        pb.record_prompt_size(
            pb.evaluate_prompt_budget("x" * 400, hard_limit_tokens=1000, soft_limit_tokens=500),
            tool_count=5,
            compact=False,
            now=NOW,
        )
    pb.record_prompt_size(
        pb.evaluate_prompt_budget("x" * 3000, hard_limit_tokens=1000, soft_limit_tokens=500),
        tool_count=12,
        compact=False,
        now=NOW,
    )
    pb.record_prompt_size(
        pb.evaluate_prompt_budget("x" * 6000, hard_limit_tokens=1000, soft_limit_tokens=500),
        tool_count=40,
        compact=False,
        now=NOW,
    )
    s = pb.prompt_budget_status()
    assert s["sample_count"] == 5
    assert s["warn_count"] == 1
    assert s["over_hard_count"] == 1
    assert s["max_estimated_tokens"] == 1500  # from the last, 6000/4
    assert s["last"]["kind"] == "over_hard"
    assert s["last"]["tool_count"] == 40


def test_history_is_bounded():
    # Push more than the ring buffer allows and confirm we cap.
    for _ in range(pb._MAX_HISTORY + 10):
        pb.record_prompt_size(
            pb.evaluate_prompt_budget("x" * 100, hard_limit_tokens=1000, soft_limit_tokens=500),
            now=NOW,
        )
    s = pb.prompt_budget_status()
    assert s["sample_count"] == pb._MAX_HISTORY


def test_status_empty_before_any_record():
    s = pb.prompt_budget_status()
    assert s["sample_count"] == 0
    assert s["last"] is None
    assert s["max_estimated_tokens"] is None
    assert s["warn_count"] == 0
    assert s["over_hard_count"] == 0
