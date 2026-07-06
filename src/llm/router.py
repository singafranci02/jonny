"""Cheap heuristic: which model tier does this turn need?"""

from __future__ import annotations


def pick_tier(user_message: str, routing_cfg: dict) -> str:
    text = user_message.lower()
    if any(kw in text for kw in routing_cfg.get("hard_keywords", [])):
        return "hard"
    if len(text.split()) >= routing_cfg.get("min_words_hard", 80):
        return "hard"
    return "default"
