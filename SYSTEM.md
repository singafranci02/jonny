# Jarvis / Jonny — the whole system, explained

This is the plain-English map of everything the assistant does today: what it
is, how the pieces fit, and where each capability lives in the code. For
step-by-step "how do I run it" instructions see [README.md](README.md); this
doc is the "how it all works" overview.

---

## 1. What it is, in one paragraph

A private voice assistant that **runs on your Mac mini** and answers you by
voice. Everyday questions run on a model in the cloud (Claude Haiku) for
snappy replies; hard questions and research use a stronger model (Claude
Sonnet 5); and if the internet ever drops, it falls back to a **free local
model** on the Mac (qwen3) automatically. It **remembers you** (an editable
profile plus facts it learns from conversations), **answers from your own
documents**, and can **research the web** into cited reports. You can talk to
it two ways — a terminal command on the Mac, or a **website (Jonny)** that is
just a remote face onto the same Mac brain.

**One brain, two faces.** There is only ever one "brain" — the code in this
repo running on your Mac. The website doesn't have its own intelligence; it
sends your voice to the Mac and plays back what the Mac produces.

```
   ┌─────────────────────────┐        ┌──────────────────────────────┐
   │  Mac terminal            │        │  Website (Jonny, on Vercel)  │
   │  `make voice`            │        │  jonnybot.xyz — orb you talk  │
   └───────────┬─────────────┘        └───────────────┬──────────────┘
               │                                       │ password-gated,
               │                                       │ over ngrok tunnel
               ▼                                       ▼
        ┌──────────────────────────────────────────────────────┐
        │           THE BRAIN  (this repo, on the Mac)          │
        │  speech-in → understand → think → tools → speak-out   │
        │  + memory + your documents + your profile + research  │
        └──────────────────────────────────────────────────────┘
```

---

## 2. The two faces

### Face A — the Mac (`make voice`)
Runs entirely on the Mac. Microphone → local Whisper → the brain → Kokoro
voice out. Has a wake word ("hey Jarvis") and interruption ("stop"). This is
`src/main.py`'s `voice_loop`.

### Face B — the website (Jonny)
A small Next.js app deployed on **Vercel** (repo: `github.com/singafranci02/jonny`,
the whole Jarvis brain lives inside it under `jarvis/`). It is a **thin
client**: every request is forwarded to the Mac. It holds no AI itself.

- You open the site, enter a **password** (`lib/auth.ts`, `middleware.ts`).
- You click the glowing orb and talk. Your voice is recorded in the browser,
  sent to the Mac's Whisper, and the Mac's reply is streamed back and spoken
  **in the Mac's own voice**.
- It reaches the Mac through a **tunnel** (a permanent public URL that points
  at your Mac — see §7). The browser never sees the secret token; the Vercel
  server holds it (`lib/brain.ts`).
- Voice-first: no transcript on screen unless you press "show text".

The website's API routes (`app/api/*`) are pure proxies to the brain:
`chat-stream`, `stt`, `tts`, `profile`, `research`, `chat`.

---

## 3. What happens in one voice turn (the pipeline)

This is the core loop, borrowing the streaming pattern from the fastest
open-source voice assistants (GLaDOS, RealtimeVoiceChat):

```
you speak
   │
   ▼
[1] STOP DETECTION   — it notices you went quiet (~0.6s of silence)
   │
   ▼
[2] TRANSCRIBE       — Whisper turns your audio into text  (~1s, on the Mac)
   │                    Mac face: large-v3-turbo | web face: distil-small.en
   ▼
[3] UNDERSTAND       — is this small talk? simple? hard? research?  (router)
   │                    → gathers profile + relevant memories + relevant notes
   ▼
[4] THINK            — the right model writes a reply, streaming word by word
   │                    simple → Haiku (cloud, fast) | hard → Sonnet 5
   │                    offline → qwen3 (local)   | + tools if needed
   ▼
[5] SPEAK            — each finished sentence is turned to speech (Kokoro)
                        and played WHILE the rest is still being written.
                        If nothing's ready within ~0.9s it says "one sec".
```

**Why it feels like a conversation:** steps 4 and 5 overlap. It starts
speaking the first sentence before the whole answer exists. Measured: a
greeting starts talking in **under 2 seconds**.

Code: the turn logic is `Session.turn` in `src/main.py`; the streaming
voice endpoint is `/chat_stream` in `src/server.py`.

