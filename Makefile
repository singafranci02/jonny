# 3.12: newest Python with full wheel coverage for the voice stack (torch/spacy)
PYTHON ?= python3.12
VENV := .venv
PIP := $(VENV)/bin/pip
PY := $(VENV)/bin/python

.PHONY: setup setup-voice run chat voice memory ingest test-once clean

setup:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip -q
	$(PIP) install -e . -q
	@test -f .env || cp .env.example .env
	@echo "Done. Put your ANTHROPIC_API_KEY in .env, then: make run"

setup-voice: setup
	brew list portaudio >/dev/null 2>&1 || brew install portaudio
	brew list espeak-ng >/dev/null 2>&1 || brew install espeak-ng
	$(PIP) install -e ".[voice]" -q
	@echo "Voice stack installed. Run: make voice"

run: voice

chat:
	$(PY) -m src.main

voice:
	$(PY) -m src.main --voice

# usage: make memory ARGS="list" | ARGS='search "grant deadline"' | ARGS='forget --all'
memory:
	$(PY) -m src.memory.cli $(ARGS)

# index the knowledge/ folder (incremental; ARGS="--force" to re-embed all)
ingest:
	$(PY) -m src.knowledge.cli $(ARGS)

test-once:
	$(PY) -m src.main --once "Say hello in one short sentence."

clean:
	rm -rf $(VENV) *.egg-info
