# Jarvis · Life HQ: plan

_Last updated 2026-11-25. Status: built on branch `life-hq` (on top of `main`) and tested on the MacBook with fakes for Telegram and Claude. Not yet running on the Mac mini; that is Phase 1._

## Goal

Jarvis, the assistant that runs always-on on the Mac mini, becomes the **main agent** of a personal organiser. You talk to Jarvis on Telegram, by voice, or on the website.

Behind him sits **one specialist agent per life area**: Work, Projects, Fitness, Health and Life. Each knows its area better than Jarvis could in every conversation, because it has three things of its own:

- its folder in iCloud Drive;
- a `CONTEXT.md` brief about that part of your life;
- that area's reminders, tasks and logs.

The area agents report to Jarvis, and Jarvis talks to you.

## Decisions

| Topic | Decision | Notes |
|---|---|---|
| Main agent | Jarvis on the Mac mini | Opus 5.5 for Telegram, the website and check-ins; voice keeps its existing fast models |
| Area agents | Work · Projects · Fitness · Health · Life | Sonnet 5.5, run by Jarvis on the Mac; several can work in parallel |
| Files | `iCloud Drive/Jarvis/`: one main folder, a subfolder per area, plus `Inbox/` | The same folder on the MacBook and the Mac mini |
| Data | Reminders, tasks, goals, notes, logs in a database on the Mac mini (`jarvis/data/life.db`) | A live database must not sit in iCloud; a nightly copy goes to `iCloud/Jarvis/_backups/` |
| Channel | A Telegram bot, polled by the Mac | No webhook and no cloud service in between |
| Cadence | One voice: 07:30 brief, 21:00 check-in, Sunday 19:00 review | Only Jarvis messages you; the area agents plan their week in the review |
| Website | A thin face onto the Mac | Dashboard, Setup, and the voice orb at `/talk`; no new Vercel settings |
| Mac off | Telegram holds your messages for 24 hours; reminders and check-ins wait | A check-in still runs if the Mac is back within 2 hours (4 for the Sunday review), otherwise it is skipped. The website says "offline" |

## How it works

```
 You ── Telegram ──────────┐
 You ── voice (/talk, Mac) ┼──►  JARVIS  (main agent, on the Mac mini)
 You ── website ───────────┘        │  quick things himself (a reminder, a log entry)
                                    │  delegate: several areas at once, in parallel
                                    ▼
        ┌────────┬──────────┬─────────┬────────┬────────┐
        │  Work  │ Projects │ Fitness │ Health │  Life  │   area agents (Sonnet 5.5)
        └───┬────┴────┬─────┴────┬────┴───┬────┴───┬────┘
            ▼ files: only their own folder, each with CONTEXT.md
   iCloud Drive/Jarvis/   Work/  Projects/  Fitness/  Health/  Life/  Inbox/
            ▼ data: only their own area
   Mac: data/life.db       reminders · tasks · goals · notes · logs · message feed

   scheduler (inside Jarvis): due reminders every 20s · 07:30 · 21:00 · Sun 19:00 · nightly backup
```

**You text the bot.** The Mac fetches your message from Telegram. Jarvis gets your message, the recent conversation, and anything you were just sent, so a reply like "7h sleep, ran 30 min, mood 4" to the evening check-in makes sense. He handles quick things himself, or delegates. For example, "plan my training week" goes to the Fitness agent: it reads `Fitness/CONTEXT.md` and your recent workouts, writes `Fitness/training-plan.md`, creates reminders for the sessions, and reports back. Jarvis then replies to you.

**A reminder comes due.** You get a Telegram message with ✅ Done, ⏰ +1h and 🌙 Tomorrow buttons. Repeating reminders roll forward in local time, so "every day at 9:00" stays at 9:00 across daylight-saving changes.

**A check-in fires.** Jarvis runs a scheduled turn and sends you the result. The Sunday review delegates to all five area agents at once, then sends one combined message.

## The iCloud folder

```
iCloud Drive/Jarvis/
  README.md
  Inbox/            anything not sorted yet (Jarvis can sort it)
  Work/  CONTEXT.md  + plans, notes, documents
  Projects/ CONTEXT.md + one subfolder per project
  Fitness/ CONTEXT.md + training-plan.md, ...
  Health/  CONTEXT.md
  Life/    CONTEXT.md
  _backups/         nightly copies of the reminders/tasks/logs database
  _trash/           older versions, kept automatically before any overwrite (also inside each area)
```

