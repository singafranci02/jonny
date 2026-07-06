from .base import STTEngine


def make_stt_engine(cfg: dict) -> STTEngine:
    engine = cfg["stt"].get("engine", "realtime_stt")
    if engine == "realtime_stt":
        from .realtime import RealtimeSTTEngine

        return RealtimeSTTEngine(cfg)
    raise ValueError(f"Unknown stt.engine: {engine!r}")
