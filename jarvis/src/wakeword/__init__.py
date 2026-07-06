"""Transcript-gated wake word.

Everything is transcribed locally anyway (free, private), so the wake
check runs on the transcript: respond only when the wake phrase appears,
or within `active_window` seconds of the last exchange so follow-up
questions don't need the phrase repeated.

(openWakeWord was the original plan but is unmaintained and silently
broken on numpy 2.x — Whisper-on-transcript is more reliable.)
"""

from __future__ import annotations

import re
import time


class TranscriptGate:
    def __init__(self, cfg: dict):
        wcfg = cfg.get("wakeword", {})
        self.enabled = wcfg.get("enabled", True)
        phrases = [wcfg.get("phrase", "hey jarvis"), *wcfg.get("aliases", [])]
        self.phrases = [self._norm(p) for p in phrases if p]
        self.active_window = wcfg.get("active_window", 60)
        self._last_exchange = 0.0

    @staticmethod
    def _norm(text: str) -> str:
        return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()

    def should_respond(self, transcript: str) -> tuple[bool, str]:
        """(respond?, transcript with a leading wake phrase stripped)."""
        if not self.enabled:
            return True, transcript
        norm = self._norm(transcript)
        for phrase in self.phrases:
            if phrase in norm:
                # strip the phrase when it leads the utterance
                stripped = re.sub(
                    rf"^\W*{re.escape(phrase)}\W*", "", transcript, flags=re.IGNORECASE
                ).strip(" ,.!?")
                return True, stripped or transcript
        if time.monotonic() - self._last_exchange < self.active_window:
            return True, transcript
        return False, transcript

    def mark_exchange(self) -> None:
        self._last_exchange = time.monotonic()
