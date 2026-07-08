"""The Mac brain, exposed over HTTP so the web app is a remote face onto it.

Same Session the CLI/voice use — local model, tools, research, memory,
knowledge, About Me profile, and Kokoro voice. One shared brain, guarded
by a lock (single user). Auth: every request must carry
`Authorization: Bearer <JARVIS_TOKEN>`; the web app's serverless function
holds that token, so the browser never sees it.

    make serve      # then expose it with a Cloudflare tunnel
"""

from __future__ import annotations

import asyncio
import io
import os
import uuid

import base64
import json

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from .config import load_config
from .main import Session
from .profile import load_profile, save_profile

app = FastAPI(title="Jarvis brain")

# the web app calls these from its own server, but allow browsers too
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

_state: dict = {}
_lock = asyncio.Lock()
_research_jobs: dict[str, dict] = {}


def require_token(authorization: str = Header(default="")) -> None:
    expected = os.environ.get("JARVIS_TOKEN", "")
    if not expected:
        raise HTTPException(500, "server has no JARVIS_TOKEN set")
    token = authorization.removeprefix("Bearer ").strip()
    if token != expected:
        raise HTTPException(401, "unauthorized")


@app.on_event("startup")
async def _startup() -> None:
    session = Session()
    await session.warmup()
    _state["session"] = session
    _state["tts"] = None  # lazily built on first /tts call
    asyncio.ensure_future(_prepare_acks())


async def _prepare_acks() -> None:
    """Pre-synthesize short acknowledgment clips so they can be spoken with
    zero latency while a slow answer is still being generated."""
    from .tts import make_tts_engine
    from .tts.kokoro_tts import SAMPLE_RATE, KokoroTTS

    try:
        session: Session = _state["session"]
        if _state.get("tts") is None:
            _state["tts"] = make_tts_engine(session.cfg)
        engine = _state["tts"]
        if not isinstance(engine, KokoroTTS):
            return
        loop = asyncio.get_event_loop()
        clips = []
        for phrase in session.cfg.get("tts", {}).get("acks", ["One sec."]):
            audio = await loop.run_in_executor(None, engine.synthesize, phrase)
            mp3 = await loop.run_in_executor(None, _encode_mp3, audio, SAMPLE_RATE)
            clips.append({"text": phrase, "mp3": base64.b64encode(mp3).decode()})
        _state["acks"] = clips
    except Exception:
        _state["acks"] = []


class ChatIn(BaseModel):
    message: str


class ProfileIn(BaseModel):
    content: str


class TTSIn(BaseModel):
    text: str


class ResearchIn(BaseModel):
    topic: str


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "ready": "session" in _state}


@app.post("/chat", dependencies=[Depends(require_token)])
async def chat(body: ChatIn) -> dict:
    session: Session = _state["session"]
    async with _lock:
        resp = await session.turn(body.message)
        research_job = None
        if session.pending_research:
            topic, session.pending_research = session.pending_research, None
            research_job = _start_research(topic)
    if resp.cost_usd:
        session.total_cost += resp.cost_usd
    return {
        "text": resp.text,
        "model": resp.model,
        "tier": resp.tier,
        "cost_usd": resp.cost_usd,
        "timings": resp.extra.get("timings"),
        "research_job_id": research_job,
    }


def _start_research(topic: str) -> str:
    job_id = uuid.uuid4().hex[:12]
    _research_jobs[job_id] = {"status": "running", "topic": topic}

    async def _run():
        session: Session = _state["session"]
        try:
            async with _lock:
                path, summary = await session.research(topic)
            _research_jobs[job_id] = {
                "status": "done",
                "topic": topic,
                "summary": summary,
                "report_file": path.name,
            }
        except Exception as e:
            _research_jobs[job_id] = {"status": "error", "topic": topic, "error": str(e)}

    asyncio.ensure_future(_run())
    return job_id


@app.post("/research", dependencies=[Depends(require_token)])
async def research(body: ResearchIn) -> dict:
    return {"job_id": _start_research(body.topic)}


@app.get("/research/{job_id}", dependencies=[Depends(require_token)])
async def research_status(job_id: str) -> dict:
    job = _research_jobs.get(job_id)
    if not job:
        raise HTTPException(404, "unknown job")
    return job


@app.get("/profile", dependencies=[Depends(require_token)])
async def get_profile() -> dict:
    return {"content": load_profile(_state["session"].cfg)}


@app.put("/profile", dependencies=[Depends(require_token)])
async def put_profile(body: ProfileIn) -> dict:
    save_profile(_state["session"].cfg, body.content)
    return {"ok": True}


def _encode_mp3(audio, sample_rate: int) -> bytes:
    # LAME (standard MP3 encoder) — ~8x smaller than WAV and universally
    # playable. Keeps tunnel bandwidth tiny (ngrok's 1GB/mo then covers
    # tens of thousands of replies).
    import lameenc
    import numpy as np

    pcm16 = (np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes()
    encoder = lameenc.Encoder()
    encoder.set_bit_rate(64)
    encoder.set_in_sample_rate(sample_rate)
    encoder.set_channels(1)
    encoder.set_quality(2)
    return bytes(encoder.encode(pcm16) + encoder.flush())


@app.post("/stt", dependencies=[Depends(require_token)])
async def stt(request: Request) -> dict:
    """Transcribe browser audio (webm/opus, wav...) with faster-whisper —
    the same accurate model the Mac voice mode uses."""
    audio_bytes = await request.body()
    if not audio_bytes or len(audio_bytes) > 25_000_000:
        raise HTTPException(400, "bad audio")

    if _state.get("whisper") is None:
        from faster_whisper import WhisperModel

        scfg = _state["session"].cfg["stt"]
        _state["whisper"] = WhisperModel(
            scfg.get("web_model") or scfg.get("model", "large-v3-turbo"),
            device=scfg.get("device", "cpu"),
            compute_type=scfg.get("compute_type", "int8"),
        )

    def transcribe() -> str:
        scfg = _state["session"].cfg["stt"]
        segments, _info = _state["whisper"].transcribe(
            io.BytesIO(audio_bytes),
            language=(scfg.get("language") or None),
            beam_size=scfg.get("beam_size", 2),
            vad_filter=True,
        )
        return " ".join(s.text.strip() for s in segments).strip()

    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, transcribe)
    return {"text": text}


