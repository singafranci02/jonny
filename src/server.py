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

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Request,
    Response,
    WebSocket,
)
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


def _valid_ticket(ticket: str) -> bool:
    """A ticket is 'base64url(payload).hex(hmac_sha256(payload, TOKEN))', with
    payload = {"exp": <unix seconds>}. The web app mints it (holding the
    token); the browser only ever carries this short-lived derived ticket."""
    import hashlib
    import hmac
    import time as _time

    secret = os.environ.get("JARVIS_TOKEN", "")
    if not secret or "." not in ticket:
        return False
    payload_b64, sig = ticket.rsplit(".", 1)
    expected = hmac.new(
        secret.encode(), payload_b64.encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(sig, expected):
        return False
    try:
        pad = "=" * (-len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + pad))
        return float(payload.get("exp", 0)) > _time.time()
    except Exception:
        return False


@app.on_event("startup")
async def _startup() -> None:
    import time as _time

    _state["started_at"] = _time.time()
    session = Session()
    await session.warmup()
    _state["session"] = session
    _state["tts"] = None  # lazily built on first /tts call
    asyncio.ensure_future(_prepare_acks())
    # load the turn-completion model in the background (heuristic until ready)
    asyncio.ensure_future(
        asyncio.get_event_loop().run_in_executor(None, _load_turn_model)
    )


def _load_turn_model() -> None:
    from .audio import turndetect

    turndetect.load()


async def _synth_clips(phrases: list[str]) -> list[dict]:
    from .tts.kokoro_tts import SAMPLE_RATE

    loop = asyncio.get_event_loop()
    engine = _state["tts"]
    clips = []
    for phrase in phrases:
        audio = await loop.run_in_executor(None, engine.synthesize, phrase)
        mp3 = await loop.run_in_executor(None, _encode_mp3, audio, SAMPLE_RATE)
        clips.append({"text": phrase, "mp3": base64.b64encode(mp3).decode()})
    return clips


async def _prepare_acks() -> None:
    """Pre-synthesize short clips (acknowledgments + 'didn't catch that'
    reprompts) so they can be spoken instantly."""
    from .tts import make_tts_engine
    from .tts.kokoro_tts import KokoroTTS

    try:
        session: Session = _state["session"]
        if _state.get("tts") is None:
            _state["tts"] = make_tts_engine(session.cfg)
        if not isinstance(_state["tts"], KokoroTTS):
            return
        tcfg = session.cfg.get("tts", {})
        _state["acks"] = await _synth_clips(tcfg.get("acks", ["One sec."]))
        _state["reprompts"] = await _synth_clips(
            tcfg.get(
                "reprompts",
                ["Sorry, I didn't catch that.", "Come again?", "Say that once more?"],
            )
        )
    except Exception:
        _state["acks"] = []
        _state["reprompts"] = []


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
    """Liveness + readiness detail for the watchdog and `make doctor`."""
    import time as _time

    import httpx

    ready = "session" in _state
    ollama_up = False
    if ready:
        url = _state["session"].cfg["llm"].get("ollama", {}).get(
            "url", "http://localhost:11434"
        )
        try:
            async with httpx.AsyncClient() as c:
                ollama_up = (await c.get(f"{url}/api/version", timeout=2)).status_code == 200
        except Exception:
            ollama_up = False
    started = _state.get("started_at")
    return {
        "status": "ok" if ready else "starting",
        "ready": ready,
        "ollama_up": ollama_up,
        "whisper_warm": _state.get("whisper") is not None,
        "voice_ready": bool(_state.get("acks")),
        "turn_model_loaded": _turn_model_loaded(),
        "uptime_s": round(_time.time() - started) if started else 0,
    }


def _turn_model_loaded() -> bool:
    try:
        from .audio import turndetect

        return bool(turndetect._state.get("loaded"))
    except Exception:
        return False


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

    def transcribe() -> str:
        scfg = _state["session"].cfg["stt"]
        segments, _info = _ensure_whisper().transcribe(
            io.BytesIO(audio_bytes),
            language=(scfg.get("language") or None),
            beam_size=scfg.get("beam_size", 5),
            initial_prompt=_stt_initial_prompt(),
            condition_on_previous_text=False,
            vad_filter=True,
        )
        return " ".join(s.text.strip() for s in segments).strip()

    loop = asyncio.get_event_loop()
    text = await loop.run_in_executor(None, transcribe)
    return {"text": text}


