#!/usr/bin/env bash
# Make the Mac mini a true always-on host for the brain. Idempotent — safe to
# re-run. Needs sudo (power settings are system-level).
#
#   sudo ./scripts/mac-liveness.sh
#
# What it does:
#   - never sleep while on power, and don't let the disk sleep
#   - power back on automatically after a power cut
# It CANNOT set "start up after power failure" reliably on Apple Silicon —
# do that once by hand (the script prints the exact clicks).
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run with sudo:  sudo ./scripts/mac-liveness.sh" >&2
  exit 1
fi

echo "Setting power management (on AC power)..."
pmset -c sleep 0            # system never sleeps on power
pmset -c disksleep 0        # disk never spins down
pmset -c displaysleep 10    # screen can still sleep — the brain doesn't care
pmset -c powernap 0         # no power-nap throttling
pmset -c autorestart 1      # power back on after a power failure (where supported)

echo
echo "Done. Current settings:"
pmset -c -g | grep -E "sleep|disksleep|autorestart|powernap" || true
echo
echo "ONE manual step (can't be scripted on Apple Silicon):"
echo "   System Settings → Energy  →  turn ON 'Start up automatically after a"
echo "   power failure'. That guarantees the Mac boots itself after an outage,"
echo "   and the brain's LaunchAgent brings Jarvis back with no action from you."
