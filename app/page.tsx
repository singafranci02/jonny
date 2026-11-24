import Link from "next/link";
import { Nav } from "./components/Nav";
import {
  addItemAction,
  addLogAction,
  addReminderAction,
  askJonnyAction,
  itemAction,
  reminderAction,
} from "./actions";
import { AREAS, COORDINATOR, areaLabel } from "@/lib/areas";
import { getAgentIds } from "@/lib/agents";
import { getSetting, isMissingTable } from "@/lib/db";
import {
  awaitingReminders,
  getLogs,
  openItems,
  recentMessages,
  upcomingReminders,
  type Item,
  type LogEntry,
  type Message,
  type Reminder,
} from "@/lib/store";
import { fmtWhen, nowLocal, RECURRENCES, toInputValue } from "@/lib/time";

export const dynamic = "force-dynamic";
export const maxDuration = 300; // askJonnyAction runs an agent turn after responding

type Data = {
  upcoming: Reminder[];
  awaiting: Reminder[];
  items: Item[];
  logs: LogEntry[];
  messages: Message[];
  agentsSynced: boolean;
  lastTick: { at: string } | null;
};

async function load(): Promise<Data | { error: string }> {
  try {
    const [upcoming, awaiting, items, logs, messages, ids, lastTick] = await Promise.all([
      upcomingReminders({ days: 7, limit: 30 }),
      awaitingReminders(8),
      openItems({ limit: 200 }),
      getLogs({ days: 14, limit: 60 }),
      recentMessages(25),
      getAgentIds(),
      getSetting<{ at: string }>("last_tick"),
    ]);
    return { upcoming, awaiting, items, logs, messages, agentsSynced: Boolean(ids?.coordinatorId), lastTick };
  } catch (err) {
    if (isMissingTable(err)) return { error: "The database tables don't exist yet." };
    return { error: err instanceof Error ? err.message : String(err) };
  }
}