async def _stream_turn(message: str, events: asyncio.Queue) -> None:
    """Run one turn, putting events onto `events`: text deltas, per-sentence
    audio (base64 MP3) as the model generates, a slow-answer ack, and a final
    `done`. Shared by the SSE endpoint and the websocket. Ends with None."""
    import random
    import re
    import time as _time

    from .tts import make_tts_engine
    from .tts.kokoro_tts import SAMPLE_RATE, KokoroTTS

    session: Session = _state["session"]
    if _state.get("tts") is None:
        _state["tts"] = make_tts_engine(session.cfg)
    engine = _state["tts"]
    can_speak = isinstance(engine, KokoroTTS)
    loop = asyncio.get_event_loop()

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

    def on_text(delta: str) -> None:
        events.put_nowait({"type": "delta", "text": delta})
        sentence_buf[0] += delta
        # ship the FIRST chunk as early as possible so speech starts sooner:
        # on the first chunk, break at a clause (comma / "and" / "but") too,
        # not just sentence-enders (Ankur2606 TextChunker idea).
        first = seq[0] == 0 and not pending
        if first:
            m = re.search(r"^.{18,}?[.!?…]|^.{25,}?(?:,| and | but | so )", sentence_buf[0])
            if m:
                pending.append(m.group(0).rstrip(" ,"))
                sentence_buf[0] = sentence_buf[0][m.end():].lstrip()
                return
        parts = re.split(r"(?<=[.!?…])\s+", sentence_buf[0])
        while len(parts) > 1 and len(parts[0]) >= 60:
            pending.append(parts.pop(0))
        sentence_buf[0] = " ".join(parts)

    try:
        async with _lock:
            turn_task = asyncio.ensure_future(session.turn(message, on_text=on_text))
            ack_after = session.cfg.get("tts", {}).get("ack_after", 1.2)
            started = _time.monotonic()
            ack_sent = False
            while not turn_task.done() or pending:
                if (
                    not ack_sent
                    and seq[0] == 0
                    and not pending
                    and _state.get("acks")
                    and _time.monotonic() - started > ack_after
                ):
                    await events.put(
                        {"type": "audio", "seq": -1, **random.choice(_state["acks"])}
                    )
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
    await events.put(None)


@app.post("/chat_stream", dependencies=[Depends(require_token)])
async def chat_stream(body: ChatIn) -> StreamingResponse:
    """SSE turn (fallback path): text deltas + per-sentence MP3."""
    events: asyncio.Queue = asyncio.Queue()
    asyncio.ensure_future(_stream_turn(body.message, events))

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


def _stt_initial_prompt() -> str:
    """Bias whisper toward names it would otherwise mishear — the configured
    vocabulary plus proper nouns pulled from the About-Me profile, so it
    adapts as you edit your profile."""
    import re

    from .profile import load_profile

    scfg = _state["session"].cfg["stt"]
    terms = [t.strip() for t in scfg.get("vocabulary", "").split(",") if t.strip()]
    try:
        profile = load_profile(_state["session"].cfg)
        # capitalised words in the profile are likely names/projects
        found = re.findall(r"\b[A-Z][a-zA-Z][a-zA-Z']+\b", profile)
        terms += [w for w in found if w.lower() not in _COMMON_CAPS]
    except Exception:
        pass
    seen, uniq = set(), []
    for t in terms:
        if t.lower() not in seen:
            seen.add(t.lower())
            uniq.append(t)
    vocab = ". ".join(uniq[:24])
    return f"Hey Jarvis. {vocab}." if vocab else "Hey Jarvis."


_COMMON_CAPS = {
    "about", "current", "how", "preferences", "the", "i", "my", "me", "write",
    "projects", "favourites", "favorites", "he", "she", "they", "what", "when",
}


