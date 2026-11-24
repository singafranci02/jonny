# Jonny · Life HQ

A personal organiser that talks to you on Telegram. Jonny, your chief of
staff, sends reminders, a morning brief, an evening check-in and a Sunday
review. Behind him, one AI agent runs each life area: Work, Projects, Fitness,
Health and Life. A password-gated dashboard on Vercel shows everything at a glance.

**Read [PLAN.md](PLAN.md)** for how it works, the decisions behind it, costs,
and the step-by-step go-live checklist.

- **Telegram first:** text Jonny in plain language ("remind me to call mum
  Sunday 6pm", "log 45 min run", "plan my training week"). Reminders arrive
  with ✅ Done / ⏰ +1h / 🌙 Tomorrow buttons.
- **Agents:** Claude Managed Agents. Jonny runs on Opus 5.5 and the area agents
  on Sonnet 5.5. They read and write your data only through the MCP tools at
  `/api/mcp`, which Jarvis will reuse later.
- **Dashboard:** reminders, tasks and goals per area, logs, and the activity
  feed. `/setup` walks through connecting everything.
- **Voice face at `/talk`:** the orb that talks to Jarvis on your Mac (see
  below). It works as before, just moved off the home page.
- **Auth:** the dashboard uses the `DASHBOARD_PASSWORD` cookie. The Telegram,
  MCP and scheduler endpoints each check their own secret.

## Setup

Follow **Phase 1** in [PLAN.md](PLAN.md#phase-1--go-live-about-an-hour-together).
In short:

1. Create a Telegram bot and a Neon Postgres database.
2. Set the env vars listed in [.env.example](.env.example) in Vercel, then deploy.
3. On `/setup`, press the buttons in order:
   1. Create tables.
   2. Connect webhook.
   3. Send test message.
   4. Sync agents & schedules.
4. Add the `APP_URL` and `CRON_SECRET` repository secrets so the GitHub
   Actions reminder trigger runs.

## Customise

Edit [lib/areas.ts](lib/areas.ts) to change areas, agent focus, check-in
times, models and spend caps. Redeploy, then press **Sync agents & schedules**
on `/setup`.

## Voice face: Jarvis on your Mac (`/talk`)

The orb at `/talk` is a remote face onto Jarvis running on your Mac: the
same local model, tools, research, memory, About Me profile and Kokoro voice.
The website holds no brain for this part. It forwards everything to the Mac
over a tunnel, and says so when the Mac is off.

```
browser (mic/speaker)  ->  Vercel (this app, password-gated proxy)
                             |  Authorization: Bearer JARVIS_TOKEN
                             v
                        tunnel  ->  Mac: `make serve` (the Jarvis brain)
```

On the Mac (see `jarvis/`), set `JARVIS_TOKEN` in its `.env`, run
`make install-web` and start the tunnel. Then set `MAC_BRAIN_URL` and the
same `JARVIS_TOKEN` in Vercel. Speech recognition needs Chrome, Edge or Safari.

Related pages: `/about` (the About Me profile, saved on the Mac) and `/files`
(read-only view of Jarvis's workspace).

| Orb color | Meaning |
|--------|---------|
| blue   | asleep — click to wake |
| green  | listening |
| purple | thinking (Mac is working) |
| amber  | speaking |

## Local dev

```sh
npm install
cp .env.example .env.local   # fill in the values (MAC_BRAIN_URL=http://localhost:8765 for the voice face)
npm run dev                  # http://localhost:3000
```

Telegram and the hosted agents need a public HTTPS URL, so test those
against the Vercel deployment.
