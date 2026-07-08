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
            beam_size=scfg.get("beam_size", 2),
            # biases Whisper toward the wake phrase / domain words
            initial_prompt=scfg.get("initial_prompt") or None,
            input_device_index=scfg.get("input_device_index"),
            # end-of-utterance detection
            post_speech_silence_duration=scfg.get("silence_duration", 1.0),
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