---

## 4. The brain — how it decides & thinks

### The router (`src/llm/router.py`)
Every message is triaged cheaply, before any model runs:
- **Small talk** ("hey", "thanks") → answered directly, **no** notes or
  memories injected (this is what stopped it dragging your projects into
  every hello).
- **Simple** (short, no tool-words) → **fast path**: skips the tool
  machinery and trims history, so it answers quickest.
- **Hard** (code, analysis, long) → routed to Claude Sonnet 5.
- **Research** ("research X", "look into Y") → the research pipeline (§6).

### The model tiers (`config.yaml` → `llm.tiers`)
The assistant is **provider-agnostic** — each tier names a provider + model,
swappable by editing one file (`src/llm/__init__.py` dispatches):

| Tier | Runs on | Used for |
|---|---|---|
| `default` | Claude **Haiku 4.5** (cloud) | everyday turns — fast, cheap |
| `fallback_local` | **qwen3:8b** (local, free) | auto-used if the cloud fails / offline |
| `hard` | Claude **Sonnet 5** (cloud) | code, multi-step reasoning |
| `research` | Claude **Sonnet 5** (cloud) | writing cited reports |
| `summarize` | **qwen3:8b** (local, free) | distilling web pages during research |

Backends: `anthropic_client.py`, `ollama_client.py`, `openai_compat.py`
(escape hatch for DeepSeek/Qwen-cloud/etc.), all behind one interface in
`base.py`.

### Tools — the agent loop (`src/tools/`)
When a question needs live facts, the model calls tools, and the loop feeds
results back until it can answer (max 5 rounds):
- `web_search` — DuckDuckGo, and it **auto-reads the top 2 pages** so it
  answers from real content, not just snippets (`tools/web.py`).
- `fetch_page` — read a specific URL.
- `get_datetime`, `calculate` — time and exact maths (`tools/local.py`).
- `search_memory`, `remember`, `search_knowledge` — its own stores
  (`tools/stores.py`).
- `deep_research` — kick off a background research job.

---

## 5. How it knows *you* (three separate stores)

This is the part that makes it not-generic. Three distinct things:

1. **Profile — "About Me"** (`src/profile.py`, `data/profile.md`)
   A short text *you hand-write and edit* (from the website's About page or
   the file). Bio, current projects, preferences, how you like to be spoken
   to. Injected into (almost) every turn. This is the steering **you
   control**. *Filling this in is the single biggest quality upgrade you can
   make.*

2. **Memory — facts it learns** (`src/memory/`, powered by Mem0)
   After each conversation, a cheap model quietly extracts durable facts
   ("sister's birthday is Oct 12") and stores them locally. Before a turn, it
   pulls back *only strongly-relevant* ones (weak matches are filtered out —
   that was the "why is it always about Mareluna" fix). Manage with
   `make memory ARGS="list"`.

3. **Knowledge — your documents** (`src/knowledge/`)
   Drop `.md`/`.txt`/`.pdf` files in `knowledge/`; they're chunked, embedded
   locally, and stored in a vector database. Relevant chunks are pulled into
   answers with the **source file named**. A file-watcher re-indexes
   automatically. Manage with `make ingest`.

Memory and knowledge embeddings run on **Ollama locally** (`nomic-embed-text`)
— free and private. Only the main chat/research calls go to the cloud.

---

## 6. Deep research (`src/research/pipeline.py`)

"Research the best X" triggers a multi-step job, designed to be cheap:

```
PLAN      Claude breaks the topic into 3-5 search angles
          (aimed at YOU — your profile + memories shape the plan)
   ▼
GATHER    for each: web search → open pages → a LOCAL model distills
          each page to notes  (this step is free — no cloud tokens)
   ▼
WRITE     Claude writes a cited markdown report from the notes
   ▼
SAVE      lands in knowledge/research/ → auto-indexed, so future
          questions can answer from it. You get a 2-3 sentence spoken summary.
```

In voice mode it runs in the background: it says "on it", you keep talking,
and it announces the summary when done. CLI: `make research ARGS='"topic"'`.

---

## 7. Where it lives & how it stays on (hosting)

Everything runs on the **Mac mini**, which is always on. Three background
services start themselves at login (macOS LaunchAgents in `scripts/`):

