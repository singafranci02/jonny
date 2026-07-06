# 3.12: newest Python with full wheel coverage for the voice stack (torch/spacy)
PYTHON ?= python3.12
VENV := .venv
PIP := $(VENV)/bin/pip
PY := $(VENV)/bin/python

.PHONY: setup setup-voice run chat voice memory ingest install-agent uninstall-agent test-once clean

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

AGENT_ID := com.francescotomatis.jarvis
AGENT_PLIST := $(HOME)/Library/LaunchAgents/$(AGENT_ID).plist

# start Jarvis on login and keep it alive (logs -> data/jarvis.log)
install-agent:
	mkdir -p $(HOME)/Library/LaunchAgents
	sed "s|__ROOT__|$(CURDIR)|g" scripts/$(AGENT_ID).plist > $(AGENT_PLIST)
	launchctl unload $(AGENT_PLIST) 2>/dev/null || true
	launchctl load $(AGENT_PLIST)
	@echo "Installed. Watch it: tail -f data/jarvis.log"

uninstall-agent:
	launchctl unload $(AGENT_PLIST) 2>/dev/null || true
	rm -f $(AGENT_PLIST)
	@echo "Removed."

test-once:
	$(PY) -m src.main --once "Say hello in one short sentence."

clean:
	rm -rf $(VENV) *.egg-info
