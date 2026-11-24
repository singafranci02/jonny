"use server";

import { redirect } from "next/navigation";
import { runCheckInNow, syncAgents } from "@/lib/agents";
import { appUrl, requireEnv } from "@/lib/config";
import { ensureSchema } from "@/lib/db";
import { notify } from "@/lib/store";
import { setWebhook } from "@/lib/telegram";

async function run(task: () => Promise<string>): Promise<never> {
  let target: string;
  try {
    target = `/setup?ok=${encodeURIComponent(await task())}`;
  } catch (err) {
    console.error("setup action failed", err);
    target = `/setup?err=${encodeURIComponent(err instanceof Error ? err.message : String(err))}`;
  }
  redirect(target); // outside try: redirect() works by throwing
}

export async function initDbAction() {
  await run(async () => {
    await ensureSchema();
    return "Database tables are ready.";
  });
}

export async function connectTelegramAction() {
  await run(async () => {
    const base = appUrl();
    if (!base) throw new Error("No public URL yet: deploy to Vercel or set APP_URL.");
    await setWebhook(`${base}/api/telegram`, requireEnv("TELEGRAM_WEBHOOK_SECRET"));
    return `Telegram now delivers messages to ${base}/api/telegram.`;
  });
}

export async function testTelegramAction() {
  await run(async () => {
    await notify("👋 Jonny here. Telegram is connected. Send /help to see what I can do.", { source: "system" });
    return "Test message sent. Check Telegram.";
  });
}

export async function syncAgentsAction() {
  await run(async () => {
    const log = await syncAgents();
    return `Agents synced: ${log.join("; ")}`;
  });
}

export async function runCheckInAction(form: FormData) {
  const key = String(form.get("key") ?? "");
  await run(async () => {
    const id = await runCheckInNow(key);
    return `Started "${key}" now (session ${id}). Its message should reach Telegram within a minute or two.`;
  });
}
