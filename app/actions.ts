"use server";

import { revalidatePath } from "next/cache";
import { lifePost } from "@/lib/life";

// Every change goes to Jarvis on the Mac, which owns the data.

function str(form: FormData, key: string): string {
  return String(form.get(key) ?? "").trim();
}

export async function addReminderAction(form: FormData) {
  const text = str(form, "text");
  const due = str(form, "due");
  if (!text || !due) return;
  await lifePost("/life/reminders", {
    area: str(form, "area"),
    text,
    due,
    recurrence: str(form, "recurrence") || "none",
  });
  revalidatePath("/");
}

export async function reminderAction(form: FormData) {
  const id = Number(form.get("id"));
  const op = str(form, "op");
  if (!Number.isInteger(id) || !["done", "snooze", "cancel"].includes(op)) return;
  await lifePost(`/life/reminders/${id}/${op}`);
  revalidatePath("/");
}

export async function addItemAction(form: FormData) {
  const title = str(form, "title");
  if (!title) return;
  await lifePost("/life/items", { area: str(form, "area"), kind: str(form, "kind") || "task", title });
  revalidatePath("/");
}

export async function itemAction(form: FormData) {
  const id = Number(form.get("id"));
  const op = str(form, "op");
  if (!Number.isInteger(id) || !["done", "archive"].includes(op)) return;
  await lifePost(`/life/items/${id}/${op}`);
  revalidatePath("/");
}

export async function addLogAction(form: FormData) {
  const metric = str(form, "metric");
  if (!metric) return;
  const raw = str(form, "value");
  const value = raw === "" ? null : Number(raw);
  await lifePost("/life/logs", {
    area: str(form, "area"),
    metric,
    value: value !== null && Number.isFinite(value) ? value : null,
    note: str(form, "note") || null,
  });
  revalidatePath("/");
}

export type AskState = { question: string; reply: string; error: string };

/** Ask Jarvis from the dashboard. He may delegate to area agents, so allow
 *  several minutes; the reply also lands in the activity feed. */
export async function askJarvisAction(_prev: AskState, form: FormData): Promise<AskState> {
  const question = str(form, "text");
  if (!question) return { question: "", reply: "", error: "" };
  const res = await lifePost<{ text: string }>("/life/ask", { text: question }, 280_000);
  revalidatePath("/");
  return res.ok
    ? { question, reply: res.data.text, error: "" }
    : { question, reply: "", error: res.error };
}
