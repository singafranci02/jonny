# Jonny

The website face of **Jarvis**, the assistant running on your Mac mini. It
holds no brain: every request is forwarded to the Mac over a secure tunnel,
and when the Mac is off, it says so.

- **`/` — Life HQ dashboard.** Reminders, tasks and goals per life area, logs,
  the activity feed, and "Ask Jarvis". Jarvis is the main agent; behind him,
  one agent per area (Work, Projects, Fitness, Health, Life) with its own
  folder in iCloud Drive/Jarvis. See **[PLAN.md](PLAN.md)** for how it works.
- **`/talk` — the orb.** Click to wake; it listens, sends your words to the
  Mac, and plays back the Mac's voice.
- **`/about`** — the About Me profile Jarvis uses on every turn.
- **`/files`** — read-only view of the Jarvis folder.
- **`/setup`** — what's connected: the Mac, the iCloud folder, Telegram, the
  reminder sender, and the check-ins (with "Run now").

Telegram, reminders, check-ins and the area agents all run on the Mac
(`jarvis/src/life/`); the website just shows and edits that data.

## How it fits together

```
browser  ->  Vercel (this app, password-gated proxy)
               |  Authorization: Bearer JARVIS_TOKEN
               v
          tunnel  ->  Mac: `make serve` (the Jarvis brain, incl. Life HQ)
Telegram  <------------------>  Mac (polls the bot directly)
```

## Deploy

1. On the **Mac** (see `jarvis/`): set `JARVIS_TOKEN` in its `.env`, run
   `make install-web`, and start the tunnel. For Life HQ, also follow
   Phase 1 in [PLAN.md](PLAN.md#phase-1--go-live-on-the-mac-mini-about-30-minutes).
2. Import `singafranci02/jonny` at vercel.com/new (framework: Next.js).
3. Add three env vars, then deploy:
   - `DASHBOARD_PASSWORD`: the password the website asks for
   - `MAC_BRAIN_URL`: the tunnel URL from step 1
   - `JARVIS_TOKEN`: the **same** string as in the Mac's `.env`
4. Open the URL and enter the password. Speech recognition on `/talk` needs
   Chrome, Edge or Safari.

## Local dev

```sh
npm install
# .env.local: DASHBOARD_PASSWORD, MAC_BRAIN_URL=http://localhost:8765, JARVIS_TOKEN
npm run dev
```

## Orb states (`/talk`)

| Color  | Meaning |
|--------|---------|
| blue   | asleep — click to wake |
| green  | listening |
| purple | thinking (Mac is working) |
| amber  | speaking |
