"""`make doctor` / `make status` — is Jarvis live, and where can I reach it?"""

from __future__ import annotations

import json
import subprocess
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
G, R, Y, D = "\033[32m", "\033[31m", "\033[33m", "\033[2m"
X = "\033[0m"


def _port() -> int:
    try:
        import yaml

        return int(
            yaml.safe_load((ROOT / "config.yaml").read_text())
            .get("server", {})
            .get("port", 8765)
        )
    except Exception:
        return 8765


def _health(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/health", timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return None


def _agent_running(label: str) -> bool:
    try:
        out = subprocess.run(
            ["launchctl", "list"], capture_output=True, text=True, timeout=10
        ).stdout
        for line in out.splitlines():
            if label in line:
                return not line.split("\t")[0].strip() == "-"
    except Exception:
        pass
    return False


def line(ok: bool, label: str, detail: str = "") -> None:
    mark = f"{G}●{X}" if ok else f"{R}●{X}"
    print(f"  {mark} {label:22} {D}{detail}{X}")


def status() -> None:
    port = _port()
    h = _health(port)
    print("\nJarvis — where to reach it:")
    line(bool(h and h.get("ready")), "brain (local)", f"http://localhost:{port}")
    url = None
    tp = ROOT / "data" / "jarvis-tunnel.log"
    if tp.exists():
        import re

        m = re.findall(r"url=(https://\S+)", tp.read_text())
        url = m[-1] if m else None
    line(bool(url), "public (website)", url or "tunnel not running")
    print()


def doctor() -> None:
    port = _port()
    h = _health(port)
    print("\nJarvis doctor:")
    line(bool(h), "brain reachable", f"localhost:{port}")
    if h:
        line(h.get("ready"), "brain ready", f"up {h.get('uptime_s', 0)}s")
        line(h.get("ollama_up"), "ollama (local model)")
        line(h.get("voice_ready"), "voice (Kokoro)")
        line(h.get("whisper_warm"), "speech-to-text warm")
        line(h.get("turn_model_loaded"), "turn-detection model")
    for label in (
        "com.francescotomatis.jarvis-web",
        "com.francescotomatis.jarvis-tunnel",
        "com.francescotomatis.jarvis-watchdog",
    ):
        line(_agent_running(label), label.split(".")[-1] + " agent")
    env = ROOT / ".env"
    has_token = env.exists() and "JARVIS_TOKEN=" in env.read_text() and any(
        l.startswith("JARVIS_TOKEN=") and len(l.strip()) > 14
        for l in env.read_text().splitlines()
    )
    line(has_token, "JARVIS_TOKEN set")
    notes = ROOT / "data" / "notifications.log"
    if notes.exists() and notes.read_text().strip():
        recent = notes.read_text().strip().splitlines()[-3:]
        print(f"\n  {Y}recent alerts:{X}")
        for r in recent:
            try:
                print(f"    - {json.loads(r)['msg']}")
            except Exception:
                pass
    print()


if __name__ == "__main__":
    (status if "status" in sys.argv else doctor)()
