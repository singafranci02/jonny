import { after, NextRequest, NextResponse } from "next/server";
import { chatWithJonny } from "@/lib/chat";
import { safeEqual } from "@/lib/config";
import { claimKey } from "@/lib/db";
import { completeReminder, getReminder, logMessage, snoozeReminder, todayText } from "@/lib/store";
import { answerCallback, editMessage, sendMessage, type TgCallbackQuery, type TgMessage, type TgUpdate } from "@/lib/telegram";
import { fmtWhen, tomorrowMorning } from "@/lib/time";

// Telegram webhook. Acknowledge immediately and do the work in after(), so
// Telegram never retries while an agent turn is running.

export const runtime = "nodejs";
export const maxDuration = 300;

export async function POST(request: NextRequest) {
  const secret = process.env.TELEGRAM_WEBHOOK_SECRET;
  const given = request.headers.get("x-telegram-bot-api-secret-token") ?? "";
  if (!secret || !safeEqual(given, secret)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  const update = (await request.json().catch(() => null)) as TgUpdate | null;
  if (!update?.update_id) return NextResponse.json({ ok: true });

  after(async () => {
    try {
      if (!(await claimKey(`tg-update:${update.update_id}`))) return; // duplicate delivery
      if (update.callback_query) await handleCallback(update.callback_query);
      else if (update.message) await handleMessage(update.message);
    } catch (err) {
      console.error("telegram update failed", update.update_id, err);
    }
  });
  return NextResponse.json({ ok: true });
}

function isOwner(chatId: number | undefined): boolean {
  const owner = process.env.TELEGRAM_CHAT_ID;
  return Boolean(owner && chatId !== undefined && String(chatId) === owner);
}

async function handleMessage(message: TgMessage) {
  const text = message.text?.trim() ?? "";

  // Before TELEGRAM_CHAT_ID is configured, /start tells you what to set it to.
  if (!isOwner(message.chat.id)) {
    if (!process.env.TELEGRAM_CHAT_ID && text.startsWith("/start")) {
      await sendMessage(
        `Hi! Your chat id is ${message.chat.id}. Set TELEGRAM_CHAT_ID=${message.chat.id} in Vercel and redeploy.`,
        { chatId: message.chat.id },
      );
    }
    return; // ignore everyone else
  }

  if (!text) {
    await sendMessage("I can only read text messages for now.");
    return;
  }
  if (text.startsWith("/start") || text.startsWith("/help")) {
    await sendMessage(
      "I'm Jonny, your life chief of staff. Just talk to me:\n" +
        "• “remind me to call mum Sunday 6pm”\n" +
        "• “log 45 min run”, “slept 7h”\n" +
        "• “plan my training this week”\n" +
        "• “what's on today?”\n\n" +
        "/today: quick summary without asking the agents",
    );
    return;
  }
  if (text.startsWith("/today")) {
    await logMessage({ direction: "in", source: "user", text });
    await sendMessage(await todayText());
    return;
  }
  await chatWithJonny(text);
}

async function handleCallback(query: TgCallbackQuery) {
  const chatId = query.message?.chat.id;
  if (!isOwner(chatId)) return answerCallback(query.id);
  const [action, idRaw, arg] = (query.data ?? "").split(":");
  const id = Number(idRaw);
  const reminder = Number.isFinite(id) ? await getReminder(id) : null;
  if (!reminder) return answerCallback(query.id, "That reminder no longer exists.");

  let status = "";
  if (action === "done") {
    await completeReminder(id);
    status = "✅ Done";
  } else if (action === "snooze") {
    const until = arg === "tomorrow" ? tomorrowMorning() : new Date(Date.now() + Number(arg || 60) * 60_000);
    await snoozeReminder(id, until);
    status = `⏰ Snoozed until ${fmtWhen(until)}`;
  }
  await answerCallback(query.id, status || undefined);
  if (status && query.message && chatId !== undefined) {
    await editMessage(chatId, query.message.message_id, `${query.message.text ?? `⏰ ${reminder.text}`}\n\n${status}`);
    await logMessage({ direction: "in", source: "user", area: reminder.area, text: `${status}: ${reminder.text}` });
  }
}
