"""Deterministic claim-level filtering after web search."""

from src.agent_loop import (
    _concrete_grounding_terms,
    _filter_web_grounded_answer,
)


SOURCES = [
    {
        "url": "https://example.test/eu-ai",
        "title": "EU-Kommission veröffentlicht Leitlinien für KI-Modelle",
        "snippet": (
            "Die EU-Kommission veröffentlichte Leitlinien für Anbieter "
            "allgemeiner KI-Modelle."
        ),
        "evidence": (
            "Die Leitlinien erläutern Pflichten für Anbieter allgemeiner "
            "KI-Modelle im Rahmen des AI Act."
        ),
    },
    {
        "url": "https://example.test/ki-thema",
        "title": "Künstliche Intelligenz: aktuelle Nachrichten",
        "snippet": (
            "Nachrichten und Hintergründe zum Thema künstliche Intelligenz."
        ),
    },
]


def test_keeps_claim_supported_by_its_direct_source():
    answer = (
        "1. **EU-Kommission veröffentlicht KI-Leitlinien.** "
        "Die Leitlinien betreffen Anbieter allgemeiner KI-Modelle. "
        "[Quelle](https://example.test/eu-ai)"
    )

    filtered = _filter_web_grounded_answer(answer, SOURCES)

    assert "EU-Kommission" in filtered
    assert "allgemeiner KI-Modelle" in filtered
    assert "https://example.test/eu-ai" in filtered


def test_drops_hallucinated_item_even_with_real_topic_page():
    answer = (
        "1. **EU-Kommission veröffentlicht KI-Leitlinien.** "
        "Die Leitlinien betreffen Anbieter allgemeiner KI-Modelle. "
        "[Quelle](https://example.test/eu-ai)\n\n"
        "2. **Anthropic veröffentlicht Mythos 5.** "
        "Das neue Modell Mythos 5 wurde heute vorgestellt. "
        "[Quelle](https://example.test/ki-thema)"
    )

    filtered = _filter_web_grounded_answer(answer, SOURCES)

    assert "EU-Kommission" in filtered
    assert "Anthropic" not in filtered
    assert "Mythos 5" not in filtered
    assert "Weitere Aussagen" in filtered


def test_fails_closed_when_every_claim_is_unsupported():
    answer = (
        "1. Anthropic veröffentlicht Mythos 5. "
        "[Quelle](https://example.test/ki-thema)"
    )

    filtered = _filter_web_grounded_answer(answer, SOURCES)

    assert "Anthropic" not in filtered
    assert "Mythos 5" not in filtered
    assert "nicht genügend" in filtered


def test_rejects_unknown_citation_url():
    answer = (
        "1. OpenAI veröffentlicht Modell 99. "
        "[Quelle](https://unknown.test/story)"
    )

    filtered = _filter_web_grounded_answer(answer, SOURCES)

    assert "OpenAI" not in filtered
    assert "Modell 99" not in filtered
    assert "nicht genügend" in filtered


def test_does_not_accept_evidence_from_a_different_source():
    answer = (
        "1. Anthropic veröffentlicht Mythos 5. "
        "[Quelle](https://example.test/ki-thema)"
    )
    sources = SOURCES + [{
        "url": "https://example.test/unrelated",
        "title": "Anthropic veröffentlicht Mythos 5",
        "snippet": "Anthropic stellte das Modell Mythos 5 vor.",
    }]

    filtered = _filter_web_grounded_answer(answer, sources)

    assert "Anthropic" not in filtered
    assert "Mythos 5" not in filtered


def test_accepts_trailing_slash_variant_of_known_url():
    answer = (
        "1. **EU-Kommission veröffentlicht KI-Leitlinien.** "
        "Die Leitlinien betreffen Anbieter allgemeiner KI-Modelle. "
        "[Quelle](https://example.test/eu-ai/)"
    )

    filtered = _filter_web_grounded_answer(answer, SOURCES)

    assert "EU-Kommission" in filtered
    assert "https://example.test/eu-ai/" in filtered


def test_keeps_claim_with_numbered_source_citation():
    answer = (
        "1. **EU-Kommission veröffentlicht KI-Leitlinien.** "
        "Die Leitlinien betreffen Anbieter allgemeiner KI-Modelle. [1]"
    )

    filtered = _filter_web_grounded_answer(answer, SOURCES)

    assert "EU-Kommission" in filtered
    assert "allgemeiner KI-Modelle" in filtered
    assert "[1]" in filtered


def test_rejects_unknown_numbered_source_citation():
    answer = (
        "1. OpenAI veröffentlicht Modell 99. [99]"
    )

    filtered = _filter_web_grounded_answer(answer, SOURCES)

    assert "OpenAI" not in filtered
    assert "Modell 99" not in filtered
    assert "nicht genügend" in filtered


def test_numbered_citation_does_not_borrow_other_source_evidence():
    answer = (
        "1. Anthropic veröffentlicht Mythos 5. [2]"
    )
    sources = SOURCES + [{
        "url": "https://example.test/unrelated",
        "title": "Unrelated",
        "snippet": "Anthropic stellte das Modell Mythos 5 vor.",
    }]

    filtered = _filter_web_grounded_answer(answer, sources)

    assert "Anthropic" not in filtered
    assert "Mythos 5" not in filtered


