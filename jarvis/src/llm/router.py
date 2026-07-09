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


# hints that a turn probably needs tools (time, web, memory, math...)
TOOL_HINTS = (
    "time", "date", "today", "tomorrow", "weather", "news", "latest",
    "price", "stock", "search", "look up", "google", "remember", "remind",
    "calculate", "convert", "my notes", "notes say", "knowledge",
    "research", "investigate",
    # workspace file requests must never take the tool-less fast path
    "file", "save", "write", "note", "doc", "workspace", "folder",
    "edit", "rename", "read me",
)


_SMALLTALK = re.compile(
    r"^(hey|hi|hello|yo|sup|what'?s up|how are you|how's it going|good (morning|afternoon|evening|night)|"
    r"thanks?|thank you|ok(ay)?|cool|nice|yes|no|yeah|nah|bye|goodbye|see you|cheers)\b",
    re.IGNORECASE,
)


def is_smalltalk(user_message: str) -> bool:
    """Greetings/acknowledgements: answer directly, inject no notes/memories
    (retrieval on 'hey what's up' only drags in irrelevant context)."""
    words = user_message.split()
    return len(words) <= 7 and bool(_SMALLTALK.match(user_message.strip()))


def is_simple(user_message: str, tier: str) -> bool:
    """Short chit-chat that needs no tools: answer on the slim fast path
    (skipping tool schemas roughly halves local prefill time)."""
    if tier != "default":
        return False
    if len(user_message.split()) > 14:
        return False
    lower = user_message.lower()
    return not any(h in lower for h in TOOL_HINTS)


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
