"""Server-side voice-activity segmenter.

Audio streams in from the browser live (16kHz mono PCM16). This watches it
with WebRTC VAD and cuts out complete utterances the instant you stop —
because the audio is already here, there's no upload wait, and transcription
can start immediately. Also exposes `speaking` so the server can detect
barge-in (you talking over the reply).
"""

from __future__ import annotations

import collections

SAMPLE_RATE = 16_000
FRAME_MS = 30
FRAME_BYTES = SAMPLE_RATE // 1000 * FRAME_MS * 2  # 30ms of 16-bit mono = 960


class VadSegmenter:
    def __init__(
        self,
        aggressiveness: int = 2,      # 0-3, higher = more aggressive filtering
        start_frames: int = 3,        # ~90ms of speech to trigger
        end_ms: int = 600,            # silence that ends an utterance
        pad_ms: int = 240,            # keep this much lead-in before speech
        min_speech_ms: int = 250,     # ignore blips shorter than this
    ):
        import webrtcvad

        self.vad = webrtcvad.Vad(aggressiveness)
        self.start_frames = start_frames
        self.end_frames = end_ms // FRAME_MS
        self.min_speech_frames = min_speech_ms // FRAME_MS
        self._pad = collections.deque(maxlen=pad_ms // FRAME_MS)
        self._buf = bytearray()          # leftover < one frame
        self._utter = bytearray()        # audio of the current utterance
        self._voiced = 0                 # trailing consecutive frames
        self._silence = 0
        self._in_speech = False
        self._speech_frames = 0

    @property
    def speaking(self) -> bool:
        return self._in_speech

    def add(self, pcm: bytes) -> list[bytes]:
        """Feed raw PCM16; returns any completed utterances (as PCM bytes)."""
        self._buf.extend(pcm)
        done: list[bytes] = []
        while len(self._buf) >= FRAME_BYTES:
            frame = bytes(self._buf[:FRAME_BYTES])
            del self._buf[:FRAME_BYTES]
            is_speech = self.vad.is_speech(frame, SAMPLE_RATE)

            if not self._in_speech:
                self._pad.append(frame)
                self._voiced = self._voiced + 1 if is_speech else 0
                if self._voiced >= self.start_frames:
                    self._in_speech = True
                    self._utter = bytearray(b"".join(self._pad))
                    self._pad.clear()
                    self._silence = 0
                    self._speech_frames = self._voiced
            else:
                self._utter.extend(frame)
                if is_speech:
                    self._silence = 0
                    self._speech_frames += 1
                else:
                    self._silence += 1
                    if self._silence >= self.end_frames:
                        if self._speech_frames >= self.min_speech_frames:
                            done.append(bytes(self._utter))
                        self._reset_speech()
        return done

    def flush(self) -> bytes | None:
        """Return the in-progress utterance (e.g. on client-signalled stop)."""
        if self._in_speech and self._speech_frames >= self.min_speech_frames:
            utter = bytes(self._utter)
            self._reset_speech()
            return utter
        self._reset_speech()
        return None

    def _reset_speech(self) -> None:
        self._in_speech = False
        self._utter = bytearray()
        self._voiced = 0
        self._silence = 0
        self._speech_frames = 0
        self._pad.clear()
