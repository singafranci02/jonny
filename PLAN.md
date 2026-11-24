# Jonny · Life HQ: plan

_Last updated 2026-11-25. Status: the foundation is built on branch `life-hq`, on top of the current `main`, and tested locally. It is not deployed and has not yet run against real Telegram, database or Anthropic accounts._

## Goal

Keep work, projects, fitness, health and life admin organised in one place that comes to you on Telegram: reminders at the right moment, a short morning brief, an evening check-in, and a Sunday review that plans the week.

**Jonny**, the chief of staff, is the one voice you talk to. Behind him, one specialist agent runs each life area. The web dashboard shows everything at a glance.

**Jarvis**, the voice assistant in `jarvis/` that runs always-on on the Mac mini, is parked. Its voice face keeps working at `/talk`, but no new Jarvis work happens until Phase 3, when it plugs into the same system.

## Decisions

| Topic | Decision | Notes |
|---|---|---|
| Channel | Telegram bot | You write free text. Reminders and updates come back with ✅ Done / ⏰ +1h / 🌙 Tomorrow buttons |
| Cadence | One voice | Only Jonny messages on a schedule: 07:30 brief, 21:00 check-in, Sunday 19:00 review |
| Areas | Work · Projects · Fitness · Health · Life (admin, money, home, people) | Defined in `lib/areas.ts` |
| Agents | Claude Managed Agents (hosted by Anthropic, beta) | Jonny plus 5 area agents. Scheduled check-ins run on Anthropic's scheduler |
| Models | Jonny: Claude Opus 5.5. Area agents: Claude Sonnet 5.5 | Change in `lib/areas.ts`, then re-sync |
| Hosting | Vercel (the existing project) | Dashboard and all endpoints |
| Database | Neon Postgres via the Vercel Marketplace | Pick the region closest to you |
| Reminder trigger | GitHub Actions, every 5 minutes | Free. Can run 5–15 minutes late (see Risks) |
| Timezone | `TIMEZONE` env var | All reminders and check-ins use it |
| Jarvis | Parked | Its voice face moves from `/` to `/talk` and keeps working. It joins Life HQ in Phase 3 |

## How it works

```
   You ── Telegram ──► /api/telegram ──► Jonny (today's chat session) ──┐
    ▲                                      │ delegates when it needs    │
    │                                      ▼ a specialist               │  tools
    │                           Work · Projects · Fitness ·             ├──► /api/mcp ──► Postgres
    │                           Health · Life agents ───────────────────┘                (reminders, tasks,
    │                                                                                     goals, notes, logs,
    ├── reminders ◄── /api/cron/tick ◄── GitHub Actions (every 5 min)                     messages)
    ├── brief · check-in · review ◄── send_telegram ◄── scheduled runs (Anthropic)            ▲
    └── dashboard (Vercel, password) ─────────────────────────────────────────────────────────┘
```

The agents never touch the database directly. The app exposes a small set of tools at `/api/mcp`. MCP is the standard way to hand tools to AI agents. The tools are `get_overview`, `add_reminder`, `update_reminder`, `add_item`, `update_item`, `log_entry`, `get_logs` and `send_telegram`. Jarvis will use the same tools.

**You text the bot.** Telegram calls `/api/telegram`, which accepts only Telegram's secret header and your chat id. The message goes to today's Jonny session, which starts fresh each day because lasting memory lives in the database. It carries a note of anything you were sent since you last wrote, so a reply like "7h, ran 30 min" makes sense. Jonny reads and writes through the tools, and hands off to an area agent when the request needs specialist planning. His reply is relayed to Telegram. If a reply takes longer than the webhook's 5-minute window, the next tick relays it.

**A reminder comes due.** GitHub Actions calls `/api/cron/tick` every 5 minutes. Due reminders are claimed atomically, so none is sent twice, and arrive with the Done/Snooze buttons. Repeating reminders roll forward in local time, so "every day at 9:00" stays at 9:00 across daylight-saving changes. If Telegram fails, the reminder is retried on the next tick.

**A scheduled check-in fires.** Anthropic starts Jonny in a fresh session at 07:30, 21:00 and Sunday 19:00, each with a hard spend cap. He reads the overview and sends one message with `send_telegram`, or nothing if there is nothing useful to say.

**Sunday review.** Jonny starts a workflow run in which each area agent reviews its last 7 days and plans the next week, creating the reminders and tasks it needs. Jonny then sends one combined message.

## Your week with it

| When | What arrives | From |
|---|---|---|
| Daily 07:30 | Morning brief: today's reminders and deadlines by area, anything overdue, top 3 to focus on. On Mondays, also the week's deadlines. Warns if the reminder trigger has stalled | Jonny |
| Any time | Your reminders, with Done / +1h / Tomorrow | Reminder |
| Daily 21:00 | Evening check-in. Reply in one line, e.g. "7h sleep, ran 30 min, mood 4, energy 3", and Jonny logs it | Jonny |
| Sunday 19:00 | Weekly review and plan: wins, what slipped and next week's focus per area, plus the reminders created | Jonny and the area agents |