export default async function Dashboard() {
  const data = await load();
  const now = nowLocal();

  if ("error" in data) {
    return (
      <div className="shell">
        <Nav active="home" />
        <section className="card setup-needed">
          <h2>Almost there</h2>
          <p>{data.error}</p>
          <p>
            Finish the steps on <Link href="/setup">Setup</Link> to connect the database, Telegram and the agents.
          </p>
        </section>
      </div>
    );
  }

  const defaultDue = toInputValue(now.plus({ hours: 1 }).startOf("hour").toJSDate());

  return (
    <div className="shell">
      <Nav active="home" />

      {(!data.agentsSynced || !data.lastTick) && (
        <p className="banner">
          {!data.agentsSynced ? "Agents aren't synced yet. " : ""}
          {!data.lastTick ? "The reminder scheduler hasn't run yet. " : ""}
          <Link href="/setup">Open Setup →</Link>
        </p>
      )}

      <div className="hero">
        <div>
          <p className="eyebrow">{now.toFormat("cccc d LLLL")}</p>
          <h2 className="greeting">{greeting(now.hour)}</h2>
        </div>
        <form action={askJonnyAction} className="ask">
          <input name="text" placeholder="Ask Jonny… (the reply arrives on Telegram)" autoComplete="off" />
          <button type="submit">Send</button>
        </form>
      </div>

      <div className="grid-main">
        <section className="card">
          <div className="card-head">
            <h3>Next up</h3>
            <span className="muted">next 7 days</span>
          </div>
          {data.upcoming.length === 0 && <p className="empty">Nothing scheduled. Add a reminder below or tell Jonny.</p>}
          <ul className="list">
            {data.upcoming.map((r) => (
              <li key={r.id} className="row">
                <span className="when">{fmtWhen(r.due_at)}</span>
                <span className="what">
                  {r.text}
                  <span className="tag">{areaLabel(r.area)}</span>
                  {r.recurrence !== "none" && <span className="tag repeat">↻ {r.recurrence}</span>}
                </span>
                <form action={reminderAction} className="row-actions">
                  <input type="hidden" name="id" value={r.id} />
                  <button name="op" value="done" title="Done" className="ghost">✓</button>
                  <button name="op" value="snooze" title="Snooze 1h" className="ghost">+1h</button>
                  <button name="op" value="cancel" title="Cancel" className="ghost">✕</button>
                </form>
              </li>
            ))}
          </ul>
          {data.awaiting.length > 0 && (
            <>
              <p className="subhead">Sent, waiting for ✓</p>
              <ul className="list">
                {data.awaiting.map((r) => (
                  <li key={r.id} className="row dim">
                    <span className="when">{fmtWhen(r.last_sent_at ?? r.due_at)}</span>
                    <span className="what">{r.text}</span>
                    <form action={reminderAction} className="row-actions">
                      <input type="hidden" name="id" value={r.id} />
                      <button name="op" value="done" className="ghost">✓</button>
                    </form>
                  </li>
                ))}
              </ul>
            </>
          )}
          <form action={addReminderAction} className="inline-form">
            <input name="text" placeholder="Remind me to…" required />
            <AreaSelect />
            <input name="due" type="datetime-local" defaultValue={defaultDue} required />
            <select name="recurrence" defaultValue="none">
              {RECURRENCES.map((r) => (
                <option key={r} value={r}>
                  {r === "none" ? "once" : r}
                </option>
              ))}
            </select>
            <button type="submit">Add</button>
          </form>
        </section>

        <section className="card">
          <div className="card-head">
            <h3>Activity</h3>
            <span className="muted">Telegram, reminders, agents</span>
          </div>
          {data.messages.length === 0 && <p className="empty">No messages yet.</p>}
          <ul className="feed">
            {data.messages.map((m) => (
              <li key={m.id} className={`feed-item ${m.direction}`}>
                <span className="feed-meta">
                  {SOURCE_LABEL[m.direction === "in" ? "user" : m.source] ?? m.source}
                  {m.area ? ` · ${areaLabel(m.area)}` : ""} · {fmtWhen(m.created_at)}
                </span>
                <span className="feed-text">{m.text}</span>
              </li>
            ))}
          </ul>
        </section>
      </div>

      <h3 className="section-title">Areas</h3>
      <div className="areas">
        {AREAS.map((area) => {
          const items = data.items.filter((i) => i.area === area.id);
          const goals = items.filter((i) => i.kind === "goal");
          const tasks = items.filter((i) => i.kind === "task");
          const logs = data.logs.filter((l) => l.area === area.id).slice(0, 3);
          return (
            <section key={area.id} className={`card area area-${area.id}`}>
              <div className="card-head">
                <h3>
                  <span className="emoji">{area.emoji}</span> {area.name}
                </h3>
                <span className="muted">{tasks.length} open</span>
              </div>
              <p className="muted small">{area.blurb}</p>
              {goals.length > 0 && (
                <div className="goals">
                  {goals.slice(0, 3).map((g) => (
                    <span key={g.id} className="goal">🎯 {g.title}</span>
                  ))}
                </div>
              )}
              <ul className="list compact">
                {tasks.slice(0, 5).map((t) => (
                  <li key={t.id} className="row">
                    <form action={itemAction}>
                      <input type="hidden" name="id" value={t.id} />
                      <button name="op" value="done" className="check" title="Mark done" />
                    </form>
                    <span className="what">
                      {t.priority === 1 && <span className="prio">!</span>}
                      {t.title}
                      {t.due_date && <span className="tag">{t.due_date}</span>}
                    </span>
                  </li>
                ))}
                {tasks.length > 5 && <li className="muted small">+{tasks.length - 5} more</li>}
              </ul>
              {logs.length > 0 && (
                <ul className="logs">
                  {logs.map((l) => (
                    <li key={l.id}>
                      <span>
                        {l.metric}
                        {l.value != null ? ` ${l.value}${l.unit ? ` ${l.unit}` : ""}` : ""}
                        {l.note ? ` · ${l.note}` : ""}
                      </span>
                      <span className="muted">{fmtWhen(l.logged_at)}</span>
                    </li>
                  ))}
                </ul>
              )}
              <form action={addItemAction} className="mini-form">
                <input type="hidden" name="area" value={area.id} />
                <input name="title" placeholder="Add task…" />
                <select name="kind" defaultValue="task" aria-label="kind">
                  <option value="task">task</option>
                  <option value="goal">goal</option>
                  <option value="note">note</option>
                </select>
              </form>
              <p className="checkins">
                {area.checkIns.length ? (
                  area.checkIns.map((c) => <span key={c.key}>{c.label}</span>)
                ) : (
                  <span>Reports to {COORDINATOR.name} · plans its week on Sundays</span>
                )}
              </p>
            </section>
          );
        })}
        <section className="card area area-jonny">
          <div className="card-head">
            <h3>
              <span className="emoji">✨</span> {COORDINATOR.name}
            </h3>
            <span className="muted">chief of staff</span>
          </div>
          <p className="muted small">Talks to you on Telegram and coordinates the area agents.</p>
          <p className="checkins">
            {COORDINATOR.checkIns.map((c) => (
              <span key={c.key}>{c.label}</span>
            ))}
          </p>
          <p className="subhead">Quick log</p>
          <form action={addLogAction} className="log-form">
            <AreaSelect defaultValue="fitness" />
            <input name="metric" placeholder="metric (workout, sleep_hours…)" required />
            <input name="value" placeholder="value" inputMode="decimal" />
            <input name="note" placeholder="note" />
            <button type="submit">Log</button>
          </form>
        </section>
      </div>
    </div>
  );
}

function AreaSelect({ defaultValue }: { defaultValue?: string }) {
  return (
    <select name="area" defaultValue={defaultValue ?? "life"} aria-label="area">
      {AREAS.map((a) => (
        <option key={a.id} value={a.id}>
          {a.emoji} {a.name}
        </option>
      ))}
    </select>
  );
}

const SOURCE_LABEL: Record<string, string> = {
  user: "you",
  chat: "Jonny",
  checkin: "Jonny · check-in",
  reminder: "reminder",
  system: "system",
};

function greeting(hour: number): string {
  if (hour < 5) return "Late night";
  if (hour < 12) return "Good morning";
  if (hour < 18) return "Good afternoon";
  return "Good evening";
}