def _ensure_whisper():
    if _state.get("whisper") is None:
        from faster_whisper import WhisperModel

        scfg = _state["session"].cfg["stt"]
        _state["whisper"] = WhisperModel(
            scfg.get("web_model") or scfg.get("model", "large-v3-turbo"),
            device=scfg.get("device", "cpu"),
            compute_type=scfg.get("compute_type", "int8"),
        )
    return _state["whisper"]


def _debug_save(pcm: bytes, text: str, low: bool) -> None:
    """Black-box recorder: keep the last utterances + what Whisper heard, so
    'it doesn't understand me' is diagnosable from real audio, not guesses.
    Private — stays on the Mac in data/debug_utterances/. stt.debug_save."""
    try:
        import time as _time
        import wave
        from pathlib import Path

        from .config import ROOT

        d = ROOT / "data" / "debug_utterances"
        d.mkdir(parents=True, exist_ok=True)
        stamp = _time.strftime("%H%M%S")
        path = d / f"utt-{stamp}.wav"
        with wave.open(str(path), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(16000)
            w.writeframes(pcm)
        with open(ROOT / "data" / "stt.log", "a") as f:
            dur = len(pcm) / 2 / 16000
            f.write(
                f"{_time.strftime('%Y-%m-%d %H:%M:%S')}  {dur:4.1f}s  "
                f"{'LOW ' if low else 'ok  '}  {text!r}  ({path.name})\n"
            )
        wavs = sorted(d.glob("utt-*.wav"))
        for old in wavs[:-20]:  # keep the last 20
            old.unlink(missing_ok=True)
    except Exception:
        pass


# phrases whisper famously invents from pure noise/silence — if a short
# utterance is exactly one of these, it's almost certainly not you talking
_NOISE_HALLUCINATIONS = {
    "thank you", "thanks", "thank you very much", "thanks for watching",
    "thank you for watching", "bye", "you", ".", "uh", "um", "oh", "so",
    "okay", "yeah", "hmm", "huh", "the",
}


def _looks_like_noise(text: str, low_conf: bool) -> bool:
    import re

    norm = re.sub(r"[^a-z ]", "", text.lower()).strip()
    return norm in _NOISE_HALLUCINATIONS and (low_conf or len(norm.split()) <= 2)


def _transcribe_pcm(pcm: bytes) -> tuple[str, bool]:
    """Returns (text, low_confidence). Low confidence = probably misheard →
    the caller asks you to repeat instead of answering garbage."""
    import numpy as np

    audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
    scfg = _state["session"].cfg["stt"]
    segments, _info = _ensure_whisper().transcribe(
        audio,
        language=(scfg.get("language") or None),
        beam_size=scfg.get("beam_size", 5),
        initial_prompt=_stt_initial_prompt(),
        # each utterance is independent — don't let the previous one bias
        # (leaves it prone to hallucinating on short real-time clips)
        condition_on_previous_text=False,
        vad_filter=False,  # our VAD already segmented the utterance
    )
    segs = list(segments)
    text = " ".join(s.text.strip() for s in segs).strip()
    if not segs or not text:
        if scfg.get("debug_save", True):
            _debug_save(pcm, "(empty)", True)
        return "", True
    avg_logprob = sum(s.avg_logprob for s in segs) / len(segs)
    no_speech = max(s.no_speech_prob for s in segs)
    lp_floor = scfg.get("min_avg_logprob", -1.0)
    ns_ceil = scfg.get("max_no_speech", 0.6)
    low = avg_logprob < lp_floor or no_speech > ns_ceil
    if scfg.get("debug_save", True):
        _debug_save(pcm, text, low)
    return text, low


@app.websocket("/ws")
async def ws(websocket: WebSocket) -> None:
    """Real-time voice: the browser streams 16kHz PCM16 as you talk; a VAD
    cuts utterances the instant you stop (no upload wait); we transcribe the
    already-present audio and stream the spoken reply back on the same socket.

    Smart turn-taking: if what you said looks unfinished (trailing "and…",
    "let me finish", a mid-thought pause), it stays quiet and waits for the
    rest instead of jumping in. Low-confidence audio → it asks you to repeat
    rather than guessing. Auth: first message is a short-lived HMAC ticket."""
    import random

    from .audio.turntaking import classify
    from .audio.vad import VadSegmenter

    await websocket.accept()
    try:
        ticket = await asyncio.wait_for(websocket.receive_text(), timeout=10)
    except Exception:
        await websocket.close(code=1008)
        return
    if not _valid_ticket(ticket):
        await websocket.send_json({"type": "error", "error": "unauthorized"})
        await websocket.close(code=1008)
        return

    await websocket.send_json({"type": "ready"})
    cfg = _state["session"].cfg.get("turn", {})
    seg = VadSegmenter(
        aggressiveness=cfg.get("vad_aggressiveness", 3),
        energy_floor=cfg.get("vad_energy_floor", 300),
    )
    loop = asyncio.get_event_loop()
    cont_timeout = cfg.get("continuation_timeout", 2.5)

    speaking = {"on": False}
    abort = {"on": False}
    pending = {"text": ""}
    flush = {"task": None}
    last_reprompt = {"at": 0.0}
    utter_q: asyncio.Queue = asyncio.Queue()

    async def respond(text: str) -> None:
        abort["on"] = False
        await websocket.send_json({"type": "transcript", "text": text})
        events: asyncio.Queue = asyncio.Queue()
        asyncio.ensure_future(_stream_turn(text, events))
        speaking["on"] = True
        while True:
            event = await events.get()
            if event is None:
                break
            if abort["on"]:  # you talked over it — stop forwarding the reply
                continue
            await websocket.send_json(event)
        speaking["on"] = False

    def cancel_flush() -> None:
        if flush["task"]:
            flush["task"].cancel()
            flush["task"] = None

    def schedule_flush() -> None:
        cancel_flush()

        async def _later() -> None:
            try:
                await asyncio.sleep(cont_timeout)
                if pending["text"]:
                    txt, pending["text"] = pending["text"], ""
                    await respond(txt)
            except asyncio.CancelledError:
                pass

        flush["task"] = asyncio.ensure_future(_later())

    async def handle_utterance(pcm: bytes) -> None:
        import time as _time

        cancel_flush()
        text, low_conf = await loop.run_in_executor(None, _transcribe_pcm, pcm)
        # nothing intelligible, or one of whisper's classic noise inventions
        # ("Thank you." from silence) → silently ignore, never nag
        if not text.strip() or _looks_like_noise(text, low_conf):
            if pending["text"]:
                schedule_flush()
            return
        # clearly misheard, with nothing buffered → ask, don't guess —
        # but at most once every 8s (false triggers must not become nagging)
        if low_conf and not pending["text"]:
            if (
                _state.get("reprompts")
                and _time.monotonic() - last_reprompt["at"] > 8
            ):
                last_reprompt["at"] = _time.monotonic()
                clip = random.choice(_state["reprompts"])
                await websocket.send_json({"type": "audio", "seq": 0, **clip})
                await websocket.send_json({"type": "done", "text": clip["text"]})
            return

        merged = f"{pending['text']} {text}".strip()
        kind = classify(merged if pending["text"] else text)
        if kind == "hold":
            # "let me finish" — drop it, keep listening
            await websocket.send_json({"type": "partial", "text": pending["text"]})
            if pending["text"]:
                schedule_flush()
            return
        if kind == "incomplete":
            pending["text"] = merged
            await websocket.send_json({"type": "partial", "text": merged})
            schedule_flush()
            return
        pending["text"] = ""
        await respond(merged)

    async def consumer() -> None:
        while True:
            pcm = await utter_q.get()
            await handle_utterance(pcm)

    consumer_task = asyncio.ensure_future(consumer())
    try:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break
            if (data := msg.get("bytes")) is not None:
                was_speaking = seg.speaking
                for utter in seg.add(data):
                    utter_q.put_nowait(utter)
                # barge-in: you started talking while it's speaking → cut it off
                if seg.speaking and not was_speaking and speaking["on"]:
                    abort["on"] = True
                    await websocket.send_json({"type": "interrupt"})
            elif (txt := msg.get("text")) is not None:
                if txt == "flush" and (utter := seg.flush()) is not None:
                    utter_q.put_nowait(utter)
    except Exception:
        pass
    finally:
        cancel_flush()
        consumer_task.cancel()


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