- **Fill in the `CONTEXT.md` files.** Add goals, routines, people and important dates. Each area agent reads its brief before every task and adds what it learns, so this is the single biggest upgrade to how useful the agents are. You can edit them from either Mac.
- **Each area agent can only touch its own folder.** Jarvis can reach the whole folder. Nothing can delete files: overwrites keep the old version in `_trash/`.
- **Where it lives:** the folder already exists, created from the MacBook on 2026-11-25. iCloud syncs it to the Mac mini.

## Your week with it

| When | What arrives | From |
|---|---|---|
| Daily 07:30 | Morning brief: today's reminders and deadlines by area, anything overdue, top 3 to focus on (on Mondays, also the week's deadlines) | Jarvis |
| Any time | Your reminders, with Done / +1h / Tomorrow | Reminder |
| Daily 21:00 | Evening check-in. Reply in one line ("7h sleep, ran 30 min, mood 4") and Jarvis logs it | Jarvis |
| Sunday 19:00 | Weekly review and plan: each area agent reviews its week and plans the next; you get one combined message | Jarvis and the area agents |

Things to say to Jarvis:

- "remind me to call mum Sunday at 6pm"
- "every weekday at 8:30 remind me to plan my day"
- "log 45 min run", "slept 7h"
- "plan my training for next week" (Fitness agent)
- "how is the side project going?" (Projects agent)
- "save this in Work: …"
- `/today` on Telegram for an instant summary with no AI involved

## What's in the repo

| Path | What it does |
|---|---|
| `jarvis/config.yaml` → `life:` | **The one place to edit:** owner, timezone, areas (name, folder, focus), check-in times and wording, models (`llm.tiers.coordinator` / `area`) |
| `jarvis/src/life/agents.py` | The area agents and Jarvis's `delegate` tool |
| `jarvis/src/life/tools.py` | `life_*` tools for reminders, tasks, goals, notes and logs (area agents get copies locked to their area) |
| `jarvis/src/life/service.py` | Runs inside the brain server: Telegram, the reminder sender, check-ins, backups, dashboard data |
| `jarvis/src/life/store.py`, `clock.py`, `areas.py` | Database, time and recurrence, iCloud folders and `CONTEXT.md` |
| `jarvis/src/life/api.py`, `telegram.py`, `cli.py` | Website API (`/life/*`), Telegram client, `make life` |
| `jarvis/src/main.py`, `server.py`, `llm/anthropic_client.py` | Small hooks: separate text-channel history, parallel tool calls, model options |
| `jarvis/prompts/system.md` | Jarvis's persona, now including the folder and when to delegate |
| `jarvis/tests/test_life.py` | 20 tests, no network (`make test`) |
| `app/page.tsx`, `app/setup/`, `lib/life.ts` | The website dashboard and Setup page, reading from the Mac |
| `app/talk/`, `app/about/`, `app/files/`, `lib/brain.ts` | The voice face and its pages (unchanged apart from the nav bar and a timeout option) |

## Cost

These are rough estimates; check them against `jarvis/data/life_agents.log`, which records each area agent run with its cost.

- **A text message with Jarvis (Opus 5.5):** about $0.03–0.08. Each area agent he brings in adds about $0.02–0.10 (Sonnet 5.5).
- **Check-ins:** about $5/month. The brief and check-in cost about $0.05 each, daily; the Sunday review about $0.40.
- **At ~10 messages a day:** roughly **$15–30/month**. Voice keeps its current cheap models.
- **Spend limits:** no platform-enforced cap applies to these calls. Set a monthly spend limit in the Anthropic Console. Per run, the area agents are bounded at 12 tool rounds and 4 minutes.

## Security and privacy

