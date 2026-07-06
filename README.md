# Jonny

A hovering ball of light you talk to. Password-gated dashboard, deployed on
Vercel, powered by Claude Sonnet 5.

- **Voice in/out:** browser speech recognition + speech synthesis (free, no
  audio ever leaves your device — only the text transcript goes to the API).
- **Always active:** click the orb once and Jonny keeps listening and
  answering until you click again.
- **Auth:** every page and API route sits behind a password checked against
  the `DASHBOARD_PASSWORD` env var (HMAC session cookie, 30 days).

## Deploy (Vercel)

1. Import `singafranci02/jonny` at vercel.com/new (framework: Next.js,
   defaults are fine).
2. Add two environment variables:
   - `DASHBOARD_PASSWORD` — the password the dashboard asks for
   - `ANTHROPIC_API_KEY` — your Claude API key
3. Deploy. Open the URL, enter the password, click the light, allow the
   microphone, talk.

Speech recognition needs Chrome, Edge, or Safari (HTTPS — Vercel provides it).

## Local dev

```sh
npm install
cp .env.example .env.local   # fill in both vars
npm run dev                  # http://localhost:3000
```

## Orb states

| color  | meaning |
|--------|---------|
| blue   | asleep — click to wake |
| green  | listening |
| purple | thinking (calling Claude) |
| amber  | speaking |
