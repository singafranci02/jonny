"""Cheap heuristics: which model tier / mode does this turn need?"""

from __future__ import annotations

import re

RESEARCH_TRIGGERS = [
    r"\bresearch\b",
    r"\bdeep dive\b",
    r"\binvestigate\b",
    r"\bwrite (?:me )?a report\b",
    r"\blook into\b",
]


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
            topic = user_message[match.end():].strip(" :,-—")
            # "research X" needs an actual X; "can you research it" does not count
            if len(topic.split()) >= 2:
                return topic
    return None
