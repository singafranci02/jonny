import { DateTime } from "luxon";
import { timezone } from "./config";

export const RECURRENCES = ["none", "daily", "weekdays", "weekly", "monthly"] as const;
export type Recurrence = (typeof RECURRENCES)[number];

export function nowLocal(): DateTime {
  return DateTime.now().setZone(timezone());
}

/** "Tue 24 Nov 2026, 22:48 (Australia/Sydney)" — prefixed onto chat messages. */
export function nowLabel(): string {
  return `${nowLocal().toFormat("ccc d LLL yyyy, HH:mm")} (${timezone()})`;
}

/** Local calendar date, used to rotate the daily chat session. */
export function todayKey(): string {
  return nowLocal().toISODate()!;
}

/**
 * Parse a date-time an agent or form gave us. Accepts local wall-clock
 * ("2026-11-25T09:00", "2026-11-25 09:00") interpreted in TIMEZONE, or a
 * full ISO string with an offset. Returns null if unparseable.
 */
export function parseLocal(input: string): Date | null {
  const s = input.trim().replace(" ", "T");
  const hasOffset = /([zZ]|[+-]\d{2}:?\d{2})$/.test(s);
  const dt = hasOffset
    ? DateTime.fromISO(s, { setZone: true })
    : DateTime.fromISO(s, { zone: timezone() });
  return dt.isValid ? dt.toJSDate() : null;
}

/** Parse "YYYY-MM-DD" (a calendar date, no time). */
export function parseDate(input: string): string | null {
  const dt = DateTime.fromISO(input.trim(), { zone: timezone() });
  return dt.isValid ? dt.toISODate() : null;
}

export function toLocal(d: Date | string): DateTime {
  return DateTime.fromJSDate(new Date(d)).setZone(timezone());
}

/** Human label relative to today: "Today 18:00", "Tomorrow 09:00", "Fri 28 Nov 09:00". */
export function fmtWhen(d: Date | string): string {
  const t = toLocal(d);
  const today = nowLocal().startOf("day");
  const days = Math.round(t.startOf("day").diff(today, "days").days);
  const time = t.toFormat("HH:mm");
  if (days === 0) return `Today ${time}`;
  if (days === 1) return `Tomorrow ${time}`;
  if (days === -1) return `Yesterday ${time}`;
  return t.toFormat(days > -7 && days < 7 ? "ccc HH:mm" : "ccc d LLL HH:mm");
}

/** Value for an <input type="datetime-local"> in the user's zone. */
export function toInputValue(d: Date | string): string {
  return toLocal(d).toFormat("yyyy-LL-dd'T'HH:mm");
}

/**
 * Next occurrence of a recurring reminder strictly after `after`, stepping
 * in local wall-clock time so DST doesn't drift "every day at 9:00".
 * Missed occurrences (e.g. the scheduler was down) are skipped, not replayed.
 */
export function nextOccurrence(due: Date | string, recurrence: Recurrence, after: Date): Date | null {
  if (recurrence === "none") return null;
  let t = toLocal(due);
  for (let i = 0; i < 2000 && t.toMillis() <= after.getTime(); i++) {
    switch (recurrence) {
      case "daily":
        t = t.plus({ days: 1 });
        break;
      case "weekdays":
        do t = t.plus({ days: 1 });
        while (t.weekday > 5);
        break;
      case "weekly":
        t = t.plus({ weeks: 1 });
        break;
      case "monthly":
        t = t.plus({ months: 1 });
        break;
    }
  }
  return t.toJSDate();
}

/** Tomorrow at 09:00 local — the "snooze till tomorrow" target. */
export function tomorrowMorning(): Date {
  return nowLocal().plus({ days: 1 }).set({ hour: 9, minute: 0, second: 0, millisecond: 0 }).toJSDate();
}