@app.post("/chat_stream", dependencies=[Depends(require_token)])
async def chat_stream(body: ChatIn) -> StreamingResponse:
    """SSE turn: text deltas stream immediately; each completed sentence is
    synthesized (Kokoro) and pushed as base64 MP3 while the model is still
    generating — the browser starts speaking after sentence one."""
    from .tts import make_tts_engine
    from .tts.kokoro_tts import SAMPLE_RATE, KokoroTTS

    session: Session = _state["session"]
    if _state.get("tts") is None:
        _state["tts"] = make_tts_engine(session.cfg)
    engine = _state["tts"]
    can_speak = isinstance(engine, KokoroTTS)

    events: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_event_loop()

    async def produce() -> None:
        sentence_buf = [""]
        pending: list[str] = []
        seq = [0]

        async def emit_sentence(sentence: str) -> None:
            if not can_speak or not sentence.strip():
                return
            audio = await loop.run_in_executor(None, engine.synthesize, sentence)
            mp3 = await loop.run_in_executor(None, _encode_mp3, audio, SAMPLE_RATE)
            await events.put(
                {
                    "type": "audio",
                    "seq": seq[0],
                    "text": sentence,
                    "mp3": base64.b64encode(mp3).decode(),
                }
            )
            seq[0] += 1

        import re

        def on_text(delta: str) -> None:
            events.put_nowait({"type": "delta", "text": delta})
            sentence_buf[0] += delta
            # eager split: ship the FIRST sentence as soon as it exists
            # (>=12 chars), be less choppy afterwards (>=60)
            min_len = 12 if seq[0] == 0 and not pending else 60
            parts = re.split(r"(?<=[.!?…])\s+", sentence_buf[0])
            while len(parts) > 1 and len(parts[0]) >= min_len:
                pending.append(parts.pop(0))
            sentence_buf[0] = " ".join(parts)

        try:
            async with _lock:
                turn_task = asyncio.ensure_future(
                    session.turn(body.message, on_text=on_text)
                )
                # if the answer is slow, speak a pre-synthesized ack instantly
                import random
                import time as _time

                ack_after = session.cfg.get("tts", {}).get("ack_after", 1.6)
                started = _time.monotonic()
                ack_sent = False
                # synthesize sentences as they complete, while the LLM runs
                while not turn_task.done() or pending:
                    if (
                        not ack_sent
                        and seq[0] == 0
                        and not pending
                        and _state.get("acks")
                        and _time.monotonic() - started > ack_after
                    ):
                        ack = random.choice(_state["acks"])
                        await events.put({"type": "audio", "seq": -1, **ack})
                        ack_sent = True
                    if pending:
                        await emit_sentence(pending.pop(0))
                    else:
                        await asyncio.sleep(0.05)
                resp = await turn_task
                if sentence_buf[0].strip():
                    await emit_sentence(sentence_buf[0])
                research_job = None
                if session.pending_research:
                    topic, session.pending_research = session.pending_research, None
                    research_job = _start_research(topic)
            if resp.cost_usd:
                session.total_cost += resp.cost_usd
            await events.put(
                {
                    "type": "done",
                    "text": resp.text,
                    "model": resp.model,
                    "tier": resp.tier,
                    "research_job_id": research_job,
                }
            )
        except Exception as e:
            await events.put({"type": "error", "error": str(e)})
        await events.put(None)  # stream end

    asyncio.ensure_future(produce())

    async def sse():
        while True:
            event = await events.get()
            if event is None:
                break
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        sse(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/tts", dependencies=[Depends(require_token)])
async def tts(body: TTSIn) -> Response:
    from .tts import make_tts_engine
    from .tts.kokoro_tts import SAMPLE_RATE, KokoroTTS

    if _state.get("tts") is None:
        _state["tts"] = make_tts_engine(_state["session"].cfg)
    engine = _state["tts"]
    if not isinstance(engine, KokoroTTS):
        # only Kokoro can hand back audio bytes; browser uses its own voice
        raise HTTPException(503, "kokoro voice unavailable on this machine")

    loop = asyncio.get_event_loop()
    audio = await loop.run_in_executor(None, engine.synthesize, body.text)
    mp3 = await loop.run_in_executor(None, _encode_mp3, audio, SAMPLE_RATE)
    return Response(content=mp3, media_type="audio/mpeg")


def main() -> None:
    import uvicorn

    cfg = load_config()
    if not os.environ.get("JARVIS_TOKEN"):
        raise SystemExit(
            "Set JARVIS_TOKEN in .env to a long random string first "
            "(the web app must send the same value)."
        )
    uvicorn.run(app, host="0.0.0.0", port=cfg.get("server", {}).get("port", 8765))


if __name__ == "__main__":
    main()
