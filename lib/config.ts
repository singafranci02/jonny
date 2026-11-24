// Central place for environment-driven settings. Secrets are read lazily so
// pages that don't need them still render (e.g. /setup shows what's missing).

export const ENV_VARS = [
  { name: "DASHBOARD_PASSWORD", required: true, help: "password for this dashboard" },
  { name: "ANTHROPIC_API_KEY", required: true, help: "Claude API key (Jonny and the area agents)" },
  { name: "DATABASE_URL", required: true, help: "Postgres URL (Neon via Vercel Marketplace)" },
  { name: "TELEGRAM_BOT_TOKEN", required: true, help: "from @BotFather" },
  { name: "TELEGRAM_CHAT_ID", required: true, help: "your chat id (send /start to the bot to see it)" },
  { name: "TELEGRAM_WEBHOOK_SECRET", required: true, help: "random string; Telegram echoes it on every update" },
  { name: "MCP_TOKEN", required: true, help: "random string; agents (and later Jarvis) use it to reach /api/mcp" },
  { name: "CRON_SECRET", required: true, help: "random string; protects /api/cron/tick" },
  { name: "TIMEZONE", required: true, help: "IANA zone for reminders, e.g. Europe/London" },
  { name: "APP_URL", required: false, help: "public https URL; defaults to the Vercel production URL" },
  { name: "OWNER_NAME", required: false, help: "your first name, used in agent prompts" },
  { name: "MAC_BRAIN_URL", required: false, help: "voice face at /talk: tunnel URL to Jarvis on your Mac" },
  { name: "JARVIS_TOKEN", required: false, help: "voice face at /talk: same token as the Mac's Jarvis .env" },
] as const;

export function envStatus() {
  return ENV_VARS.map((v) => ({
    ...v,
    set: Boolean(databaseUrlFor(v.name) ?? process.env[v.name]),
  }));
}

// Neon's Vercel integration sets POSTGRES_URL / DATABASE_URL; accept either.
function databaseUrlFor(name: string): string | undefined {
  if (name !== "DATABASE_URL") return undefined;
  return process.env.DATABASE_URL || process.env.POSTGRES_URL || undefined;
}

export function databaseUrl(): string {
  const url = databaseUrlFor("DATABASE_URL");
  if (!url) throw new Error("DATABASE_URL is not set");
  return url;
}

export function requireEnv(name: string): string {
  const value = process.env[name];
  if (!value) throw new Error(`${name} is not set`);
  return value;
}

export function timezone(): string {
  return process.env.TIMEZONE || "UTC";
}

export function ownerName(): string {
  return process.env.OWNER_NAME?.trim() || "the user";
}

export function appUrl(): string {
  const explicit = process.env.APP_URL?.trim().replace(/\/+$/, "");
  if (explicit) return explicit;
  const vercel = process.env.VERCEL_PROJECT_PRODUCTION_URL;
  return vercel ? `https://${vercel}` : "";
}

export function mcpUrl(): string {
  const base = appUrl();
  return base ? `${base}/api/mcp` : "";
}

/** Constant-time string compare (works in Edge and Node). */
export function safeEqual(a: string, b: string): boolean {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

export function bearerMatches(header: string | null, secret: string | undefined): boolean {
  if (!secret || !header?.startsWith("Bearer ")) return false;
  return safeEqual(header.slice("Bearer ".length), secret);
}
