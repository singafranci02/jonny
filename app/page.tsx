import Link from "next/link";
import { Nav } from "./components/Nav";
import { AskJarvis } from "./components/AskJarvis";
import { addItemAction, addLogAction, addReminderAction, itemAction, reminderAction } from "./actions";
import { lifeGet, type Area, type Dashboard as Data } from "@/lib/life";

// Life HQ: everything shown here lives on the Mac with Jarvis, who owns the
// reminders, tasks and logs; this page is a window onto it.

export const dynamic = "force-dynamic";
export const maxDuration = 300; // "Ask Jarvis" can involve area agents

const RECURRENCES = ["none", "daily", "weekdays", "weekly", "monthly"];

const SOURCE_LABEL: Record<string, string> = {
  user: "you",
  chat: "Jarvis",
  checkin: "Jarvis · check-in",
  reminder: "reminder",
  system: "system",
};

export default async function Dashboard() {
  const res = await lifeGet<Data>("/life/dashboard");

  if (!res.ok) {
    return (
      <div className="shell">
        <Nav active="home" />
        <section className="card setup-needed">
          <h2>Jarvis is offline</h2>
          <p>{res.error}</p>
          <p className="muted small">
            Reminders resume as soon as the Mac is back. <Link href="/setup">Setup</Link> shows what is connected.
          </p>
        </section>
      </div>
    );
  }

  const d = res.data;
  const label = (id: string | null) => {
    const a = d.areas.find((x) => x.id === id);
    return a ? `${a.emoji} ${a.name}` : (id ?? "");
  };
  const problems = [
    !d.status.telegram.configured && "Telegram isn't set up on the Mac yet",
    d.status.telegram.configured && !d.status.telegram.chat_id_set && "Telegram needs your chat id",
    !d.status.folder.writable && "Jarvis can't write to the iCloud folder",
    !d.status.scheduler_ok && "the reminder sender isn't running",
  ].filter(Boolean) as string[];

  return (
    <div className="shell">
      <Nav active="home" />

      {problems.length > 0 && (
        <p className="banner">
          Heads up: {problems.join(" · ")}. <Link href="/setup">Open Setup →</Link>
        </p>
      )}

      <div className="hero">
        <div>
          <p className="eyebrow">{d.date_label}</p>
          <h2 className="greeting">{greeting(d.hour)}</h2>
        </div>
        <AskJarvis />
      </div>

      <div className="grid-main">
        <section className="card">
          <div className="card-head">
            <h3>Next up</h3>
            <span className="muted">next 7 days</span>
          </div>
          {d.upcoming.length === 0 && <p className="empty">Nothing scheduled. Add a reminder below, or tell Jarvis.</p>}
          <ul className="list">
            {d.upcoming.map((r) => (
              <li key={r.id} className="row">
                <span className="when">{r.when}</span>
                <span className="what">
                  {r.text}
                  <span className="tag">{label(r.area)}</span>
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
          {d.awaiting.length > 0 && (
            <>
              <p className="subhead">Sent, waiting for ✓</p>
              <ul className="list">
                {d.awaiting.map((r) => (
                  <li key={r.id} className="row dim">
                    <span className="when">{r.when}</span>
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
            <AreaSelect areas={d.areas} />
            <input name="due" type="datetime-local" defaultValue={d.default_due} required />
            <select name="recurrence" defaultValue="none" aria-label="repeat">
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
            <span className="muted">Telegram, reminders, check-ins</span>
          </div>
          {d.messages.length === 0 && <p className="empty">No messages yet.</p>}
          <ul className="feed">
            {d.messages.map((m) => (
              <li key={m.id} className={`feed-item ${m.direction}`}>
                <span className="feed-meta">
                  {SOURCE_LABEL[m.direction === "in" ? "user" : m.source] ?? m.source}
                  {m.area ? ` · ${label(m.area)}` : ""} · {m.when}
                </span>
                <span className="feed-text">{m.text}</span>
              </li>
            ))}
          </ul>
        </section>
      </div>

      <h3 className="section-title">Areas</h3>
      <div className="areas">
        {d.areas.map((area) => {
          const items = d.items.filter((i) => i.area === area.id);
          const goals = items.filter((i) => i.kind === "goal");
          const tasks = items.filter((i) => i.kind === "task");
          const logs = d.logs.filter((l) => l.area === area.id).slice(0, 3);
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
                      <span className="muted">{l.when}</span>
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
                <button type="submit" className="secondary">Add</button>
              </form>
              <p className="checkins">
                <span>📁 iCloud › Jarvis › {area.folder}</span>
              </p>
            </section>
          );
        })}
        <section className="card area area-jonny">
          <div className="card-head">
            <h3>
              <span className="emoji">✨</span> Jarvis
            </h3>
            <span className="muted">main agent</span>
          </div>
          <p className="muted small">Talks to you on Telegram, by voice and here, and brings in the area agents.</p>
          <p className="checkins">
            {d.status.checkins.map((c) => (
              <span key={c.key} title={`next: ${c.next}`}>
                {c.label}
              </span>
            ))}
          </p>
          <p className="subhead">Quick log</p>
          <form action={addLogAction} className="log-form">
            <AreaSelect areas={d.areas} defaultValue="fitness" />
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

function AreaSelect({ areas, defaultValue }: { areas: Area[]; defaultValue?: string }) {
  const fallback = areas[areas.length - 1]?.id;
  return (
    <select name="area" defaultValue={defaultValue ?? fallback} aria-label="area">
      {areas.map((a) => (
        <option key={a.id} value={a.id}>
          {a.emoji} {a.name}
        </option>
      ))}
    </select>
  );
}

function greeting(hour: number): string {
  if (hour < 5) return "Late night";
  if (hour < 12) return "Good morning";
  if (hour < 18) return "Good afternoon";
  return "Good evening";
}
