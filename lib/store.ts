// Data access for the organizer. Used by the dashboard, the Telegram bot,
// the reminder scheduler, and the MCP tools the agents call.

import { db, getSetting } from "./db";
import { AREAS, areaLabel, type AreaId } from "./areas";
import { fmtWhen, nextOccurrence, nowLabel, todayKey, type Recurrence } from "./time";
import { sendMessage, type InlineKeyboard } from "./telegram";

export type ItemKind = "task" | "goal" | "note";
export type ItemStatus = "open" | "done" | "archived";
export type ReminderStatus = "pending" | "sending" | "sent" | "done" | "cancelled";

export type Item = {
  id: number;
  area: string;
  kind: ItemKind;
  title: string;
  details: string | null;
  status: ItemStatus;
  priority: number;
  due_date: string | null;
  updated_at: string;
};

export type Reminder = {
  id: number;
  area: string;
  text: string;
  due_at: string;
  recurrence: Recurrence;
  status: ReminderStatus;
  last_sent_at: string | null;
};

export type LogEntry = {
  id: number;
  area: string;
  metric: string;
  value: number | null;
  unit: string | null;
  note: string | null;
  logged_at: string;
};

export type Message = {
  id: number;
  direction: "in" | "out";
  source: string;
  area: string | null;
  text: string;
  created_at: string;
};

const iso = (d: unknown) => (d == null ? null : new Date(d as string).toISOString());

function toReminder(r: Record<string, unknown>): Reminder {
  return { ...(r as unknown as Reminder), due_at: iso(r.due_at)!, last_sent_at: iso(r.last_sent_at) };
}
function toItem(r: Record<string, unknown>): Item {
  return { ...(r as unknown as Item), updated_at: iso(r.updated_at)! };
}
function toLog(r: Record<string, unknown>): LogEntry {
  return { ...(r as unknown as LogEntry), logged_at: iso(r.logged_at)! };
}
function toMessage(r: Record<string, unknown>): Message {
  return { ...(r as unknown as Message), created_at: iso(r.created_at)! };
}

// ---------------- reminders ----------------

export async function addReminder(input: {
  area: AreaId;
  text: string;
  dueAt: Date;
  recurrence?: Recurrence;
}): Promise<Reminder> {
  const rows = await db()`insert into reminders (area, text, due_at, recurrence)
    values (${input.area}, ${input.text}, ${input.dueAt.toISOString()}, ${input.recurrence ?? "none"})
    returning *`;
  return toReminder(rows[0]);
}

export async function updateReminder(
  id: number,
  patch: { text?: string; dueAt?: Date; recurrence?: Recurrence; status?: ReminderStatus; area?: AreaId },
): Promise<Reminder | null> {
  const rows = await db()`update reminders set
      text = coalesce(${patch.text ?? null}, text),
      due_at = coalesce(${patch.dueAt?.toISOString() ?? null}::timestamptz, due_at),
      recurrence = coalesce(${patch.recurrence ?? null}, recurrence),
      area = coalesce(${patch.area ?? null}, area),
      status = coalesce(${patch.status ?? null},
                        case when ${patch.dueAt ? 1 : 0} = 1 and status = 'sent' then 'pending' else status end),
      updated_at = now()
    where id = ${id} returning *`;
  return rows.length ? toReminder(rows[0]) : null;
}

export async function getReminder(id: number): Promise<Reminder | null> {
  const rows = await db()`select * from reminders where id = ${id}`;
  return rows.length ? toReminder(rows[0]) : null;
}

export async function upcomingReminders(opts: { area?: string; days?: number; limit?: number } = {}): Promise<Reminder[]> {
  const days = opts.days ?? 7;
  const rows = await db()`select * from reminders
    where status = 'pending'
      and due_at <= now() + make_interval(days => ${days})
      and (${opts.area ?? null}::text is null or area = ${opts.area ?? null})
    order by due_at asc limit ${opts.limit ?? 50}`;
  return rows.map(toReminder);
}

/** Reminders sent but not yet marked done (non-recurring), newest first. */
export async function awaitingReminders(limit = 10): Promise<Reminder[]> {
  const rows = await db()`select * from reminders where status = 'sent'
    and last_sent_at > now() - interval '3 days' order by last_sent_at desc limit ${limit}`;
  return rows.map(toReminder);
}

export function reminderKeyboard(id: number): InlineKeyboard {
  return {
    inline_keyboard: [
      [
        { text: "✅ Done", callback_data: `done:${id}` },
        { text: "⏰ +1h", callback_data: `snooze:${id}:60` },
        { text: "🌙 Tomorrow", callback_data: `snooze:${id}:tomorrow` },
      ],
    ],
  };
}

/**
 * Send every due reminder once. Rows are claimed atomically (pending ->
 * sending) so overlapping ticks can't double-send; rows stuck in "sending"
 * for 10+ minutes (crashed tick) are returned to pending first.
 */