Things to text Jonny:

- "remind me to call mum Sunday at 6pm"
- "every weekday at 8:30 remind me to plan my day"
- "new project goal: launch the side project by March"
- "log 45 min run, felt strong"
- "plan my training for next week" (the Fitness agent does this)
- "what's on today?", or `/today` for an instant summary with no AI involved

## What's in the repo

| Path | What it does |
|---|---|
| `lib/areas.ts` | **The one file to edit**: areas, each agent's focus, models, check-in times, spend caps |
| `lib/agents.ts` | Creates or updates the hosted agents, the vault holding `MCP_TOKEN`, and the schedules (Setup → Sync) |
| `lib/chat.ts` | Telegram ↔ Jonny bridge: daily session, relaying replies exactly once |
| `lib/mcp-tools.ts`, `app/api/mcp/` | The tools the agents (and later Jarvis) use |
| `lib/store.ts`, `lib/db.ts` | Data access and table definitions |
| `lib/time.ts` | Timezone, date parsing, repeating reminders |
| `lib/telegram.ts`, `app/api/telegram/` | Bot API calls and the webhook |
| `app/api/cron/tick/`, `.github/workflows/reminder-tick.yml` | The reminder trigger |
| `app/page.tsx`, `app/setup/` | Dashboard and setup checklist |
| `app/talk/`, `app/about/`, `app/files/`, `lib/brain.ts`, `app/api/{chat,chat-stream,stt,tts,profile,research,workspace,ws-ticket}/` | Jarvis's voice face (moved from `/` to `/talk`), proxied to the Mac over the tunnel. Unchanged |
| `jarvis/` | Parked voice assistant (untouched) |

**Data:** `items` (task / goal / note per area), `reminders`, `logs` (workouts, sleep, mood…), `messages` (the activity feed), plus `settings` and `processed_keys` (stored agent ids, de-duplication). The agents only see this through the tools.

## Cost

These are rough estimates, to check against real numbers after week 1:

- **Chatting with Jonny (Opus 5.5):** about $0.03–0.08 per message, so roughly $10–25/month at ~10 messages a day.
- **Scheduled runs:** 15 a week, so about $5/month. The Sunday review costs the most at about $0.50.
- **Everything else is free tier:** Neon, Vercel Hobby, Telegram and GitHub Actions on a public repo.
- **Total:** roughly **$15–30/month**.
- **Hard caps, enforced by Anthropic:**
  - $3 per day for the chat. Chat pauses until tomorrow if it's hit.
  - $1 per check-in.
  - $2.50 per Sunday review.
- **Real spend:** Anthropic Console → Managed Agents → Sessions shows the cost of each session.

## Security and privacy

- **Access to each part:**
  - **Dashboard:** a password and a signed cookie. Every page and action requires it.
  - **`/api/telegram`:** requires Telegram's secret header, and only accepts your chat id. Everyone else is ignored.
  - **`/api/mcp`:** requires the bearer `MCP_TOKEN`. The agents hold it in an Anthropic vault, never in prompts.
  - **`/api/cron/tick`:** requires the bearer `CRON_SECRET`.
