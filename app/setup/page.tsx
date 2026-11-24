import { Nav } from "../components/Nav";
import { runCheckInAction, testTelegramAction } from "./actions";
import { brainConfig, brainFetch } from "@/lib/brain";
import { envStatus } from "@/lib/config";
import { lifeGet, type Status } from "@/lib/life";

export const dynamic = "force-dynamic";

type Health = { status: string; uptime_s: number; ollama_up: boolean };

async function health(): Promise<Health | null> {
  if (!brainConfig()) return null;
  try {
    const res = await brainFetch("/health", { cache: "no-store" }, 8_000);
    return res.ok ? ((await res.json()) as Health) : null;
  } catch {
    return null;
  }
}

function uptime(s: number): string {
  if (s < 3600) return `${Math.round(s / 60)} min`;
  if (s < 86400) return `${Math.round(s / 3600)} h`;
  return `${Math.round(s / 86400)} days`;
}

export default async function SetupPage({
  searchParams,
}: {
  searchParams: Promise<{ ok?: string; err?: string }>;
}) {
  const { ok, err } = await searchParams;
  const env = envStatus();
  const [brain, life] = await Promise.all([health(), lifeGet<Status>("/life/status")]);
  const s = life.ok ? life.data : null;

  return (
    <div className="shell">
      <Nav active="setup" />
      {ok && <p className="flash ok">{ok}</p>}
      {err && <p className="flash err">{err}</p>}

      <section className="card step">
        <h3>1 · This website (Vercel)</h3>
        <p className="muted small">
          The website only needs three settings; everything else lives on the Mac. Set them in Vercel → Project →
          Settings → Environment Variables, then redeploy.
        </p>
        <ul className="checklist">
          {env.map((v) => (
            <li key={v.name} className={v.set ? "done" : "todo"}>
              <code>{v.name}</code> <span className="muted">{v.help}</span>
            </li>
          ))}
        </ul>
      </section>

      <section className="card step">
        <h3>2 · Jarvis on the Mac mini</h3>
        <p className={brain ? "status-ok" : "status-todo"}>
          {brain ? `Brain is up (${uptime(brain.uptime_s)})${brain.ollama_up ? "" : " · Ollama is down"}` : "Brain not reachable"}
        </p>
        {!s && <p className="status-todo">Life HQ: {life.ok ? "" : life.error}</p>}
        {s && (
          <>
            <p className={s.folder.writable ? "status-ok" : "status-todo"}>
              Folder: <code>{s.folder.root}</code>{" "}
              {s.folder.writable ? "is writable" : s.folder.exists ? "exists but Jarvis can't write to it" : "doesn't exist yet"}
            </p>
            {!s.folder.writable && (
              <p className="muted small">
                iCloud Drive is privacy-protected. On the Mac: run <code>make life ARGS=python-path</code>, then System
                Settings → Privacy &amp; Security → Full Disk Access → + → add that file → restart Jarvis (
                <code>make install-web</code>). If the folder doesn&apos;t exist, run <code>make life ARGS=folders</code>.
              </p>
            )}
            <p className={s.telegram.configured && s.telegram.chat_id_set && s.telegram.telegram === "polling" ? "status-ok" : "status-todo"}>
              Telegram:{" "}
              {!s.telegram.configured
                ? "not set up"
                : !s.telegram.chat_id_set
                  ? `bot @${s.telegram.bot ?? "?"} works, chat id missing`
                  : `@${s.telegram.bot ?? "?"} · ${s.telegram.telegram}${s.telegram.last_poll ? ` · last checked ${s.telegram.last_poll}` : ""}`}
            </p>
            {(!s.telegram.configured || !s.telegram.chat_id_set) && (
              <p className="muted small">
                On the Mac, add <code>TELEGRAM_BOT_TOKEN=…</code> (from @BotFather) to Jarvis&apos;s <code>.env</code> and
                restart. Then send <code>/start</code> to your bot: it replies with your chat id. Add{" "}
                <code>TELEGRAM_CHAT_ID=…</code> and restart again.
              </p>
            )}
            <p className={s.scheduler_ok ? "status-ok" : "status-todo"}>
              Reminder sender: {s.scheduler_last_sweep ? `last ran ${s.scheduler_last_sweep}` : "hasn't run"}
            </p>
            <p className="muted small">
              Timezone <code>{s.timezone}</code> · Jarvis on <code>{s.coordinator_model}</code> · area agents on{" "}
              <code>{s.area_model ?? "?"}</code>
            </p>
            <div className="button-row">
              <form action={testTelegramAction}>
                <button type="submit" className="secondary">Send test Telegram message</button>
              </form>
            </div>
          </>
        )}
      </section>

      {s && (
        <section className="card step">
          <h3>3 · Check-ins</h3>
          <p className="muted small">Scheduled messages from Jarvis. Times and wording live in the Mac&apos;s config.yaml.</p>
          <table className="schedules">
            <thead>
              <tr>
                <th>Check-in</th>
                <th>Next</th>
                <th />
              </tr>
            </thead>
            <tbody>
              {s.checkins.map((c) => (
                <tr key={c.key}>
                  <td>{c.label}</td>
                  <td>{c.next}</td>
                  <td>
                    <form action={runCheckInAction}>
                      <input type="hidden" name="key" value={c.key} />
                      <button type="submit" className="ghost">Run now</button>
                    </form>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      )}
    </div>
  );
}
