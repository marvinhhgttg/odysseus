from __future__ import annotations

from enum import StrEnum


class RiskLevel(StrEnum):
    READ = "read"
    DRAFT = "draft"
    WRITE_CONFIRM = "write_confirm"
    DANGEROUS = "dangerous"


WRITE_WORDS = (
    "sende",
    "verschicke",
    "verschieben",
    "verschiebe",
    "lösche",
    "loesche",
    "umbenennen",
    "benenne",
    "erstelle termin",
    "termin anlegen",
    "kalender ändern",
    "kalender aendern",
    "task anlegen",
    "aufgabe anlegen",
    "git commit",
    "git push",
    "überschreibe",
    "ueberschreibe",
)


def classify_risk(user_text: str) -> RiskLevel:
    normalized = user_text.casefold()
    if any(word in normalized for word in WRITE_WORDS):
        return RiskLevel.WRITE_CONFIRM
    return RiskLevel.READ
