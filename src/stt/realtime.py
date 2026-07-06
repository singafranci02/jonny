"""RealtimeSTT wrapper: mic capture + VAD + faster-whisper transcription."""

from __future__ import annotations

from .base import STTEngine


class RealtimeSTTEngine(STTEngine):
    def __init__(self, cfg: dict):
        from RealtimeSTT import AudioToTextRecorder

        scfg = cfg["stt"]
        self.recorder = AudioToTextRecorder(
            model=scfg.get("model", "large-v3-turbo"),
            language=scfg.get("language", "") or "",
            compute_type=scfg.get("compute_type", "int8"),
            device=scfg.get("device", "cpu"),
            # end-of-utterance detection
            post_speech_silence_duration=scfg.get("silence_duration", 0.8),
            min_length_of_recording=0.3,
            # we transcribe whole utterances; no partial streaming needed
            enable_realtime_transcription=False,
            spinner=False,
            level=40,  # logging.ERROR — keep the console clean
        )

    def listen(self) -> str:
        return (self.recorder.text() or "").strip()

    def shutdown(self) -> None:
        self.recorder.shutdown()
