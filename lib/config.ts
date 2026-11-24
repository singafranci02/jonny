// The website's own settings. Everything else (Telegram, the agents, the
// reminders database, the iCloud folder) is configured on the Mac, in
// Jarvis's .env and config.yaml.

export const ENV_VARS = [
  { name: "DASHBOARD_PASSWORD", help: "password for this website" },
  { name: "MAC_BRAIN_URL", help: "the tunnel URL to Jarvis on your Mac" },
  { name: "JARVIS_TOKEN", help: "the same token as in the Mac's Jarvis .env" },
] as const;

export function envStatus() {
  return ENV_VARS.map((v) => ({ ...v, set: Boolean(process.env[v.name]?.trim()) }));
}
