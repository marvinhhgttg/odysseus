"""Tests for src.services.grounding_transliteration."""
from __future__ import annotations

import pytest

from src.services.grounding_transliteration import (
    transliteration_variants,
    name_supported_by_evidence,
)


# ── transliteration_variants ────────────────────────────────────────────────

def test_aschgabat_yields_ashgabat():
    v = transliteration_variants("Aschgabat")
    assert "ashgabat" in v


def test_ashgabat_yields_aschgabat():
    v = transliteration_variants("Ashgabat")
    assert "aschgabat" in v


def test_asgabat_ascii_variant_is_covered():
    # Wikipedia and news sources sometimes strip diacritics from Aşgabat.
    v = transliteration_variants("Aschgabat")
    assert "asgabat" in v


def test_tokio_yields_tokyo_and_reverse():
    assert "tokyo" in transliteration_variants("Tokio")
    assert "tokio" in transliteration_variants("Tokyo")


def test_peking_yields_beijing_and_reverse():
    assert "beijing" in transliteration_variants("Peking")
    assert "peking" in transliteration_variants("Beijing")


def test_kiew_yields_kyiv_and_kiev():
    v = transliteration_variants("Kiew")
    assert "kyiv" in v or "kiev" in v
    # Bidirectional
    assert "kiew" in transliteration_variants("Kyiv")


def test_zuerich_yields_zurich_via_ue_rule():
    v = transliteration_variants("Zuerich")
    assert "zürich" in v


def test_umlaut_expansion():
    assert "muenchen" in transliteration_variants("München")
    assert "müller" in transliteration_variants("Mueller")


def test_no_variants_returns_empty_for_plain_word():
    # A generic word with no rule triggers and no exonym: no variants.
    v = transliteration_variants("apple")
    assert v == set()


def test_variants_never_contain_input():
    for name in ("Aschgabat", "Ashgabat", "Kiew", "Tokio", "München"):
        v = transliteration_variants(name)
        assert name.casefold() not in v


def test_empty_input_yields_empty_variants():
    assert transliteration_variants("") == set()
    assert transliteration_variants("   ") == set()


# Regression: rule "internal w -> v" must not fire at position 0
def test_wien_is_not_treated_as_vien_transliteration():
    # "Wien" has no reason to be turned into "Vien"; the rule only applies
    # to w/v after position 0.
    v = transliteration_variants("Wien")
    assert "vien" not in v


# ── name_supported_by_evidence ──────────────────────────────────────────────

def test_supported_when_direct_substring():
    assert name_supported_by_evidence("Aschgabat", "die hauptstadt aschgabat liegt am fuß") is True


def test_supported_via_exonym_lookup():
    # The exact Turkmenistan reproduction from the user's session.
    evidence = "ashgabat is the capital and largest city of turkmenistan."
    assert name_supported_by_evidence("Aschgabat", evidence) is True


def test_supported_via_reverse_exonym():
    evidence = "aschgabat ist die hauptstadt von turkmenistan."
    assert name_supported_by_evidence("Ashgabat", evidence) is True


def test_supported_tokio_vs_tokyo():
    assert name_supported_by_evidence("Tokio", "tokyo tower is a landmark") is True
    assert name_supported_by_evidence("Tokyo", "der tokio tower ist eine sehenswürdigkeit") is True


def test_not_supported_when_completely_absent():
    assert name_supported_by_evidence("Aschgabat", "berlin ist die hauptstadt deutschlands") is False


def test_empty_inputs_are_not_supported():
    assert name_supported_by_evidence("", "some evidence") is False
    assert name_supported_by_evidence("Aschgabat", "") is False


def test_no_false_positive_from_partial_letter_overlap():
    # "Berlin" must not match "Beirut" just because letters overlap.
    assert name_supported_by_evidence("Berlin", "beirut is the capital of lebanon") is False


def test_muenchen_matches_muenchen_and_muenchen():
    # Both spellings should be accepted.
    for evidence in (
        "münchen ist die hauptstadt bayerns",
        "muenchen ist die hauptstadt bayerns",
    ):
        assert name_supported_by_evidence("München", evidence) is True
        assert name_supported_by_evidence("Muenchen", evidence) is True
