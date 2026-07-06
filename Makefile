PYTHON ?= python3.14
VENV := .venv
PIP := $(VENV)/bin/pip
PY := $(VENV)/bin/python

.PHONY: setup run chat test-once clean

setup:
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --upgrade pip -q
	$(PIP) install -e . -q
	@test -f .env || cp .env.example .env
	@echo "Done. Put your ANTHROPIC_API_KEY in .env, then: make run"

run: chat

chat:
	$(PY) -m src.main

test-once:
	$(PY) -m src.main --once "Say hello in one short sentence."

clean:
	rm -rf $(VENV) *.egg-info
