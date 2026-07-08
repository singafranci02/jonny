"""Model-based turn completion, ported from KoljaB/RealtimeVoiceChat.

A small DistilBERT classifier (`KoljaB/SentenceFinishedClassification`) reads
the words and judges whether the sentence sounds *finished* — so "call my…"
scores ~0 (wait) while "what time should we leave" scores ~0.97 (answer now).
~10ms on CPU. Loads in the background at startup; until it's ready (or if it
fails / transformers isn't installed) callers fall back to the heuristic.
"""

from __future__ import annotations

import threading
from functools import lru_cache

MODEL_NAME = "KoljaB/SentenceFinishedClassification"

_lock = threading.Lock()
_state = {"tok": None, "model": None, "loaded": False, "failed": False}


def load() -> bool:
    """Load the model (blocking). Safe to call repeatedly. Returns success."""
    with _lock:
        if _state["loaded"]:
            return True
        if _state["failed"]:
            return False
        try:
            import torch
            from transformers import (
                DistilBertForSequenceClassification,
                DistilBertTokenizerFast,
            )

            _state["tok"] = DistilBertTokenizerFast.from_pretrained(MODEL_NAME)
            model = DistilBertForSequenceClassification.from_pretrained(MODEL_NAME)
            model.eval()
            _state["model"] = model
            _state["torch"] = torch
            _state["loaded"] = True
            return True
        except Exception:
            _state["failed"] = True
            return False


@lru_cache(maxsize=256)
def completion_probability(text: str) -> float | None:
    """P(the sentence is finished) in [0,1], or None if the model isn't
    available (caller should fall back to the heuristic)."""
    if not _state["loaded"]:
        return None
    torch = _state["torch"]
    enc = _state["tok"](text, return_tensors="pt", truncation=True, max_length=128)
    with torch.no_grad():
        logits = _state["model"](**enc).logits
        probs = torch.softmax(logits, dim=-1)[0]
    return float(probs[1])
