"""Echo filter: drop transcripts that are Jarvis hearing its own voice.

isair/jarvis pattern, without an extra model: fuzzy-match the incoming
transcript against sentences spoken in the last few seconds.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def is_echo(transcript: str, recent_sentences, threshold: float = 0.75) -> bool:
    heard = _norm(transcript)
    if not heard:
        return True
    for spoken in recent_sentences:
        s = _norm(spoken)
        if not s:
            continue
        if heard in s or SequenceMatcher(None, heard, s).ratio() >= threshold:
            return True
    return False


STOP_WORDS = ("stop", "quiet", "shut up", "enough", "cancel")


def is_stop_command(transcript: str) -> bool:
    text = _norm(transcript)
    return len(text.split()) <= 4 and any(w in text for w in STOP_WORDS)
