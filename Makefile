# 3.12: newest Python with full wheel coverage for the voice stack (torch/spacy)
PYTHON ?= python3.12
VENV := .venv
PIP := $(VENV)/bin/pip
PY := $(VENV)/bin/python

.PHONY: setup setup-voice run chat voice memory ingest research serve install-agent uninstall-agent install-web uninstall-web push test-once clean

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
	ollama list | grep -q "^qwen3:8b" || ollama pull qwen3:8b
	ollama list | grep -q "^nomic-embed-text" || ollama pull nomic-embed-text
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

# deep research: make research ARGS="best tide prediction APIs"
research:
	$(PY) -m src.research.cli $(ARGS)

# diagnose listening problems (ARGS="--devices" lists microphones)
mic-test:
	$(PY) -m src.stt.mictest $(ARGS)

# run the brain as a server so the web app can reach it (needs JARVIS_TOKEN in .env)
serve:
	$(PY) -m src.server

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

WEB_AGENT_ID := com.francescotomatis.jarvis-web
WEB_AGENT_PLIST := $(HOME)/Library/LaunchAgents/$(WEB_AGENT_ID).plist

# run the brain server on login & keep it alive, so the website is always
# reachable (pair with `tailscale funnel --bg 8765`). Needs JARVIS_TOKEN in .env.
install-web:
	@grep -q '^JARVIS_TOKEN=.\+' .env || (echo "Set JARVIS_TOKEN in .env first (run: openssl rand -hex 24)"; exit 1)
	mkdir -p $(HOME)/Library/LaunchAgents
	sed "s|__ROOT__|$(CURDIR)|g" scripts/$(WEB_AGENT_ID).plist > $(WEB_AGENT_PLIST)
	launchctl unload $(WEB_AGENT_PLIST) 2>/dev/null || true
	launchctl load $(WEB_AGENT_PLIST)
	@echo "Brain server runs on login now. Logs: tail -f data/jarvis-web.log"

uninstall-web:
	launchctl unload $(WEB_AGENT_PLIST) 2>/dev/null || true
	rm -f $(WEB_AGENT_PLIST)
	@echo "Removed."

# publish committed Jarvis work into the shared jonny repo (jarvis/ subtree)
JONNY_REPO := $(HOME)/jonny
push:
	cd $(JONNY_REPO) && git pull -q && \
	git subtree pull --prefix=jarvis $(CURDIR) main -m "Update jarvis/ from local work" && \
	git push

test-once:
	$(PY) -m src.main --once "Say hello in one short sentence."

clean:
	rm -rf $(VENV) *.egg-info
