"""macOS built-in `say` — the always-works fallback."""

from __future__ import annotations

import subprocess

from .base import TTSEngine


class SayTTS(TTSEngine):
    def __init__(self, cfg: dict):
        tcfg = cfg.get("tts", {})
        self.voice = tcfg.get("say_voice")  # None = system default
        self.rate = tcfg.get("say_rate", 190)
        self._proc: subprocess.Popen | None = None

    def speak(self, text: str) -> None:
        cmd = ["say", "-r", str(self.rate)]
        if self.voice:
            cmd += ["-v", self.voice]
        self._proc = subprocess.Popen([*cmd, text])
        self._proc.wait()
        self._proc = None

    def stop(self) -> None:
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
