"""Semantic turn-taking: decide whether you're actually *done* talking, not
just whether you paused. Voice-activity detection only hears silence; a human
knows "I was thinking maybe we could…" isn't finished. This reads the words.

Approximates the model-based approach (LiveKit / Pipecat smart-turn) with a
fast heuristic — no extra model, no added latency.
"""

from __future__ import annotations

import re

# "quiet, I'm still going" — spoken to hold the floor. We stay silent and keep
# listening; the phrase itself is dropped, not answered.
_HOLD = re.compile(
    r"^\s*(wait|hold on|hold up|hang on|one sec(ond)?|give me (a )?(sec|second|moment|minute)|"
    r"let me (finish|think|speak)|not (yet|done)|i'?m not (done|finished)|"
    r"shush|quiet|stop talking|listen)\b[\s.!,]*$",
    re.IGNORECASE,
)

# if the utterance ends on one of these, the thought is unfinished → wait
_TRAILING = {
    "and", "but", "or", "so", "because", "if", "when", "while", "as", "than",
    "to", "of", "for", "with", "at", "by", "from", "in", "on", "about", "into",
    "the", "a", "an", "my", "your", "our", "their", "his", "her", "its", "that",
    "this", "these", "those", "some", "any", "i", "we", "you", "they", "he",
    "she", "it", "is", "are", "was", "were", "am", "be", "been", "will",
    "would", "could", "should", "can", "may", "might", "do", "does", "did",
    "have", "has", "had", "gonna", "wanna", "gotta", "let", "like", "just",
    "um", "uh", "er", "hmm", "well", "actually", "maybe", "also", "plus",
    "then", "which", "who", "what", "where", "how", "very", "really", "kind",
    "sort", "i'm", "i'll", "i've", "we're", "it's", "there's",
}


def _last_word(text: str) -> str:
    words = re.findall(r"[a-z']+", text.lower())
    return words[-1] if words else ""


def classify(text: str) -> str:
    """'hold' | 'incomplete' | 'complete'."""
    stripped = text.strip()
    if not stripped:
        return "incomplete"
    if _HOLD.match(stripped):
        return "hold"
    # ended cleanly on . ! ? and isn't dangling → done
    ends_clean = stripped[-1] in ".!?"
    last = _last_word(stripped)
    if last in _TRAILING:
        return "incomplete"
    if stripped[-1] == "," or stripped.endswith("-"):
        return "incomplete"
    # very short with no terminal punctuation is usually a mid-thought fragment,
    # unless it's a normal short reply/command (yes/no/stop/thanks…)
    if not ends_clean and len(stripped.split()) <= 2 and last not in _SHORT_OK:
        return "incomplete"
    return "complete"


_SHORT_OK = {
    "yes", "yeah", "yep", "no", "nope", "nah", "stop", "thanks", "thank",
    "cheers", "ok", "okay", "sure", "hi", "hey", "hello", "bye", "goodbye",
    "please", "correct", "right", "wrong", "next", "continue", "go", "done",
}
