# Jarvis — personal voice assistant (Mac mini)

A private, always-available voice assistant with a **hybrid brain**:
everyday turns run on a **local model** (qwen3:8b via Ollama — free,
private, offline), hard turns (code, analysis, research synthesis) go to
**Claude Sonnet 5**. Everything else — speech, memory, documents — runs
locally.

Jarvis has **tools** (agent loop): web search (DuckDuckGo → Wikipedia
fallback), page reading, date/time, exact arithmetic, and lookups into its
own memory and knowledge stores. It also has a **deep research** mode and
**streaming voice** with interrupts.

Every tier is one block in `config.yaml` — point any of them at anthropic,
ollama, or any OpenAI-compatible endpoint.

## Quick start

```sh
make setup-voice           # venv + deps + brew portaudio/espeak-ng
# put ANTHROPIC_API_KEY=sk-ant-... in .env
make voice                 # talk to it (mic + speaker); Ctrl-C to exit
make chat                  # text-only REPL
make test-once             # one scripted turn (smoke test)
```

First `make voice` run downloads the Whisper STT model and Kokoro TTS
weights, and macOS will ask for microphone permission for your terminal.
If Kokoro fails to load for any reason, Jarvis speaks through the built-in
macOS `say` voice instead.

Each reply prints the model used, token counts (incl. cache read/write), the
turn cost, and the running session total.

## Model routing (hybrid brain)

Four tiers in `config.yaml`, each with its own provider + model:

| tier | default | used for |
|---|---|---|
| `default` | ollama / qwen3:8b | everyday voice turns ($0) |
| `hard` | anthropic / claude-sonnet-5 | code, multi-step analysis (keyword/length router) |
| `research` | anthropic / claude-sonnet-5 | research planning + report synthesis |
| `summarize` | ollama / qwen3:8b | research page-notes ($0) |

If a cloud tier fails (API down), the turn degrades to the local default
automatically. Any tier can point at `openai_compatible` (DeepSeek, Qwen
cloud...) — `base_url` + key in `.env`, no code changes.

## Tools (agent loop)

The model decides when to call: `web_search`, `fetch_page`, `get_datetime`,
`calculate`, `search_knowledge`, `search_memory`, `remember`. Up to five
tool rounds per turn; each call is printed, and the usage line shows
`tools xN`.

## Deep research

"Hey Jarvis, research the best e-ink tablets" or:

```sh
make research ARGS='"best tide prediction APIs"'
```

Plans sub-queries (Claude) → searches and reads pages, distilling notes
with the free local model → synthesizes a cited markdown report (Claude) →
saves it to `knowledge/research/`, where it's auto-indexed so future
questions answer from it. In voice mode it runs in the background and
Jarvis announces the summary when done.

## Build phases

1. **Text loop** — CLI chat, routing, cost logging ✅
2. **Voice I/O** — faster-whisper/RealtimeSTT in, Kokoro (fallback `say`) out ✅
3. **Memory** — Mem0: retrieve before / extract-and-write after each turn ✅
4. **Knowledge/RAG** — `knowledge/` folder → Ollama embeddings → Chroma ✅
5. **Wake word + polish** — transcript gate, LaunchAgent, local fallback ✅

## Wake word ("hey jarvis")

In voice mode Jarvis transcribes everything locally but only responds when
the transcript contains the wake phrase — or within 60s of the last
exchange, so follow-up questions don't need it repeated. Tune under
`wakeword:` in `config.yaml`, or set `enabled: false` to respond to
everything. (openWakeWord was the original plan but is unmaintained and
silently broken on numpy 2.x; gating on the Whisper transcript is more
accurate anyway.)

## Streaming voice + interrupts

Replies are spoken sentence-by-sentence while the model is still
generating, so Jarvis starts talking after the first sentence. The mic
stays open during playback: say **"stop"** to cut it off, or barge in with
the wake phrase to ask something new. An echo filter (fuzzy match against
what Jarvis just said) keeps it from hearing itself.

## Always on

```sh
make install-agent     # launchd starts Jarvis on login and keeps it alive
tail -f data/jarvis.log
make uninstall-agent
```

macOS will ask for microphone permission for the python binary on first
launch. If the agent shows no mic prompt, run `make voice` once in a
terminal first and grant it there.

## Graceful degradation

- Cloud API down → the turn retries on a local Ollama model
  (`ollama pull llama3.2:3b` once to enable; `llm.local_fallback` in config).
- Kokoro fails to load → speech falls back to macOS `say`.
- Memory extraction failures log and never break a turn.

## Memory

Facts you mention get distilled by Claude Haiku after each turn (in the
background, no added latency) and stored locally: Ollama `nomic-embed-text`
embeddings in Chroma under `data/mem0/`. Mem0 decides ADD/UPDATE/DELETE per
fact, so corrections supersede stale facts. Before every turn the top
relevant memories are injected into the prompt.

```sh
make memory ARGS="list"
make memory ARGS='search "birthday"'
make memory ARGS='add "I prefer metric units"'
make memory ARGS="forget <id>"     # or ARGS="forget --all"
```

Requires Ollama running (`brew services start ollama`) with
`ollama pull nomic-embed-text` done once.

## Knowledge (RAG over your documents)

Drop `.md` / `.txt` / `.pdf` files into `knowledge/`. They get chunked,
embedded locally (Ollama), and stored in Chroma under `data/knowledge/`.
Relevant chunks are injected into every turn with their source file, and
Jarvis names the file when it uses one ("according to your
mareluna-project notes...").

```sh
make ingest                    # incremental index (only new/changed files)
make ingest ARGS="--force"     # re-embed everything
make ingest ARGS='--search "tide app"'   # test retrieval
```

While Jarvis runs, a file watcher re-indexes automatically ~2s after you
save a file into `knowledge/`; it also catches up at startup.

## Layout

```
config.yaml          models, routing, pricing — the one file you edit
prompts/system.md    persona (cached prompt prefix — keep byte-stable)
src/llm/             LLMClient interface + anthropic / openai_compatible backends
src/stt/             STTEngine interface + RealtimeSTT (faster-whisper) wrapper
src/tts/             TTSEngine interface + Kokoro and macOS `say` backends
src/memory/          MemoryStore interface + Mem0 backend + CLI
src/knowledge/       KnowledgeIndex: chunker, Ollama embeddings, Chroma, watcher
src/wakeword/        transcript gate (wake phrase + conversation window)
src/context.py       prompt assembly (persona + memories + knowledge + history)
scripts/             LaunchAgent plist template
src/main.py          async chat + voice loops
knowledge/           drop notes/PDFs here (Phase 4)
data/                vector DB + memory store (gitignored)
```
