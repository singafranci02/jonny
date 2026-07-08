"""Cheap heuristics: which model tier / mode does this turn need?"""

from __future__ import annotations

import re

RESEARCH_TRIGGERS = [
    r"\b(?:do\s+(?:some\s+)?)?research(?:\s+(?:on|about|into))?\b",
    r"\bdeep dive(?:\s+(?:on|into))?\b",
    r"\binvestigate\b",
    r"\bwrite (?:me )?a report (?:on|about)\b",
    r"\blook into\b",
    r"\bfind out (?:everything |all )?(?:about|on)\b",
    r"\bgive me a (?:full |detailed )?(?:rundown|breakdown) (?:on|of|about)\b",
]

_NON_TOPICS = {"it", "this", "that", "them", "these", "those", "him", "her"}


def pick_tier(user_message: str, routing_cfg: dict) -> str:
    text = user_message.lower()
    if any(kw in text for kw in routing_cfg.get("hard_keywords", [])):
        return "hard"
    if len(text.split()) >= routing_cfg.get("min_words_hard", 80):
        return "hard"
    return "default"


def detect_research(user_message: str) -> str | None:
    """Returns the research topic if this is a research request, else None."""
    text = user_message.lower()
    for pattern in RESEARCH_TRIGGERS:
        match = re.search(pattern, text)
        if match:
            topic = user_message[match.end():].strip(" :,-—.?!")
            words = topic.split()
            # "research crypto" counts; "can you research it" does not
            if words and topic.lower() not in _NON_TOPICS:
                return topic
    return None
