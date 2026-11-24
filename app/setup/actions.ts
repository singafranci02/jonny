"use server";

import { redirect } from "next/navigation";
import { lifePost } from "@/lib/life";

async function finish(result: { ok: boolean; error?: string }, okMessage: string): Promise<never> {
  const q = result.ok ? `ok=${encodeURIComponent(okMessage)}` : `err=${encodeURIComponent(result.error ?? "failed")}`;
  redirect(`/setup?${q}`);
}

export async function testTelegramAction() {
  const res = await lifePost("/life/test-telegram");
  await finish(res.ok ? { ok: true } : { ok: false, error: res.error }, "Test message sent. Check Telegram.");
}

export async function runCheckInAction(form: FormData) {
  const key = String(form.get("key") ?? "");
  const res = await lifePost(`/life/checkins/${encodeURIComponent(key)}/run`);
  await finish(
    res.ok ? { ok: true } : { ok: false, error: res.error },
    "Started. Its message should reach Telegram within a minute or two.",
  );
}
