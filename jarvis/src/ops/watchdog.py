"""Keeps the brain alive. Every interval it hits /health; if the brain is
down or wedged, it restarts the LaunchAgent. If it can't recover after a few
tries it backs off and records a notification (so `make doctor` shows it).

Stdlib only, everything wrapped — the watchdog must never be a crash source.
Runs as its own LaunchAgent (com.francescotomatis.jarvis-watchdog).
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
LOG = ROOT / "data" / "watchdog.log"
NOTES = ROOT / "data" / "notifications.log"
BRAIN_AGENT = "com.francescotomatis.jarvis-web"

INTERVAL = 60          # seconds between checks
STARTUP_GRACE = 180    # allow this long for a fresh start before judging it down
FAILS_BEFORE_RESTART = 2
MAX_RESTARTS = 4       # within a cooldown window before we give up + notify
COOLDOWN = 900         # 15 min: after MAX_RESTARTS, back off this long


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    try:
        LOG.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


def notify(msg: str) -> None:
    try:
        NOTES.parent.mkdir(parents=True, exist_ok=True)
        with open(NOTES, "a") as f:
            f.write(json.dumps({"at": time.time(), "msg": msg}) + "\n")
    except Exception:
        pass
    log(f"NOTIFY: {msg}")


def health(port: int) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://localhost:{port}/health", timeout=5) as r:
            return json.loads(r.read())
    except Exception:
        return None


def restart_brain() -> None:
    try:
        uid = os.getuid()
        subprocess.run(
            ["launchctl", "kickstart", "-k", f"gui/{uid}/{BRAIN_AGENT}"],
            timeout=30,
            capture_output=True,
        )
        log("restarted the brain agent")
    except Exception as e:
        log(f"restart failed: {e}")


def port_from_config() -> int:
    try:
        import yaml

        cfg = yaml.safe_load((ROOT / "config.yaml").read_text())
        return int(cfg.get("server", {}).get("port", 8765))
    except Exception:
        return 8765


def main() -> None:
    port = port_from_config()
    log("watchdog started")
    consecutive_fails = 0
    restarts = 0
    window_started = time.time()

    while True:
        try:
            h = health(port)
            healthy = bool(h and h.get("ready"))
            # a freshly-started brain reports status 'starting' — don't punish it
            if h and not healthy and (h.get("uptime_s", 0) < STARTUP_GRACE):
                healthy = True

            if healthy:
                if consecutive_fails:
                    log("brain healthy again")
                consecutive_fails = 0
                restarts = 0
                window_started = time.time()
            else:
                consecutive_fails += 1
                log(f"health check failed ({consecutive_fails})")
                if consecutive_fails >= FAILS_BEFORE_RESTART:
                    if time.time() - window_started > COOLDOWN:
                        restarts = 0  # new window
                        window_started = time.time()
                    if restarts < MAX_RESTARTS:
                        restarts += 1
                        restart_brain()
                        consecutive_fails = 0
                        time.sleep(STARTUP_GRACE)  # give it time to boot
                        continue
                    else:
                        notify(
                            "Jarvis brain keeps failing to start — check "
                            "data/jarvis-web.log. Ollama down? Out of memory?"
                        )
                        time.sleep(COOLDOWN)
                        restarts = 0
                        window_started = time.time()
                        continue
        except Exception as e:
            log(f"watchdog loop error (continuing): {e}")
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
