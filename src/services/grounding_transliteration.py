"""Transliteration-tolerant matching for the web-grounding filter.

Odysseus's web-grounding filter compares "concrete grounding terms" (proper
names, product identifiers, quoted expressions) extracted from a model answer
against the citation evidence. It runs on lower-cased whitespace-normalised
strings and requires substring containment.

That breaks in a very predictable way for German answers, because the model
often uses the German transliteration of a name while the fetched web source
uses the original / English form:

    answer:   "Aschgabat"    evidence: "Ashgabat"    -> not supported
    answer:   "Tokio"        evidence: "Tokyo"       -> not supported
    answer:   "Peking"       evidence: "Beijing"     -> not supported
    answer:   "Moskau"       evidence: "Moscow"      -> not supported
    answer:   "Tschechien"   evidence: "Czechia"     -> not supported

The end user sees the correct answer replaced by the German
"Websuchergebnisse enthalten nicht genügend Evidenz" fallback, even though
the plan/chain-of-thought shows the sources were read correctly.

This module supplies two conservative helpers:

- transliteration_variants(name): returns the set of alternative spellings the
  grounding matcher should also try. Purely rule-based, deterministic, no
  fuzzy scoring: sch<->sh, w<->v inside a word, k<->c before "a/o/u/r", ph<->f,
  final -y <-> -ie, and a small table of well-known exonyms.

- name_supported_by_evidence(name, canonical_evidence): returns True when
  either the name itself or *any* of its transliteration variants occurs as a
  substring inside the already-canonicalised evidence text.

Kept intentionally small and English/German-only. Adding more languages is a
conscious change: this table decides what evidence-mismatches Odysseus will
silently accept.
"""
from __future__ import annotations

import re
from typing import Iterable, Set


# Well-known exonym pairs. The left side is the German or common German-media
# spelling; the right side is the English/original spelling that Wikipedia,
# Reuters, AP, and most English sources use. Each pair is bidirectional.
#
# Only enter a pair here when both spellings really refer to the same entity
# and there is no risk of collision with an unrelated name. When in doubt,
# leave it out — a false-positive here silently accepts a mismatched citation.
_EXONYMS: tuple[tuple[str, str], ...] = (
    ("aschgabat", "ashgabat"),
    ("aschgabat", "asgabat"),   # Aşgabat, ASCII-only
    ("tokio", "tokyo"),
    ("peking", "beijing"),
    ("moskau", "moscow"),
    ("prag", "prague"),
    ("warschau", "warsaw"),
    ("kiew", "kyiv"),
    ("kiew", "kiev"),
    ("belgrad", "belgrade"),
    ("bukarest", "bucharest"),
    ("kopenhagen", "copenhagen"),
    ("lissabon", "lisbon"),
    ("mailand", "milan"),
    ("mailand", "milano"),
    ("rom", "rome"),
    ("neapel", "naples"),
    ("neapel", "napoli"),
    ("venedig", "venice"),
    ("venedig", "venezia"),
    ("genf", "geneva"),
    ("basel", "basel"),
    ("zuerich", "zurich"),
    ("bruegge", "bruges"),
    ("bruessel", "brussels"),
    ("den haag", "the hague"),
    ("den haag", "hague"),
    ("tschechien", "czechia"),
    ("tschechische republik", "czech republic"),
    ("weissrussland", "belarus"),
    ("kroatien", "croatia"),
    ("kroatien", "hrvatska"),
)


def _build_exonym_map() -> dict[str, set[str]]:
    m: dict[str, set[str]] = {}
    for a, b in _EXONYMS:
        m.setdefault(a, set()).add(b)
        m.setdefault(b, set()).add(a)
    return m


_EXONYM_MAP = _build_exonym_map()


def _rule_based_variants(name: str) -> Iterable[str]:
    """Deterministic rule-based transliteration variants.

    Rules produce alternative spellings for the *same* name; they do not
    generate cross-name matches. Each rule is bidirectional. Rules only apply
    when they would actually change the string, so a name that doesn't match
    any rule yields nothing here.
    """
    variants: set[str] = set()

    # sch <-> sh (Aschgabat/Ashgabat, Puschkin/Pushkin)
    if "sch" in name:
        variants.add(name.replace("sch", "sh"))
    if "sh" in name and "sch" not in name:
        variants.add(name.replace("sh", "sch"))

    # ph <-> f (German transliteration of Greek names)
    if "ph" in name:
        variants.add(name.replace("ph", "f"))
    # We deliberately do not reverse "f -> ph" — it fires far too often on
    # unrelated German words.

    # Word-internal w <-> v (Slavic names, e.g. Warschau/Warsaw, Kiew/Kiev).
    # Only apply after position 0 to avoid W-initial German words that are
    # not transliterations (Wien != Vien).
    if len(name) >= 2 and "w" in name[1:]:
        head, tail = name[0], name[1:].replace("w", "v")
        variants.add(head + tail)
    if len(name) >= 2 and "v" in name[1:]:
        head, tail = name[0], name[1:].replace("v", "w")
        variants.add(head + tail)

    # German "ü" written as "ue" (Zürich <-> Zuerich, Nürnberg <-> Nuernberg).
    if "ue" in name:
        variants.add(name.replace("ue", "ü"))
    if "ü" in name:
        variants.add(name.replace("ü", "ue"))
    if "ö" in name:
        variants.add(name.replace("ö", "oe"))
    if "oe" in name:
        variants.add(name.replace("oe", "ö"))
    if "ä" in name:
        variants.add(name.replace("ä", "ae"))
    if "ae" in name:
        variants.add(name.replace("ae", "ä"))
    if "ß" in name:
        variants.add(name.replace("ß", "ss"))

    variants.discard(name)
    return variants


def transliteration_variants(name: str) -> Set[str]:
    """Return alternative spellings to try when matching name in evidence.

    Always returns a set that does NOT include the input itself. The caller
    should try the input first, then this set.
    """
    if not name:
        return set()
    lowered = re.sub(r"\s+", " ", name).strip().casefold()
    variants: set[str] = set()

    # Exonym table pass — direct lookup, both directions.
    variants.update(_EXONYM_MAP.get(lowered, set()))

    # Rule-based pass — apply to both the original and each exonym variant,
    # so "Zuerich" (rule ue->ü) and "Zurich" (exonym) both produce the same
    # canonical target when different sources use different spellings.
    seeds = {lowered, *variants}
    for seed in seeds:
        variants.update(_rule_based_variants(seed))

    variants.discard(lowered)
    return variants


def name_supported_by_evidence(name: str, canonical_evidence: str) -> bool:
    """True when `name` or one of its transliteration variants is in evidence.

    `canonical_evidence` must already be lower-cased and whitespace-normalised
    (i.e. run through the caller's canonicalisation pipeline). This function
    does not re-normalise the evidence to keep the caller in control of that
    step.
    """
    if not name or not canonical_evidence:
        return False
    lowered = re.sub(r"\s+", " ", name).strip().casefold()
    if lowered in canonical_evidence:
        return True
    for variant in transliteration_variants(lowered):
        if variant in canonical_evidence:
            return True
    return False