| Service | What it is | Install |
|---|---|---|
| `jarvis-web` | the brain as an HTTP server (`src/server.py`, port 8765) | `make install-web` |
| `jarvis-tunnel` | ngrok — the permanent public URL to the Mac | `make install-tunnel NGROK_URL=…` |
| `jarvis` (optional) | always-on `make voice` mic loop | `make install-agent` |

**The tunnel:** Vercel (cloud) can't reach into your Mac directly, so ngrok
gives the Mac a fixed public address (`…ngrok-free.dev`). Vercel is
configured **once** with that URL + the shared token, then never again.
Audio is sent as compressed MP3 so bandwidth stays tiny (~30KB/reply).

> **Note:** these LaunchAgents run with `ProcessType=Interactive` — this
> matters. Without it macOS throttles the brain's CPU as a "background" job
> and turns take 5-10× longer. That single flag was the biggest speed fix.

**Security:** the website is password-gated; the Mac's HTTP server requires a
secret bearer token (`JARVIS_TOKEN`, in `.env`, matched on Vercel). The
browser never sees the token — only Vercel's server does.

---

## 8. Speed — where the time goes

Measured warm, on the 16GB Mac:

| Stage | Time |
|---|---|
| you stop → it notices | ~0.6s |
| transcribe your speech | ~1s |
| think → first spoken word (simple, Haiku) | ~1.5–3s |
| **total: you stop → it talks** | **~3–4.5s** (ack bridges at ~0.9s) |

Key tricks in place: model kept warm in memory (cold load is ~30s, avoided),
the CPU-throttle flag above, streaming sentence-by-sentence, a fast path that
skips tools, terse replies that finish sooner, and pre-made "one sec" clips.

**The next big speed step (researched, not yet built):** stream the
microphone over a websocket so transcription happens *while you talk* instead
of after you stop — the ~500ms approach from
[RealtimeVoiceChat](https://github.com/KoljaB/RealtimeVoiceChat). That would
remove most of the remaining ~1.5s.

---

## 9. What it costs

Everything is free except the cloud chat calls:
- Speech-in, speech-out, memory, knowledge, embeddings, research page-reading,
  the local fallback model — **all $0**, run on the Mac.
- Cloud model (Haiku for everyday, Sonnet 5 for hard/research) — roughly
  **$2–4/month** at heavy personal use.
- Tunnel + website hosting (ngrok free + Vercel free) — **$0**.

Want it fully free? Point the `default` tier back at `ollama / qwen3:8b` in
`config.yaml` — everything keeps working, replies are just a couple seconds
slower.

---

## 10. File map (quick reference)

```
Jarvis/  (the brain)
  config.yaml            EVERYTHING tunable — models, voice, timing, thresholds
  prompts/system.md      the persona (terse, British, straight to the point)
  src/
    main.py              the turn loop + Mac voice loop + warmup
    server.py            HTTP brain for the website (/chat_stream, /stt, /tts…)
    llm/                 tiered models (haiku/sonnet/qwen), router, tools loop
    tools/               web_search, fetch_page, datetime, calculate, memory…
    stt/                 speech-in (faster-whisper) + mic-test diagnostic
    tts/                 speech-out (Kokoro voice, macOS `say` fallback) + streamer
    memory/              Mem0 store + CLI (facts it learns)
    knowledge/           your documents → vector DB + file-watcher
    profile.py           the editable "About Me"
    research/            plan → gather → cite → save pipeline
    context.py           assembles profile + memories + knowledge into the prompt
    wakeword/            "hey jarvis" gate + conversation window
    audio/echo.py        stops it hearing its own voice
  scripts/               LaunchAgent service files + ngrok tunnel

jonny/  (the website — same repo, deployed on Vercel)
  app/page.tsx           the orb + voice-first UI
  app/about/page.tsx     edit your profile
  app/api/*              thin proxies to the Mac brain
  lib/brain.ts           holds the token, forwards to the Mac
  lib/auth.ts            password gate
```

---

## 11. Everyday commands

```sh
make voice        # talk to it on the Mac
make chat         # type to it
make research ARGS='"best e-ink tablets"'
make memory ARGS="list"          # see what it remembers
make ingest                      # re-index your documents
make mic-test                    # diagnose the microphone
make install-web                 # brain auto-starts on login (for the website)
```

That's the whole system as it stands.
