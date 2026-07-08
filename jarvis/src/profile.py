"""About Me: a hand-edited profile injected into every turn.

Distinct from mem0's auto-extracted memory — this is the deliberate,
user-curated steering (bio, projects, preferences). Lives on the Mac as
one source of truth; editable from the CLI or the web app, read by both.
"""

from __future__ import annotations

from .config import ROOT

TEMPLATE = """# About me

(Write a sentence or two about who you are.)

# Current projects

- (What are you working on right now?)

# Preferences & favourites

- (Foods, music, tools, how you like things done...)

# How Jarvis should talk to me

- (Tone, length, anything to always keep in mind.)
"""


def _path(cfg: dict):
    rel = cfg.get("profile", {}).get("file", "data/profile.md")
    return ROOT / rel


def load_profile(cfg: dict) -> str:
    """Raw profile markdown; creates the template on first run."""
    path = _path(cfg)
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(TEMPLATE)
        return TEMPLATE
    return path.read_text()


def save_profile(cfg: dict, content: str) -> None:
    path = _path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)


def profile_for_prompt(cfg: dict) -> str:
    """Profile text to inject, or '' if it's still the untouched template."""
    content = load_profile(cfg).strip()
    if not content or content.strip() == TEMPLATE.strip():
        return ""
    # drop the parenthetical placeholder lines the user hasn't filled in
    kept = [
        line
        for line in content.splitlines()
        if not line.strip().startswith("(") and not line.strip() in ("-", "- ")
    ]
    cleaned = "\n".join(kept).strip()
    return cleaned