- **The website** stays password-gated and reaches the Mac only through the existing token and tunnel.
- **Telegram:** Jarvis answers only your chat id and ignores everyone else.
- **Area agents** can only touch files inside their own folder (real paths are checked, so `../` and symlinks can't escape), can't delete anything, and their data tools are locked to their own area.
- **Where your data goes:**
  - Your data stays on your Macs and in iCloud.
  - Model calls go to Anthropic.
  - Telegram chats aren't end-to-end encrypted.
- **The repo is public:** no secrets are in code, and the Telegram token lives only in Jarvis's `.env` on the Mac.

## Roadmap

### Phase 0 · Built ✅

**What was built:**
- Jarvis's `delegate` tool and the five area agents.
- The iCloud folder structure, which already exists in your iCloud Drive.
- Reminders, tasks, goals, notes and logs on the Mac.
- Telegram with Done/Snooze buttons.
- The scheduler with the three check-ins and nightly backups.
- The website dashboard, Setup page, and "Ask Jarvis".

**Verified:**
- 20 unit tests (no network): time and daylight-saving recurrence, the database, area locks on tools, folder setup, the area agent loop including folder confinement, Telegram handling, the scheduler, the website API, and the turn-loop hooks.
- The real Anthropic SDK sends the right requests: checked against a local stand-in API.
- The website builds, and renders real data from the real `/life` API.

**Not verified yet:** real Telegram, real Claude calls, and the Mac mini's iCloud permissions. That is Phase 1.

### Phase 1 · Go live on the Mac mini (about 30 minutes)

1. **Telegram:** message @BotFather → `/newbot` → copy the token.
2. **Push:** push the `life-hq` branch to GitHub (Claude asks before pushing; the repo is public).
3. **Get the code onto the Mac mini.** Jarvis runs there from its own repo, so pull the new Jarvis code into it from the `jonny` checkout:

   ```sh
   cd ~/jonny && git fetch origin && git checkout life-hq
   git subtree split --prefix=jarvis -b jarvis-life-hq     # Jarvis-only history, incl. the new work
   cd <your Jarvis folder> && git pull ~/jonny jarvis-life-hq
   make setup && make test                                 # deps + the 20 tests
   ```

4. **Telegram on the Mac:**
   1. Add `TELEGRAM_BOT_TOKEN=…` to Jarvis's `.env`, then run `make install-web` to restart.
   2. Send `/start` to your bot: it replies with your chat id.
   3. Add `TELEGRAM_CHAT_ID=…` and restart again.
5. **iCloud access for the background service:**
   1. Run `make life ARGS=python-path`.
   2. In System Settings → Privacy & Security → Full Disk Access, add that file, then restart.
   3. In Finder → iCloud Drive → Jarvis, right-click → **Keep Downloaded**.
   4. Run `make life`: everything should be green.
6. **Website:** merge `life-hq` into `main`. Vercel deploys, and no new settings are needed.

**Done when all of these pass:**

- `/help` answers.
- "remind me in 5 minutes to stand up" arrives with buttons, and Done works.
- "plan my training week" makes the Fitness agent write `Fitness/training-plan.md`, which shows up on the MacBook too.
- The website's Setup page is all green.
- "Run now" on the morning brief arrives on Telegram.

**Then:** fill in the five `CONTEXT.md` files.

### Phase 2 · Tune (first two weeks)

- Adjust times, wording and the `CONTEXT.md` files from real use.
- Show weekly spend on the dashboard, from `life_agents.log`.
- Let Jarvis search the iCloud folder with his existing knowledge search, so he can answer from area files without delegating.
- Delegate in the background on voice: the website's voice face times out after 2 minutes, so long planning requests are better on Telegram for now.
- Quiet hours.
- Habits and streaks with one-tap check-offs.
- A "Mac offline" alert from an outside uptime monitor watching `/health`.
- Consider running Jarvis straight from the `jonny` repo on the Mac mini, so there is one copy of the code instead of two.

### Phase 3 · Later

- Calendar sync, so the brief knows your meetings.
- Telegram voice notes and photos (meals, receipts).
- A weekly email digest.
- More areas (Finance, Learning): one entry each in `config.yaml`.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| The Mac mini is off, asleep, or stuck at the FileVault login after a power cut: nothing is sent | Your existing liveness script (no sleep, power back on) and watchdog. Telegram holds messages for 24h; check-ins catch up within 2h; the website shows "offline". Phase 2 adds an outside alert |
| macOS blocks the background service from iCloud Drive | Full Disk Access for its python (`make life ARGS=python-path`). `make life` and the Setup page detect the problem from inside the server |
| iCloud keeps files cloud-only ("Optimize Mac Storage") | "Keep Downloaded" on the Jarvis folder on the Mac mini |
| The same file edited on both Macs at once | iCloud keeps both ("file 2.md"); the agents write mostly on the Mac mini, so this should be rare |
| Spend | A spend limit in the Anthropic Console; per-run bounds on the area agents; costs logged per run |
| Two copies of the Jarvis code (its own repo on the Mac mini, and `jonny/jarvis`) | Phase 1 step 3 syncs them; Phase 2 suggests keeping one |

## Open questions (not blocking)

- Are 07:30, 21:00 and Sunday 19:00 the right times, and is the timezone `Australia/Sydney` right?
- What should the evening check-in track by default: sleep, training, mood, energy?
- Anything to seed on day one in each `CONTEXT.md`?
