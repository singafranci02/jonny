"use server";

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";
import { lifePost, type LifeResult } from "@/lib/life";

// Every change goes to Jarvis on the Mac, which owns the data. A failure is
// shown on the dashboard (?err=...) instead of vanishing silently.

function str(form: FormData, key: string): string {
  return String(form.get(key) ?? "").trim();
}

function settle(res: LifeResult<unknown>) {
  if (!res.ok) redirect(`/?err=${encodeURIComponent(res.error)}`); // outside any try: redirect throws
  revalidatePath("/");
}

export async function addReminderAction(form: FormData) {
  const text = str(form, "text");
  const due = str(form, "due");
  if (!text || !due) return;
  settle(
    await lifePost("/life/reminders", {
      area: str(form, "area"),
      text,
      due,
      recurrence: str(form, "recurrence") || "none",
    }),
  );
}

export async function reminderAction(form: FormData) {
  const id = Number(form.get("id"));
  const op = str(form, "op");
  if (!Number.isInteger(id) || !["done", "snooze", "cancel"].includes(op)) return;
  settle(await lifePost(`/life/reminders/${id}/${op}`));
}

export async function addItemAction(form: FormData) {
  const title = str(form, "title");
  if (!title) return;
  settle(await lifePost("/life/items", { area: str(form, "area"), kind: str(form, "kind") || "task", title }));
}

export async function itemAction(form: FormData) {
  const id = Number(form.get("id"));
  const op = str(form, "op");
  if (!Number.isInteger(id) || !["done", "archive"].includes(op)) return;
  settle(await lifePost(`/life/items/${id}/${op}`));
}

export async function addLogAction(form: FormData) {
  const metric = str(form, "metric");
  if (!metric) return;
  const raw = str(form, "value");
  const value = raw === "" ? null : Number(raw);
  settle(
    await lifePost("/life/logs", {
      area: str(form, "area"),
      metric,
      value: value !== null && Number.isFinite(value) ? value : null,
      note: str(form, "note") || null,
    }),
  );
}