export async function processDueReminders(): Promise<{ sent: number; failed: number }> {
  const sql = db();
  await sql`update reminders set status = 'pending'
            where status = 'sending' and updated_at < now() - interval '10 minutes'`;
  const claimed = (
    await sql`update reminders set status = 'sending', updated_at = now()
              where status = 'pending' and due_at <= now()
              returning *`
  ).map(toReminder);

  let sent = 0;
  let failed = 0;
  for (const r of claimed) {
    const repeat = r.recurrence !== "none" ? ` · repeats ${r.recurrence}` : "";
    const text = `⏰ ${r.text}\n${areaLabel(r.area)}${repeat}`;
    try {
      await sendMessage(text, { replyMarkup: reminderKeyboard(r.id) });
      await logMessage({ direction: "out", source: "reminder", area: r.area, text: `⏰ ${r.text} (reminder #${r.id})` });
      const next = nextOccurrence(r.due_at, r.recurrence, new Date());
      if (next) {
        await sql`update reminders set status = 'pending', due_at = ${next.toISOString()},
                  last_sent_at = now(), updated_at = now() where id = ${r.id}`;
      } else {
        await sql`update reminders set status = 'sent', last_sent_at = now(), updated_at = now()
                  where id = ${r.id}`;
      }
      sent++;
    } catch (err) {
      console.error("reminder send failed", r.id, err);
      await sql`update reminders set status = 'pending', updated_at = now() where id = ${r.id}`;
      failed++;
    }
  }
  return { sent, failed };
}

/** Snooze: one-off reminders move; recurring ones get a one-off copy so the series is untouched. */
export async function snoozeReminder(id: number, until: Date): Promise<Reminder | null> {
  const r = await getReminder(id);
  if (!r) return null;
  if (r.recurrence === "none") return updateReminder(id, { dueAt: until, status: "pending" });
  return addReminder({ area: r.area as AreaId, text: r.text, dueAt: until });
}

/** Done: one-off reminders close; for recurring ones this just acknowledges today's occurrence. */
export async function completeReminder(id: number): Promise<Reminder | null> {
  const r = await getReminder(id);
  if (!r) return null;
  if (r.recurrence !== "none") return r;
  return updateReminder(id, { status: "done" });
}

// ---------------- items (tasks, goals, notes) ----------------

export async function addItem(input: {
  area: AreaId;
  kind: ItemKind;
  title: string;
  details?: string | null;
  priority?: number;
  dueDate?: string | null;
}): Promise<Item> {
  const rows = await db()`insert into items (area, kind, title, details, priority, due_date)
    values (${input.area}, ${input.kind}, ${input.title}, ${input.details ?? null},
            ${input.priority ?? 2}, ${input.dueDate ?? null})
    returning id, area, kind, title, details, status, priority, due_date::text as due_date, updated_at`;
  return toItem(rows[0]);
}

export async function updateItem(
  id: number,
  patch: {
    title?: string;
    details?: string | null;
    status?: ItemStatus;
    priority?: number;
    dueDate?: string | null;
    area?: AreaId;
    kind?: ItemKind;
  },
): Promise<Item | null> {
  const clearDue = patch.dueDate === null;
  const rows = await db()`update items set
      title = coalesce(${patch.title ?? null}, title),
      details = coalesce(${patch.details ?? null}, details),
      status = coalesce(${patch.status ?? null}, status),
      priority = coalesce(${patch.priority ?? null}::int, priority),
      area = coalesce(${patch.area ?? null}, area),
      kind = coalesce(${patch.kind ?? null}, kind),
      due_date = case when ${clearDue}::boolean then null
                      else coalesce(${patch.dueDate ?? null}::date, due_date) end,
      completed_at = case when ${patch.status ?? null}::text = 'done' then now()
                          when ${patch.status ?? null}::text = 'open' then null
                          else completed_at end,
      updated_at = now()
    where id = ${id}
    returning id, area, kind, title, details, status, priority, due_date::text as due_date, updated_at`;
  return rows.length ? toItem(rows[0]) : null;
}

export async function openItems(opts: { area?: string; limit?: number } = {}): Promise<Item[]> {
  const rows = await db()`select id, area, kind, title, details, status, priority,
      due_date::text as due_date, updated_at
    from items
    where status = 'open' and (${opts.area ?? null}::text is null or area = ${opts.area ?? null})
    order by (due_date is null), due_date asc, priority asc, updated_at desc
    limit ${opts.limit ?? 100}`;
  return rows.map(toItem);
}

export async function recentlyDone(opts: { area?: string; days?: number } = {}): Promise<Item[]> {
  const rows = await db()`select id, area, kind, title, details, status, priority,
      due_date::text as due_date, updated_at
    from items
    where status = 'done' and completed_at > now() - make_interval(days => ${opts.days ?? 7})
      and (${opts.area ?? null}::text is null or area = ${opts.area ?? null})
    order by completed_at desc limit 30`;
  return rows.map(toItem);
}

// ---------------- logs (workouts, sleep, weight, mood...) ----------------

