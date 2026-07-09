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


# words a sentence grammatically cannot end on — force "incomplete" even if
# whisper stuck a period on the fragment
_STRICT = {
    "the", "a", "an", "my", "your", "our", "their", "his", "its", "and", "or",
    "but", "so", "to", "of", "for", "with", "at", "by", "from", "in", "on",
    "about", "into", "than", "as", "is", "are", "was", "were", "am", "be",
    "will", "would", "could", "should", "can", "may", "might", "do", "does",
    "did", "have", "has", "had", "gonna", "wanna", "gotta", "let", "very",
    "i", "we", "they",
}


def _last_word(text: str) -> str:
    words = re.findall(r"[a-z']+", text.lower())
    return words[-1] if words else ""


def classify(text: str) -> str:
    """'hold' | 'incomplete' | 'complete'.

    Uses the DistilBERT completion model when it's loaded, combined with
    whisper's punctuation (which is authoritative for sentence ends and
    covers the few phrases the model over-holds). Falls back to a pure
    heuristic when the model isn't available."""
    stripped = text.strip()
    if not stripped:
        return "incomplete"
    if _HOLD.match(stripped):
        return "hold"

    from . import turndetect

    terminal = stripped[-1] in ".!?"
    last = _last_word(stripped)
    strict = last in _STRICT           # can't grammatically end a sentence
    soft = last in _TRAILING and not strict  # e.g. "um", "well", "maybe"
    prob = turndetect.completion_probability(stripped)

    # a sentence literally can't end on "to/the/and…" — hold it, whatever the
    # punctuation, unless the model is near-certain it's somehow done
    if strict and (prob is None or prob < 0.9):
        return "incomplete"
    if stripped[-1] in (",", "-"):
        return "incomplete"

    # everything else defaults to RESPONDING (bias against leaving you hanging).
    # only a soft trailing word ("...and", "...well") consults the model, and
    # even then only holds when it's fairly sure you're mid-thought.
    if soft and prob is not None and prob < 0.35:
        return "incomplete"
    return "complete"

