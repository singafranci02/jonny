import { Nav } from "../components/Nav";
import {
  connectTelegramAction,
  initDbAction,
  runCheckInAction,
  syncAgentsAction,
  testTelegramAction,
} from "./actions";
import { getAgentIds, scheduleStatuses, type AgentIds, type ScheduleStatus } from "@/lib/agents";
import { appUrl, envStatus, mcpUrl, timezone } from "@/lib/config";
import { db, getSetting } from "@/lib/db";
import { getMe, getWebhookInfo } from "@/lib/telegram";
import { fmtWhen } from "@/lib/time";

export const dynamic = "force-dynamic";
export const maxDuration = 120; // syncing agents makes ~20 API calls

async function attempt<T>(fn: () => Promise<T>): Promise<{ value?: T; error?: string }> {
  try {
    return { value: await fn() };
  } catch (err) {
    return { error: err instanceof Error ? err.message : String(err) };
  }
}

export default async function SetupPage({
  searchParams,
}: {
  searchParams: Promise<{ ok?: string; err?: string }>;
}) {
  const { ok, err } = await searchParams;
  const env = envStatus();
  const base = appUrl();

  const dbCheck = await attempt(async () => {
    await db()`select 1 from settings limit 1`;
    return true;
  });
  const dbReady = dbCheck.value === true;
  const [bot, hook, idsResult, lastTick] = await Promise.all([
    attempt(getMe),
    attempt(getWebhookInfo),
    dbReady ? attempt(getAgentIds) : Promise.resolve({ value: null as AgentIds | null }),
    dbReady ? getSetting<{ at: string; sent: number; replies: number }>("last_tick").catch(() => null) : null,
  ]);
  const ids = idsResult.value ?? null;
  const schedules: ScheduleStatus[] = ids?.coordinatorId ? await scheduleStatuses(ids).catch(() => []) : [];
  const expectedHook = base ? `${base}/api/telegram` : "";

  return (
    <div className="shell">
      <Nav active="setup" />
      {ok && <p className="flash ok">{ok}</p>}
      {err && <p className="flash err">{err}</p>}

      <section className="card step">
        <h3>1 · Environment variables</h3>
        <p className="muted small">Set these in Vercel → Project → Settings → Environment Variables, then redeploy.</p>
        <ul className="checklist">
          {env.map((v) => (
            <li key={v.name} className={v.set ? "done" : v.required ? "todo" : "optional"}>
              <code>{v.name}</code> <span className="muted">{v.help}</span>
            </li>
          ))}
        </ul>
        <p className="muted small">
          Public URL: <code>{base || "not detected"}</code> · Timezone: <code>{timezone()}</code>
        </p>
      </section>

      <section className="card step">
        <h3>2 · Database</h3>
        <p className={dbReady ? "status-ok" : "status-todo"}>
          {dbReady ? "Tables exist." : `Not ready: ${dbCheck.error}`}
        </p>
        <form action={initDbAction}>
          <button type="submit">{dbReady ? "Update tables" : "Create tables"}</button>
        </form>
      </section>

      <section className="card step">
        <h3>3 · Telegram</h3>
        {bot.value ? (
          <p className="status-ok">
            Bot: <a href={`https://t.me/${bot.value.username}`}>@{bot.value.username}</a>
          </p>
        ) : (
          <p className="status-todo">Bot not reachable: {bot.error}</p>
        )}
        <p className={hook.value?.url && hook.value.url === expectedHook ? "status-ok" : "status-todo"}>
          Webhook: <code>{hook.value?.url || "not set"}</code>
          {hook.value?.last_error_message && <span className="muted"> · last error: {hook.value.last_error_message}</span>}
        </p>
        <p className="muted small">
          Don&apos;t know your chat id? Set everything except <code>TELEGRAM_CHAT_ID</code>, connect the webhook, send{" "}
          <code>/start</code> to the bot, and it replies with your id.
        </p>
        <div className="button-row">
          <form action={connectTelegramAction}>
            <button type="submit">Connect webhook</button>
          </form>
          <form action={testTelegramAction}>
            <button type="submit" className="secondary">Send test message</button>
          </form>
        </div>
      </section>

      <section className="card step">
        <h3>4 · Agents and schedules</h3>
        <p className="muted small">
          Creates or updates the hosted agents (Jonny plus one per area), the vault holding the token for{" "}
          <code>{mcpUrl() || "/api/mcp"}</code>, and one scheduled check-in per entry in <code>lib/areas.ts</code>. Run
          it again after changing <code>lib/areas.ts</code> or <code>MCP_TOKEN</code>.
        </p>
        <p className={ids?.coordinatorId ? "status-ok" : "status-todo"}>
          {ids?.coordinatorId
            ? `Synced ${ids.syncedAt ? fmtWhen(ids.syncedAt) : ""} · ${Object.keys(ids.areaAgents).length} area agents · ${Object.keys(ids.deployments).length} schedules`
            : "Not synced yet."}
        </p>
        <form action={syncAgentsAction}>
          <button type="submit" disabled={!dbReady}>Sync agents &amp; schedules</button>
        </form>
        {schedules.length > 0 && (
          <table className="schedules">
            <thead>
              <tr>
                <th>Check-in</th>
                <th>Status</th>
                <th>Next run</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {schedules.map((s) => (
                <tr key={s.key}>
                  <td>{s.name}</td>
                  <td>{s.status}</td>
                  <td>{s.nextRun ? fmtWhen(s.nextRun) : "—"}</td>
                  <td>
                    <form action={runCheckInAction}>
                      <input type="hidden" name="key" value={s.key} />
                      <button type="submit" className="ghost">Run now</button>
                    </form>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

      <section className="card step">
        <h3>5 · Reminder scheduler</h3>
        <p className={lastTick ? "status-ok" : "status-todo"}>
          {lastTick ? `Last tick ${fmtWhen(lastTick.at)} (sent ${lastTick.sent}, relayed ${lastTick.replies})` : "Has not run yet."}
        </p>
        <p className="muted small">
          Something must call <code>{base || "https://…"}/api/cron/tick</code> every few minutes with header{" "}
          <code>Authorization: Bearer $CRON_SECRET</code>. The repo ships a GitHub Actions workflow for it: add repository
          secrets <code>APP_URL</code> and <code>CRON_SECRET</code> (GitHub → Settings → Secrets and variables → Actions).
        </p>
      </section>

      <section className="card step">
        <h3>Later · Jarvis</h3>
        <p className="muted small">
          Jarvis (or any MCP client) can use the same tools as the agents: MCP server <code>{mcpUrl() || "/api/mcp"}</code>{" "}
          with header <code>Authorization: Bearer $MCP_TOKEN</code>.
        </p>
      </section>
    </div>
  );
}
