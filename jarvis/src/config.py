"""Load config.yaml + .env once, expose a plain dict."""

from __future__ import annotations

from pathlib import Path

import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent


def load_config() -> dict:
    load_dotenv(ROOT / ".env")
    with open(ROOT / "config.yaml") as f:
        return yaml.safe_load(f)


def load_system_prompt(cfg: dict) -> str:
    path = ROOT / cfg["persona"]["system_prompt_file"]
    return path.read_text()
