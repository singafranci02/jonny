"use server";

import { after } from "next/server";
import { revalidatePath } from "next/cache";
import { isAreaId, type AreaId } from "@/lib/areas";
import { chatWithJonny } from "@/lib/chat";
import {
  addItem,
  addLog,
  addReminder,
  completeReminder,
  snoozeReminder,
  updateItem,
  updateReminder,
  type ItemKind,
} from "@/lib/store";
import { parseDate, parseLocal, RECURRENCES, type Recurrence } from "@/lib/time";

function str(form: FormData, key: string): string {
  return String(form.get(key) ?? "").trim();
}

function areaOf(form: FormData): AreaId {
  const area = str(form, "area");
  if (!isAreaId(area)) throw new Error(`unknown area ${area}`);
  return area;
}

export async function addReminderAction(form: FormData) {
  const text = str(form, "text");
  const due = parseLocal(str(form, "due"));
  const recurrence = str(form, "recurrence") as Recurrence;
  if (!text || !due) return;
  await addReminder({
    area: areaOf(form),
    text,
    dueAt: due,
    recurrence: RECURRENCES.includes(recurrence) ? recurrence : "none",
  });
  revalidatePath("/");
}

export async function reminderAction(form: FormData) {
  const id = Number(form.get("id"));
  const op = str(form, "op");
  if (op === "done") await completeReminder(id);
  else if (op === "cancel") await updateReminder(id, { status: "cancelled" });
  else if (op === "snooze") await snoozeReminder(id, new Date(Date.now() + 60 * 60_000));
  revalidatePath("/");
}

export async function addItemAction(form: FormData) {
  const title = str(form, "title");
  if (!title) return;
  const kind = (str(form, "kind") || "task") as ItemKind;
  const due = str(form, "due_date");
  await addItem({ area: areaOf(form), kind, title, dueDate: due ? parseDate(due) : null });
  revalidatePath("/");
}

export async function itemAction(form: FormData) {
  const id = Number(form.get("id"));
  const op = str(form, "op");
  if (op === "done") await updateItem(id, { status: "done" });
  else if (op === "archive") await updateItem(id, { status: "archived" });
  revalidatePath("/");
}

export async function addLogAction(form: FormData) {
  const metric = str(form, "metric");
  if (!metric) return;
  const raw = str(form, "value");
  const value = raw === "" ? null : Number(raw);
  await addLog({
    area: areaOf(form),
    metric,
    value: value != null && Number.isFinite(value) ? value : null,
    note: str(form, "note") || null,
  });
  revalidatePath("/");
}

/** Message Jonny from the dashboard; the reply arrives on Telegram (and in the activity feed). */
export async function askJonnyAction(form: FormData) {
  const text = str(form, "text");
  if (!text) return;
  after(() => chatWithJonny(text).catch((err) => console.error("askJonny failed", err)));
  revalidatePath("/");
}
