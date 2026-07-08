#!/usr/bin/env bash
# Expose the Mac brain to the internet with a free Cloudflare quick tunnel.
#
#   1. brew install cloudflared   (one time)
#   2. make serve                 (in one terminal — the brain)
#   3. ./scripts/tunnel.sh        (in another — the tunnel)
#
# It prints a https://<random>.trycloudflare.com URL. Put that in the Vercel
# project as MAC_BRAIN_URL. The quick-tunnel URL changes each run; for a
# stable custom domain, set up a Named Tunnel (see README).
set -euo pipefail

PORT="${1:-8765}"

if ! command -v cloudflared >/dev/null 2>&1; then
  echo "cloudflared not found. Install it:  brew install cloudflared" >&2
  exit 1
fi

echo "Starting tunnel to http://localhost:${PORT} ..."
echo "Copy the https://<...>.trycloudflare.com URL below into Vercel as MAC_BRAIN_URL."
exec cloudflared tunnel --url "http://localhost:${PORT}"