- **The GitHub repo is public.** No secrets are in code, but the agent prompts and this plan are visible. Your name only enters the prompts through the `OWNER_NAME` env var.
- **Where your data lives:**
  - Neon (the database).
  - Anthropic (Managed Agents keeps session history until it's deleted).
  - Telegram (bot chats are not end-to-end encrypted, so keep anything you consider very sensitive out of them).
- **Managed Agents is a beta product,** so its API can change.

## Roadmap

### Phase 0 · Foundation ✅

Built on branch `life-hq`. The pre-change state of `main` is tagged `pre-life-hq` locally.

- **What it contains:**
  - Database tables.
  - The MCP tools.
  - The Telegram webhook with Done/Snooze buttons.
  - The reminder trigger with atomic sending and daylight-saving-safe repeats.
  - The daily Jonny chat session.
  - Agent, vault and schedule sync.
  - The dashboard and the Setup page. The orb moved to `/talk`.
- **Verified:**
  - Type-check and production build.
  - Every database query run against a local Postgres: 23 checks covering reminders, snooze/done, repeats across daylight saving, failed sends and recovery, items, logs, and the check-in context.
  - A local run: all endpoints reject missing or wrong secrets, and the MCP server answers and lists its 8 tools.
- **Not verified yet:** anything that needs your real Telegram, Neon or Anthropic accounts. That happens in Phase 1.

### Phase 1 · Go live (about an hour, together)

1. **Telegram:** message @BotFather → `/newbot` → copy the token.
2. **Database:** Vercel → project → Storage → Create → Neon Postgres (nearest region). Connecting it sets `DATABASE_URL`.
3. **Secrets:** run `openssl rand -hex 32` three times, for `TELEGRAM_WEBHOOK_SECRET`, `MCP_TOKEN` and `CRON_SECRET`.
4. **Vercel env vars:** add the new ones (see `.env.example`) and set `TIMEZONE` and `OWNER_NAME`. Keep `DASHBOARD_PASSWORD`, `MAC_BRAIN_URL` and `JARVIS_TOKEN` as they are. `ANTHROPIC_API_KEY` may be missing, because the voice face never needed it; add it if `/setup` shows it unset.
5. **Ship:** merge `life-hq` into `main`. Vercel deploys production. The orb lives on at `/talk`.
6. **Setup page:**
   1. Create tables.
   2. Connect webhook.
   3. Send `/start` to the bot to see your chat id.
   4. Add `TELEGRAM_CHAT_ID` and redeploy.
   5. Send test message.
7. **Setup page:** Sync agents & schedules, then press "Run now" on the morning brief. It should arrive on Telegram within about 2 minutes.
8. **GitHub:** repo Settings → Secrets → Actions: add `APP_URL` and `CRON_SECRET`. Then Actions → "Reminder tick" → Run workflow once.
9. **Seed:** tell Jonny your current goals and recurring obligations (meds, bills, birthdays, training days) and check the dashboard.

**Done when all of these pass:**

- "remind me to drink water in 10 minutes" arrives with buttons, and Done works.
- "log 45 min run" shows up under Fitness.
- "what's on today?" answers from your data.
- Running the evening check-in, then replying in one line, logs the right things.
- Setup shows a recent tick.

### Phase 2 · Shake-down and tune (first two weeks)

Use it daily, then adjust from real use:

- Tune times, wording and what the brief and check-in include (`lib/areas.ts`, then re-sync).
- Show spend on the dashboard, using each session's reported cost.
- Send alerts to Telegram when a scheduled run fails, a schedule pauses, or the chat cap is hit.
- Projects: link tasks to a goal or project (sub-tasks, milestones).
- Habits and streaks, with one-tap check-offs in Telegram.
- Quiet hours: hold messages overnight until the morning.
- Edit and delete items inline on the dashboard. Polish the mobile layout.
- Fix monthly repeats that start on the 29th–31st, which drift to the 28th.
- If GitHub's timing is too loose, point a free per-minute service such as cron-job.org at the same endpoint.

### Phase 3 · Jarvis joins

Jarvis becomes a voice channel into the same system:

Jarvis already runs always-on on the Mac mini, with its own tool loop and a voice face at `/talk`. Joining means:

- **Give Jarvis the Life HQ tools.** Add the MCP server (`/api/mcp`, with `MCP_TOKEN`) to its tool loop. Then "hey Jarvis, remind me to…" or "what's on today?" reads and writes the same reminders, tasks and logs as Telegram.
- **Or hand longer requests to Jonny.** Jarvis forwards them to an authenticated endpoint (e.g. `/api/jarvis`) and speaks the reply, so planning stays with the area agents.
- **First, fix two issues still present in Jarvis:**
  - Its cost logging uses Sonnet 5's intro price, which expired on 2026-08-31, so turn costs read about a third low.
  - `make push` is unsafe to run from inside a `jonny/` checkout.

### Phase 4 · Later ideas

- Telegram voice notes, transcribed and passed to Jonny.
- Photo logging (meals, receipts).
- Calendar sync, so the brief knows your meetings.
- Dashboard charts (sleep, training volume, mood).
- A weekly email digest.
- Data export and backup.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| GitHub runs the tick late, skips it, or disables it after 60 days without commits | The morning brief warns when the last tick is over 60 minutes old, and Setup shows the last tick. Any scheduler can call the same endpoint, e.g. cron-job.org (free, every minute) |
| Managed Agents is in beta | The SDK version is pinned. Sync is idempotent and can be re-run. The tools are plain HTTP, so the agent loop could move to the Messages API if needed |
| A long reply outlives the webhook's 5 minutes | The next tick relays any unsent reply |
| An agent files something wrong (time, area) | Every change is confirmed in the reply with its time. Fix it on the dashboard or tell Jonny. The tools reject times they can't parse and one-off reminders in the past |
| Too many messages | One-voice cadence, and check-ins send nothing when there's nothing useful |
| Spend creeps up | Hard caps per day and per run. Check the Console weekly at first |
| Privacy of health data | See Security and privacy |

## Open questions (not blocking)

- Are 07:30, 21:00 and Sunday 19:00 the right times?
- What should the evening check-in track by default: sleep, training, mood, energy, anything else?
- What should be seeded on day one in each area?
