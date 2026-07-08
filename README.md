# Jonny

A hovering ball of light you talk to — a **remote face onto Jarvis running on
your Mac**. Same brain everywhere: the free local model for everyday turns,
Claude for hard ones, web research, your memory + knowledge, the editable
About Me profile, and the same Kokoro British voice (streamed from the Mac).

The website itself holds no brain — it forwards everything to the Mac over a
secure tunnel. When the Mac is off, it says so.

## How it fits together

```
browser (mic/speaker)  ->  Vercel (this app, password-gated proxy)
                             |  Authorization: Bearer JARVIS_TOKEN
                             v
                        Cloudflare tunnel  ->  Mac: `make serve` (the Jarvis brain)
```

## Deploy

1. On the **Mac** (see the Jarvis repo): set `JARVIS_TOKEN` in its `.env`,
   run `make serve`, then `./scripts/tunnel.sh` to get a public URL.
2. Import `singafranci02/jonny` at vercel.com/new (framework: Next.js).
3. Add env vars, then deploy:
   - `DASHBOARD_PASSWORD` — password to open the dashboard
   - `MAC_BRAIN_URL` — the Cloudflare tunnel URL from step 1
   - `JARVIS_TOKEN` — the **same** string as in the Mac's `.env`
4. Open the URL, enter the password, click the light, talk.

Speech recognition needs Chrome, Edge, or Safari.

## Pages

- `/` — the orb. Click to wake; it listens, sends your words to the Mac,
  and plays back the Mac's voice. "stop" (browser) or clicking the orb halts it.
- `/about` — the About Me profile, saved on the Mac and used on every turn
  (here and on the Mac).

## Local dev

```sh
npm install
# .env.local: DASHBOARD_PASSWORD, MAC_BRAIN_URL=http://localhost:8765, JARVIS_TOKEN
npm run dev
```

## Orb states

| color  | meaning |
|--------|---------|
| blue   | asleep — click to wake |
| green  | listening |
| purple | thinking (Mac is working) |
| amber  | speaking |