def test_source_only_line_is_not_a_substantive_news_item():
    answer = (
        "Die Transparenzregeln gelten seit dem 2. August 2026.\n\n"
        "Quelle: [EU AI Act Transparency Obligations: Preparing for "
        "Compliance by 2 August 2026]"
        "(https://example.test/eu-ai)"
    )

    filtered = _filter_web_grounded_answer(answer, SOURCES)

    assert filtered == (
        "Die Websuchergebnisse enthalten nicht genügend ausdrücklich "
        "belegte Informationen für eine verlässliche Meldung."
    )
    assert "Quelle:" not in filtered


def test_bare_url_slug_is_not_treated_as_claim_evidence():
    url = (
        "https://example.test/news/"
        "ki-kennzeichnung-ab-2-august-in-der-eu-933780"
    )
    sources = [{
        "url": url,
        "title": "Neue Transparenzpflichten",
        "snippet": (
            "Ab dem 2. August 2026 müssen Anbieter und professionelle "
            "Nutzer in der EU KI-generierte Inhalte klar kennzeichnen."
        ),
    }]
    answer = (
        "Ab dem 2. August 2026 müssen Anbieter und professionelle "
        "Nutzer in der EU KI-generierte Inhalte klar kennzeichnen. "
        f"Quelle: {url}"
    )

    filtered = _filter_web_grounded_answer(answer, sources)

    assert "Anbieter und professionelle Nutzer" in filtered
    assert "KI-generierte Inhalte" in filtered
    assert url in filtered
    assert "nicht genügend" not in filtered


def test_duplicate_grounded_news_blocks_are_collapsed():
    url = "https://example.test/eu-ai"
    sources = [{
        "url": url,
        "title": "Neue Transparenzpflichten",
        "snippet": (
            "Ab dem 2. August 2026 müssen Anbieter und professionelle "
            "Nutzer in der EU KI-generierte Inhalte klar kennzeichnen."
        ),
    }]
    item = (
        "Ab dem 2. August 2026 müssen Anbieter und professionelle "
        "Nutzer in der EU KI-generierte Inhalte klar kennzeichnen. "
        f"[Quelle]({url})"
    )
    answer = item + "\n\n" + item

    filtered = _filter_web_grounded_answer(answer, sources)

    assert filtered.count("Ab dem 2. August 2026") == 1
    assert filtered.count(url) == 1
    assert "nicht genügend" not in filtered

def test_accepts_grounded_german_translation_of_english_evidence():
    url = "https://example.test/article-50"
    sources = [{
        "url": url,
        "title": "EU AI Act Transparency Obligations",
        "snippet": (
            "From 2 August 2026, organisations will become subject to the "
            "transparency obligations set out in Article 50 of the EU AI Act "
            "(Regulation (EU) 2024/1689)."
        ),
    }]
    answer = (
        "Ab 2. August 2026 unterliegen Organisationen den in Artikel 50 "
        "der EU-KI-Verordnung (Regulation (EU) 2024/1689) festgelegten "
        f"Transparenzverpflichtungen. [Quelle]({url})"
    )

    filtered = _filter_web_grounded_answer(answer, sources)

    assert "Artikel 50" in filtered
    assert "2024/1689" in filtered
    assert url in filtered
    assert "nicht genügend" not in filtered


def test_sentence_initial_ab_is_not_a_concrete_name():
    terms = _concrete_grounding_terms(
        "Ab 2. August 2026 gelten neue Anforderungen."
    )

    assert "ab 2" not in terms
    assert "august 2026" in terms
    assert "2026" in terms



def test_matches_unique_named_source_label():
    sources = [
        {
            "url": "https://example.test/ihk-ai-act",
            "title": (
                "EU AI Act: Transparenzpflichten | "
                "IHK Nürnberg für Mittelfranken"
            ),
            "snippet": (
                "Ab dem 2. August 2026 gelten nach Artikel 50 "
                "Transparenzpflichten für AI systems."
            ),
        },
        {
            "url": "https://example.test/other",
            "title": "Andere Meldung",
            "snippet": "Ein anderes Thema.",
        },
    ]
    answer = (
        "Ab dem 2. August 2026 gelten nach Artikel 50 des "
        "EU-KI-Gesetzes Transparenzpflichten für KI-Systeme. "
        "Quelle: IHK Nürnberg für Mittelfranken."
    )

    filtered = _filter_web_grounded_answer(answer, sources)

    assert "Artikel 50" in filtered
    assert "EU-KI-Gesetzes" in filtered
    assert "KI-Systeme" in filtered
    assert "https://example.test/ihk-ai-act" in filtered
    assert "nicht genügend" not in filtered


def test_rejects_ambiguous_named_source_label():
    sources = [
        {
            "url": "https://example.test/first",
            "title": "KI-Meldung | Beispiel Verlag",
            "snippet": "Artikel 50 gilt ab dem 2. August 2026.",
        },
        {
            "url": "https://example.test/second",
            "title": "Weitere KI-Meldung | Beispiel Verlag",
            "snippet": "Artikel 50 gilt ab dem 2. August 2026.",
        },
    ]
    answer = (
        "Artikel 50 gilt ab dem 2. August 2026. "
        "Quelle: Beispiel Verlag."
    )

    filtered = _filter_web_grounded_answer(answer, sources)

    assert filtered == (
        "Die Websuchergebnisse enthalten nicht genügend ausdrücklich "
        "belegte Informationen für eine verlässliche Meldung."
    )
