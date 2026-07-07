"""Sentence-streaming speech: speak the reply while it's still generating.

LLM text deltas are fed in; complete sentences are queued and a worker
thread synthesizes + plays them one by one (Pipecat pattern, minus the
framework). Playback starts after the first sentence instead of the
full response.
"""

from __future__ import annotations

import queue
import re
import threading
from collections import deque

_SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
_MIN_CHUNK = 40  # don't split "Dr." / "3.5" fragments into tiny utterances


def split_complete_sentences(buffer: str) -> tuple[list[str], str]:
    """(complete sentences ready to speak, remainder to keep buffering)."""
    parts = _SENTENCE_END.split(buffer)
    if len(parts) <= 1:
        return [], buffer
    complete, rest = parts[:-1], parts[-1]
    # merge fragments that are too short to speak naturally
    merged: list[str] = []
    for part in complete:
        if merged and len(merged[-1]) < _MIN_CHUNK:
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    return merged, rest


class SentenceSpeaker:
    """Feed text deltas; sentences are spoken in order on a worker thread."""

    def __init__(self, tts):
        self.tts = tts
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._buffer = ""
        self._speaking = threading.Event()
        self._stopped = threading.Event()
        self.recent_sentences: deque[str] = deque(maxlen=12)  # echo-filter memory
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()

    def _run(self) -> None:
        while True:
            sentence = self._queue.get()
            if sentence is None:  # drain marker
                self._speaking.clear()
                continue
            if self._stopped.is_set():
                continue
            self._speaking.set()
            self.recent_sentences.append(sentence)
            try:
                self.tts.speak(sentence)
            except Exception:
                pass
            if self._queue.empty():
                self._speaking.clear()

    @property
    def speaking(self) -> bool:
        return self._speaking.is_set()

    def feed(self, delta: str) -> None:
        if self._stopped.is_set():
            return
        self._buffer += delta
        sentences, self._buffer = split_complete_sentences(self._buffer)
        for s in sentences:
            if s.strip():
                self._queue.put(s.strip())

    def finish(self) -> None:
        """Flush the remainder and block until playback is done."""
        if self._buffer.strip() and not self._stopped.is_set():
            self._queue.put(self._buffer.strip())
        self._buffer = ""
        # wait for queue drain + current utterance
        while not self._queue.empty() or self.speaking:
            if self._stopped.is_set():
                break
            threading.Event().wait(0.1)

    def speak_now(self, text: str) -> None:
        """Queue a full text (used for announcements) and wait for it."""
        self._stopped.clear()
        self.feed(text + " ")
        self.finish()

    def stop(self) -> None:
        """Barge-in: kill current playback and drop everything queued."""
        self._stopped.set()
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._buffer = ""
        self.tts.stop()
        self._speaking.clear()

    def reset(self) -> None:
        """Arm for a new turn after a stop()."""
        self._stopped.clear()
        self._buffer = ""
