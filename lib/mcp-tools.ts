// The "life" MCP tools: the only way agents (and, later, Jarvis) touch the
// organizer. Served at /api/mcp behind a bearer token.

import type { McpServer } from "@modelcontextprotocol/server";
import { z } from "zod";
import { AREA_IDS, areaLabel } from "./areas";
import {
  addItem,
  addLog,
  addReminder,
  getLogs,
  notify,
  overview,
  updateItem,
  updateReminder,
} from "./store";
import { fmtWhen, parseDate, parseLocal, RECURRENCES } from "./time";

const area = z.enum(AREA_IDS).describe("Life area: work, projects, fitness, health or life");
const localDateTime = z
  .string()
  .describe('Local wall-clock date-time in the user\'s timezone, "YYYY-MM-DDTHH:mm"');
const localDate = z.string().describe('Calendar date "YYYY-MM-DD"');

type ToolResult = { content: { type: "text"; text: string }[]; isError?: boolean };
const ok = (text: string): ToolResult => ({ content: [{ type: "text", text }] });
const fail = (text: string): ToolResult => ({ content: [{ type: "text", text }], isError: true });

function guard<A>(fn: (args: A) => Promise<ToolResult>) {
  return async (args: A): Promise<ToolResult> => {
    try {
      return await fn(args);
    } catch (err) {
      console.error("mcp tool error", err);
      return fail(`Error: ${err instanceof Error ? err.message : String(err)}`);
    }
  };
}