export async function addLog(input: {
  area: AreaId;
  metric: string;
  value?: number | null;
  unit?: string | null;
  note?: string | null;
  loggedAt?: Date;
}): Promise<LogEntry> {
  const rows = await db()`insert into logs (area, metric, value, unit, note, logged_at)
    values (${input.area}, ${input.metric.trim().toLowerCase()}, ${input.value ?? null},
            ${input.unit ?? null}, ${input.note ?? null}, ${(input.loggedAt ?? new Date()).toISOString()})
    returning *`;
  return toLog(rows[0]);
}

export async function getLogs(opts: { area?: string; metric?: string; days?: number; limit?: number } = {}): Promise<LogEntry[]> {
  const rows = await db()`select * from logs
    where logged_at > now() - make_interval(days => ${opts.days ?? 30})
      and (${opts.area ?? null}::text is null or area = ${opts.area ?? null})
      and (${opts.metric ?? null}::text is null or metric = ${opts.metric?.toLowerCase() ?? null})
    order by logged_at desc limit ${opts.limit ?? 200}`;
  return rows.map(toLog);
}

// ---------------- messages (activity feed) ----------------

export async function logMessage(m: { direction: "in" | "out"; source: string; area?: string | null; text: string }) {
  await db()`insert into messages (direction, source, area, text)
             values (${m.direction}, ${m.source}, ${m.area ?? null}, ${m.text})`;
}

export async function recentMessages(limit = 20): Promise<Message[]> {
  const rows = await db()`select * from messages order by created_at desc limit ${limit}`;
  return rows.map(toMessage);
}

/**
 * Check-ins and reminders sent since the user last wrote (within 24h). The
 * Telegram chat session never saw these (they come from other sessions or
 * the scheduler), so they're passed along as context with the next message.
 */
export async function unseenOutbound(): Promise<Message[]> {
  const rows = await db()`select * from messages
    where direction = 'out' and source in ('checkin', 'reminder')
      and created_at > now() - interval '24 hours'
      and created_at > coalesce((select max(created_at) from messages where direction = 'in'), '-infinity'::timestamptz)
    order by created_at asc limit 6`;
  return rows.map(toMessage);
}

/** Send to Telegram and record it in the activity feed. */
export async function notify(text: string, opts: { source: string; area?: string | null; replyMarkup?: InlineKeyboard }) {
  await sendMessage(text, { replyMarkup: opts.replyMarkup });
  await logMessage({ direction: "out", source: opts.source, area: opts.area ?? null, text });
}

// ---------------- overview (what agents and /today read) ----------------

export async function overview(area?: AreaId) {
  const [items, reminders, logs, done, lastTick] = await Promise.all([
    openItems({ area, limit: 60 }),
    upcomingReminders({ area, days: 7, limit: 40 }),
    getLogs({ area, days: 7, limit: 40 }),
    recentlyDone({ area, days: 7 }),
    getSetting<{ at: string }>("last_tick"),
  ]);
  const tickAgeMin = lastTick ? Math.round((Date.now() - new Date(lastTick.at).getTime()) / 60_000) : null;
  return {
    now: nowLabel(),
    scheduler_last_tick: tickAgeMin == null ? "never" : `${tickAgeMin} minutes ago`,
    areas: area ? undefined : AREAS.map((a) => ({ id: a.id, name: a.name, covers: a.blurb })),
    open_items: items.map((i) => ({
      id: i.id,
      area: i.area,
      kind: i.kind,
      title: i.title,
      details: i.details ?? undefined,
      priority: i.priority,
      due_date: i.due_date ?? undefined,
    })),
    reminders_next_7_days: reminders.map((r) => ({
      id: r.id,
      area: r.area,
      text: r.text,
      due: fmtWhen(r.due_at),
      recurrence: r.recurrence,
    })),
    logs_last_7_days: logs.map((l) => ({
      area: l.area,
      metric: l.metric,
      value: l.value ?? undefined,
      unit: l.unit ?? undefined,
      note: l.note ?? undefined,
      when: fmtWhen(l.logged_at),
    })),
    done_last_7_days: done.map((i) => ({ id: i.id, area: i.area, title: i.title })),
  };
}

/** Plain-text summary for the /today Telegram command (no model call). */
export async function todayText(): Promise<string> {
  const [reminders, items] = await Promise.all([upcomingReminders({ days: 1 }), openItems({ limit: 100 })]);
  const lines: string[] = [`📅 ${nowLabel()}`];
  lines.push("", reminders.length ? "Reminders (next 24h):" : "No reminders in the next 24h.");
  for (const r of reminders) lines.push(`• ${fmtWhen(r.due_at)} — ${r.text} (${areaLabel(r.area)})`);
  const today = todayKey();
  const due = items.filter((i) => i.due_date && i.due_date <= today);
  if (due.length) {
    lines.push("", "Due today or overdue:");
    for (const i of due) lines.push(`• ${i.title} (${areaLabel(i.area)}, ${i.due_date})`);
  }
  return lines.join("\n");
}
