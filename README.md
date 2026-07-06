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
4. **Knowledge/RAG** — `knowledge/` folder → Ollama embeddings → Chroma
5. **Wake word + polish** — openWakeWord, LaunchAgent, graceful fallbacks

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

## Layout

```
config.yaml          models, routing, pricing — the one file you edit
prompts/system.md    persona (cached prompt prefix — keep byte-stable)
src/llm/             LLMClient interface + anthropic / openai_compatible backends
src/stt/             STTEngine interface + RealtimeSTT (faster-whisper) wrapper
src/tts/             TTSEngine interface + Kokoro and macOS `say` backends
src/memory/          MemoryStore interface + Mem0 backend + CLI
src/context.py       prompt assembly (knowledge slot ready for Phase 4)
src/main.py          async chat + voice loops
knowledge/           drop notes/PDFs here (Phase 4)
data/                vector DB + memory store (gitignored)
```
