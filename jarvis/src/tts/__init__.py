from .base import TTSEngine


def make_tts_engine(cfg: dict) -> TTSEngine:
    """Build the configured TTS engine; fall back to macOS `say` on failure."""
    from .say import SayTTS

    engine = cfg["tts"].get("engine", "kokoro")
    if engine == "kokoro":
        try:
            from .kokoro_tts import KokoroTTS

            return KokoroTTS(cfg)
        except Exception as e:
            print(f"[tts] kokoro unavailable ({e!r}); falling back to `say`")
    return SayTTS(cfg)
