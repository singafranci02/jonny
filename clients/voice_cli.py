"""Native voice client — the fastest face onto the Mac brain.

No browser, no Vercel: it opens a WebSocket straight to the brain (over
Tailscale from another device, or localhost at the Mac), streams your mic up,
and plays the reply back. The Mac does all the VAD / turn-taking / thinking.

    make talk            # over Tailscale to the Mac
    make talk LOCAL=1    # localhost (at the Mac — lowest latency)

Auth: it mints the same short-lived HMAC ticket the website uses, from
JARVIS_TOKEN (read from the environment or the repo .env).
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import hmac
import io
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

import numpy as np
import sounddevice as sd
import soundfile as sf
import websockets

RATE = 16_000
FRAME = 480  # 30ms
# int16 RMS above this during playback = you're talking over it (barge-in).
# Higher = fewer false triggers from its own voice on speakers; lower = easier
# to interrupt (best with headphones). Tune via JARVIS_BARGE_RMS.
BARGE_RMS = int(os.environ.get("JARVIS_BARGE_RMS", "1600"))


def _token() -> str:
    tok = os.environ.get("JARVIS_TOKEN", "").strip()
    if tok:
        return tok
    env = Path(__file__).resolve().parent.parent / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("JARVIS_TOKEN="):
                return line.split("=", 1)[1].strip()
    sys.exit("No JARVIS_TOKEN — set it in the environment or the repo .env.")


def _ticket(secret: str) -> str:
    payload = base64.urlsafe_b64encode(
        json.dumps({"exp": time.time() + 120}).encode()
    ).decode().rstrip("=")
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}.{sig}"


def _default_url(local: bool) -> str:
    if local:
        return "ws://localhost:8765/ws"
    if os.environ.get("JARVIS_URL"):
        return os.environ["JARVIS_URL"]
    # try the Mac's Tailscale name
    ts = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    try:
        import subprocess

        out = subprocess.run([ts, "status", "--json"], capture_output=True, text=True, timeout=5)
        name = json.loads(out.stdout)["Self"]["DNSName"].rstrip(".")
        return f"ws://{name}:8765/ws"
    except Exception:
        return "ws://localhost:8765/ws"


class Player:
    """Sequential audio playback on a worker thread, interruptible for barge-in."""

    def __init__(self):
        self.q: queue.Queue = queue.Queue()
        self.playing = threading.Event()
        self._stop = threading.Event()
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            audio, sr = self.q.get()
            if self._stop.is_set():
                continue
            self.playing.set()
            try:
                sd.play(audio, sr)
                sd.wait()
            except Exception:
                pass
            if self.q.empty():
                self.playing.clear()

    def add_mp3(self, mp3: bytes):
        try:
            audio, sr = sf.read(io.BytesIO(mp3), dtype="float32")
            self.q.put((audio, sr))
        except Exception:
            pass

    def stop(self):  # barge-in
        self._stop.set()
        try:
            while not self.q.empty():
                self.q.get_nowait()
        except queue.Empty:
            pass
        sd.stop()
        self.playing.clear()
        self._stop.clear()


async def run(url: str, secret: str, file: str | None) -> None:
    print(f"connecting to {url} …")
    async with websockets.connect(url, max_size=None, open_timeout=15) as ws:
        await ws.send(_ticket(secret))
        first = json.loads(await ws.recv())
        if first.get("type") != "ready":
            sys.exit(f"handshake failed: {first}")
        print("connected. Just talk — Ctrl-C to quit.\n" if not file else "streaming test file…")

        player = Player()
        loop = asyncio.get_event_loop()
        timings: list[float] = []
        turn = {"asked_at": None}

        # ---- receiver ----
        async def receiver():
            async for raw in ws:
                e = json.loads(raw)
                t = e["type"]
                if t == "partial":
                    print(f"  … {e['text']}", end="\r")
                elif t == "transcript":
                    turn["asked_at"] = time.time()
                    print(f"\nyou:   {e['text']}")
                elif t == "audio":
                    if turn["asked_at"] and e.get("seq") not in (-1, -2):
                        dt = time.time() - turn["asked_at"]
                        if dt > 0.05:
                            timings.append(dt)
                            print(f"jarvis ({dt:.1f}s): {e.get('text','')}")
                            turn["asked_at"] = None
                    player.add_mp3(base64.b64decode(e["mp3"]))
                elif t == "done":
                    if e.get("text"):
                        print(f"jarvis: {e['text']}")
                elif t == "interrupt":
                    player.stop()
                elif t == "error":
                    print(f"[error] {e.get('error')}")

        recv_task = asyncio.ensure_future(receiver())

        # ---- microphone (or test file) ----
        if file:
            data, sr = sf.read(file, dtype="int16")
            if data.ndim > 1:
                data = data[:, 0]
            if sr != RATE:
                import scipy.signal as ss

                data = ss.resample_poly(data.astype(np.float32), RATE, sr).astype(np.int16)
            pcm = np.concatenate([data, np.zeros(RATE, dtype=np.int16)]).tobytes()
            for i in range(0, len(pcm), FRAME * 2):
                await ws.send(pcm[i : i + FRAME * 2])
                await asyncio.sleep(0.028)
            await asyncio.sleep(8)  # let the reply come back
        else:
            mic_q: asyncio.Queue = asyncio.Queue()

            def on_mic(indata, frames, time_info, status):
                loop.call_soon_threadsafe(mic_q.put_nowait, bytes(indata))

            stream = sd.InputStream(
                samplerate=RATE, channels=1, dtype="int16",
                blocksize=FRAME, callback=on_mic,
            )
            stream.start()
            try:
                while True:
                    chunk = await mic_q.get()
                    if player.playing.is_set():
                        # while it's speaking, don't feed the mic back (echo) —
                        # unless you're clearly talking over it (barge-in)
                        rms = float(np.sqrt(np.mean(
                            np.frombuffer(chunk, dtype=np.int16).astype(np.float32) ** 2
                        )))
                        if rms > BARGE_RMS:
                            player.stop()
                            await ws.send(chunk)
                    else:
                        await ws.send(chunk)
            finally:
                stream.stop()
                stream.close()

        recv_task.cancel()
        if timings:
            avg = sum(timings) / len(timings)
            print(f"\n— {len(timings)} turns, avg {avg:.1f}s to first word —")


def main() -> None:
    p = argparse.ArgumentParser(description="Jarvis native voice client")
    p.add_argument("--url", help="ws URL (default: Tailscale name, or localhost)")
    p.add_argument("--local", action="store_true", help="talk to localhost")
    p.add_argument("--file", help="stream a WAV file instead of the mic (testing)")
    args = p.parse_args()
    url = args.url or _default_url(args.local)
    try:
        asyncio.run(run(url, _token(), args.file))
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
