# Jarvis — personal voice assistant (Mac mini)

A private, always-available voice assistant: local STT/TTS/memory/RAG, with only
the LLM calls going to a paid API.

**LLM:** Claude **Sonnet 5** (`claude-sonnet-5`) — near-Opus-4.8 quality at
$2/$10 per 1M tokens (intro pricing through 2026-08-31, then $3/$15), and the
cached system prompt bills at ~$0.20/1M on repeat turns. Swappable to any
OpenAI-compatible provider (DeepSeek, Qwen, local Ollama...) by editing
`config.yaml` only.

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

## Model routing

Every turn defaults to Sonnet 5 with thinking **off** (fast, cheap voice
replies). A keyword/length heuristic (`routing:` in `config.yaml`) promotes
hard turns (code, multi-step analysis) to Sonnet 5 with **adaptive thinking +
high effort**. To cut cost further, point the `default` tier at
`claude-haiku-4-5` in `config.yaml`.

## Swapping the LLM provider

Set `llm.provider: openai_compatible` in `config.yaml`, fill in `base_url` +
model ids, and put the key in `.env` (`LLM_API_KEY`). No code changes.

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
