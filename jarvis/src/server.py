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

from fastapi import Depends, FastAPI, Header, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
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


@app.post("/tts", dependencies=[Depends(require_token)])
async def tts(body: TTSIn) -> Response:
    import soundfile as sf

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
    buf = io.BytesIO()
    sf.write(buf, audio, SAMPLE_RATE, format="WAV")
    return Response(content=buf.getvalue(), media_type="audio/wav")


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
