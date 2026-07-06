"""Kokoro TTS (local, MIT) played through sounddevice.

Model weights (~330MB) download from Hugging Face on first use.
If anything here fails to import or load, the factory falls back to `say`.
"""

from __future__ import annotations

import numpy as np

from .base import TTSEngine

SAMPLE_RATE = 24_000


class KokoroTTS(TTSEngine):
    def __init__(self, cfg: dict):
        import sounddevice  # noqa: F401 — fail fast if audio output is broken
        from kokoro import KPipeline

        tcfg = cfg.get("tts", {})
        self.voice = tcfg.get("voice", "af_heart")
        self.speed = tcfg.get("speed", 1.0)
        # 'a' = American English G2P; see kokoro docs for other languages
        self.pipeline = KPipeline(lang_code=tcfg.get("lang_code", "a"))
        self._sd = sounddevice
        self._playing = False

    def synthesize(self, text: str) -> np.ndarray:
        chunks = [
            audio
            for _, _, audio in self.pipeline(text, voice=self.voice, speed=self.speed)
        ]
        if not chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate([np.asarray(c, dtype=np.float32) for c in chunks])

    def speak(self, text: str) -> None:
        audio = self.synthesize(text)
        if audio.size == 0:
            return
        self._playing = True
        try:
            self._sd.play(audio, SAMPLE_RATE)
            self._sd.wait()
        finally:
            self._playing = False

    def stop(self) -> None:
        if self._playing:
            self._sd.stop()
