"""Regression coverage for web-search citation grounding."""

from pathlib import Path


SOURCE = Path("src/agent_loop.py").read_text(encoding="utf-8")


def test_web_search_tool_prompt_requires_claim_level_grounding():
    assert SOURCE.count(
        "WEB-SEARCH GROUNDING — mandatory after every web_search:"
    ) == 1
    assert (
        "A source title, URL, source-list entry, or general topic page "
        "does not by itself support a claim."
    ) in SOURCE
    assert (
        "Never invent another example merely to satisfy a requested count."
    ) in SOURCE


def test_force_answer_synthesis_preserves_web_grounding():
    marker = "Using ONLY the information already gathered above"
    start = SOURCE.index(marker)
    synthesis = SOURCE[start:start + 2500]

    assert "every concrete factual claim must be explicitly" in synthesis
    assert "supported by the returned search text" in synthesis
    assert (
        "source-list entry, or general topic page alone is not"
    ) in synthesis
    assert (
        "fewer items and state the limitation instead of inventing"
    ) in synthesis

def test_grace_synthesis_uses_single_completion_path():
    marker = "_synth = _strip_think_blocks"
    start = SOURCE.index(marker)
    synthesis_path = SOURCE[start:start + 2200]

    assert "round_response = _synth" in synthesis_path
    assert "_grace_synthesized = True" in synthesis_path
    assert 'json.dumps({"delta": _synth})' not in synthesis_path
    assert "full_response += _synth" not in synthesis_path