export function registerLifeTools(server: McpServer): void {
  server.registerTool(
    "get_overview",
    {
      title: "Get overview",
      description:
        "Current local time plus open tasks/goals/notes, reminders due in the next 7 days, logs from the last 7 days and items done in the last 7 days. Pass an area to scope it. Call this before summarising or planning.",
      inputSchema: z.object({ area: area.optional() }),
    },
    guard(async ({ area }) => ok(JSON.stringify(await overview(area)))),
  );

  server.registerTool(
    "add_reminder",
    {
      title: "Add reminder",
      description:
        "Schedule a reminder that arrives on Telegram at due_at with Done/Snooze buttons. Use recurrence for repeating ones (the time of day is kept).",
      inputSchema: z.object({
        area,
        text: z.string().min(1).max(500).describe("What to remind about, phrased as the reminder itself"),
        due_at: localDateTime,
        recurrence: z.enum(RECURRENCES).optional().describe("Default none"),
      }),
    },
    guard(async ({ area, text, due_at, recurrence }) => {
      const due = parseLocal(due_at);
      if (!due) return fail(`Could not parse due_at "${due_at}". Use YYYY-MM-DDTHH:mm.`);
      if ((recurrence ?? "none") === "none" && due.getTime() < Date.now() - 60_000) {
        return fail(`due_at ${fmtWhen(due)} is in the past. Call get_overview for the current time.`);
      }
      const r = await addReminder({ area, text, dueAt: due, recurrence });
      const repeat = r.recurrence === "none" ? "" : `, repeats ${r.recurrence}`;
      return ok(`Reminder #${r.id} set for ${fmtWhen(r.due_at)}${repeat} (${areaLabel(r.area)}).`);
    }),
  );

  server.registerTool(
    "update_reminder",
    {
      title: "Update reminder",
      description: "Change, reschedule, complete or cancel a reminder by id (ids come from get_overview).",
      inputSchema: z.object({
        id: z.number().int(),
        text: z.string().min(1).max(500).optional(),
        due_at: localDateTime.optional(),
        recurrence: z.enum(RECURRENCES).optional(),
        status: z.enum(["pending", "done", "cancelled"]).optional(),
        area: area.optional(),
      }),
    },
    guard(async ({ id, text, due_at, recurrence, status, area }) => {
      const dueAt = due_at ? parseLocal(due_at) : undefined;
      if (due_at && !dueAt) return fail(`Could not parse due_at "${due_at}".`);
      const r = await updateReminder(id, { text, dueAt: dueAt ?? undefined, recurrence, status, area });
      if (!r) return fail(`No reminder #${id}.`);
      return ok(`Reminder #${r.id}: "${r.text}", ${r.status}, due ${fmtWhen(r.due_at)}, recurrence ${r.recurrence}.`);
    }),
  );

  server.registerTool(
    "add_item",
    {
      title: "Add task, goal or note",
      description:
        "Add a task (something to do, optionally with a due date), a goal (an outcome being worked towards) or a note (reference info) to an area.",
      inputSchema: z.object({
        area,
        kind: z.enum(["task", "goal", "note"]),
        title: z.string().min(1).max(300),
        details: z.string().max(4000).optional(),
        priority: z.number().int().min(1).max(3).optional().describe("1 high, 2 normal (default), 3 low"),
        due_date: localDate.optional(),
      }),
    },
    guard(async ({ area, kind, title, details, priority, due_date }) => {
      const dueDate = due_date ? parseDate(due_date) : null;
      if (due_date && !dueDate) return fail(`Could not parse due_date "${due_date}".`);
      const item = await addItem({ area, kind, title, details, priority, dueDate });
      return ok(`Added ${item.kind} #${item.id} "${item.title}" to ${areaLabel(item.area)}${item.due_date ? `, due ${item.due_date}` : ""}.`);
    }),
  );

  server.registerTool(
    "update_item",
    {
      title: "Update task, goal or note",
      description:
        'Edit, complete (status "done"), reopen or archive an item by id. Set due_date to "none" to clear it.',
      inputSchema: z.object({
        id: z.number().int(),
        title: z.string().min(1).max(300).optional(),
        details: z.string().max(4000).optional(),
        status: z.enum(["open", "done", "archived"]).optional(),
        priority: z.number().int().min(1).max(3).optional(),
        due_date: z.string().optional().describe('"YYYY-MM-DD", or "none" to clear'),
        area: area.optional(),
        kind: z.enum(["task", "goal", "note"]).optional(),
      }),
    },
    guard(async ({ id, due_date, ...rest }) => {
      let dueDate: string | null | undefined;
      if (due_date === "none") dueDate = null;
      else if (due_date) {
        dueDate = parseDate(due_date);
        if (!dueDate) return fail(`Could not parse due_date "${due_date}".`);
      }
      const item = await updateItem(id, { ...rest, dueDate });
      if (!item) return fail(`No item #${id}.`);
      return ok(`Item #${item.id} "${item.title}": ${item.status}, ${areaLabel(item.area)}${item.due_date ? `, due ${item.due_date}` : ""}.`);
    }),
  );

  server.registerTool(
    "log_entry",
    {
      title: "Log an entry",
      description:
        "Record something that happened: a workout, sleep, weight, mood, water, a medication taken, money spent… Use a short lowercase metric name and reuse existing names (see get_logs) so trends line up.",
      inputSchema: z.object({
        area,
        metric: z.string().min(1).max(60).describe('e.g. "workout", "sleep_hours", "weight", "mood", "steps"'),
        value: z.number().optional().describe("Numeric value if there is one (minutes, hours, kg, 1-5…)"),
        unit: z.string().max(20).optional(),
        note: z.string().max(2000).optional(),
        logged_at: localDateTime.optional().describe("When it happened; defaults to now"),
      }),
    },
    guard(async ({ area, metric, value, unit, note, logged_at }) => {
      const loggedAt = logged_at ? parseLocal(logged_at) : undefined;
      if (logged_at && !loggedAt) return fail(`Could not parse logged_at "${logged_at}".`);
      const entry = await addLog({ area, metric, value, unit, note, loggedAt: loggedAt ?? undefined });
      const v = entry.value != null ? ` ${entry.value}${entry.unit ? ` ${entry.unit}` : ""}` : "";
      return ok(`Logged ${entry.metric}${v} (${areaLabel(entry.area)}, ${fmtWhen(entry.logged_at)}).`);
    }),
  );

  server.registerTool(
    "get_logs",
    {
      title: "Get logs",
      description: "Log history for trends, newest first. Filter by area and/or metric; days defaults to 30.",
      inputSchema: z.object({
        area: area.optional(),
        metric: z.string().optional(),
        days: z.number().int().min(1).max(365).optional(),
      }),
    },
    guard(async ({ area, metric, days }) => {
      const logs = await getLogs({ area, metric, days, limit: 300 });
      return ok(
        JSON.stringify(
          logs.map((l) => ({ area: l.area, metric: l.metric, value: l.value, unit: l.unit, note: l.note, when: fmtWhen(l.logged_at) })),
        ),
      );
    }),
  );

  server.registerTool(
    "send_telegram",
    {
      title: "Send Telegram message",
      description:
        'Send a message to the user on Telegram. ONLY for "[scheduled]" check-ins; normal replies are delivered automatically.',
      inputSchema: z.object({
        text: z.string().min(1).max(3500),
        area: area.optional().describe("Which area the message is about, for the activity feed"),
      }),
    },
    guard(async ({ text, area }) => {
      await notify(text, { source: "checkin", area: area ?? null });
      return ok("Sent.");
    }),
  );
}
