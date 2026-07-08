# 3.12: newest Python with full wheel coverage for the voice stack (torch/spacy)
PYTHON ?= python3.12
VENV := .venv
PIP := $(VENV)/bin/pip
PY := $(VENV)/bin/python

.PHONY: setup setup-voice run chat voice memory ingest research serve install-agent uninstall-agent install-web uninstall-web install-tunnel uninstall-tunnel install-watchdog uninstall-watchdog doctor status push test-once clean

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

TUNNEL_AGENT_ID := com.francescotomatis.jarvis-tunnel
TUNNEL_AGENT_PLIST := $(HOME)/Library/LaunchAgents/$(TUNNEL_AGENT_ID).plist

# keep the ngrok tunnel up on login so the permanent URL is always served.
# usage: make install-tunnel NGROK_URL=your-domain.ngrok-free.app
install-tunnel:
	@test -n "$(NGROK_URL)" || (echo 'Pass your reserved domain: make install-tunnel NGROK_URL=xxx.ngrok-free.app'; exit 1)
	mkdir -p $(HOME)/Library/LaunchAgents
	sed -e "s|__ROOT__|$(CURDIR)|g" -e "s|__NGROK_URL__|$(NGROK_URL)|g" \
		scripts/$(TUNNEL_AGENT_ID).plist > $(TUNNEL_AGENT_PLIST)
	launchctl unload $(TUNNEL_AGENT_PLIST) 2>/dev/null || true
	launchctl load $(TUNNEL_AGENT_PLIST)
	@echo "Tunnel runs on login now. Public URL: https://$(NGROK_URL)"

uninstall-tunnel:
	launchctl unload $(TUNNEL_AGENT_PLIST) 2>/dev/null || true
	rm -f $(TUNNEL_AGENT_PLIST)
	@echo "Removed."

WATCHDOG_ID := com.francescotomatis.jarvis-watchdog
WATCHDOG_PLIST := $(HOME)/Library/LaunchAgents/$(WATCHDOG_ID).plist

# watchdog: restarts the brain within ~60s if it ever dies
install-watchdog:
	mkdir -p $(HOME)/Library/LaunchAgents
	sed "s|__ROOT__|$(CURDIR)|g" scripts/$(WATCHDOG_ID).plist > $(WATCHDOG_PLIST)
	launchctl unload $(WATCHDOG_PLIST) 2>/dev/null || true
	launchctl load $(WATCHDOG_PLIST)
	@echo "Watchdog running. It keeps the brain alive."

uninstall-watchdog:
	launchctl unload $(WATCHDOG_PLIST) 2>/dev/null || true
	rm -f $(WATCHDOG_PLIST)
	@echo "Removed."

# is it live, and is everything healthy?
doctor:
	$(PY) -m src.ops.doctor

status:
	$(PY) -m src.ops.doctor status

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
