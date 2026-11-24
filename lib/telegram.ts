// Minimal Telegram Bot API client (plain fetch, no library).

import { requireEnv } from "./config";

const MAX_LEN = 4000; // Telegram's hard limit is 4096 chars per message

type InlineButton = { text: string; callback_data: string };
export type InlineKeyboard = { inline_keyboard: InlineButton[][] };

type TelegramResponse<T> = { ok: boolean; result?: T; description?: string };

async function call<T = unknown>(method: string, body: object): Promise<T> {
  const token = requireEnv("TELEGRAM_BOT_TOKEN");
  const res = await fetch(`https://api.telegram.org/bot${token}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const json = (await res.json().catch(() => ({ ok: false }))) as TelegramResponse<T>;
  if (!json.ok) throw new Error(`telegram ${method} failed: ${json.description ?? res.status}`);
  return json.result as T;
}

function ownerChatId(): string {
  return requireEnv("TELEGRAM_CHAT_ID");
}

function chunks(text: string): string[] {
  if (text.length <= MAX_LEN) return [text];
  const out: string[] = [];
  let rest = text;
  while (rest.length > MAX_LEN) {
    const cut = rest.lastIndexOf("\n", MAX_LEN) > MAX_LEN / 2 ? rest.lastIndexOf("\n", MAX_LEN) : MAX_LEN;
    out.push(rest.slice(0, cut));
    rest = rest.slice(cut).replace(/^\n/, "");
  }
  if (rest) out.push(rest);
  return out;
}

/** Plain-text message to the owner's chat (no parse_mode: agent text never breaks formatting). */
export async function sendMessage(
  text: string,
  opts: { replyMarkup?: InlineKeyboard; chatId?: string | number } = {},
): Promise<number | undefined> {
  const parts = chunks(text.trim() || "(empty)");
  let lastId: number | undefined;
  for (let i = 0; i < parts.length; i++) {
    const msg = await call<{ message_id: number }>("sendMessage", {
      chat_id: opts.chatId ?? ownerChatId(),
      text: parts[i],
      link_preview_options: { is_disabled: true },
      ...(i === parts.length - 1 && opts.replyMarkup ? { reply_markup: opts.replyMarkup } : {}),
    });
    lastId = msg.message_id;
  }
  return lastId;
}

export async function sendTyping(): Promise<void> {
  await call("sendChatAction", { chat_id: ownerChatId(), action: "typing" }).catch(() => {});
}

export async function answerCallback(callbackQueryId: string, text?: string): Promise<void> {
  await call("answerCallbackQuery", { callback_query_id: callbackQueryId, text }).catch(() => {});
}

/** Replace a message's text and drop its buttons (used after Done/Snooze). */
export async function editMessage(chatId: number | string, messageId: number, text: string): Promise<void> {
  await call("editMessageText", {
    chat_id: chatId,
    message_id: messageId,
    text: text.slice(0, MAX_LEN),
    link_preview_options: { is_disabled: true },
  }).catch(() => {});
}

export async function setWebhook(url: string, secret: string): Promise<void> {
  await call("setWebhook", {
    url,
    secret_token: secret,
    allowed_updates: ["message", "callback_query"],
    drop_pending_updates: true,
  });
}

export async function getWebhookInfo(): Promise<{ url: string; last_error_message?: string; pending_update_count?: number }> {
  return call("getWebhookInfo", {});
}

export async function getMe(): Promise<{ username: string }> {
  return call("getMe", {});
}

// ---- incoming update shapes (only the fields we use) ----

export type TgUser = { id: number; first_name?: string };
export type TgChat = { id: number; type: string };
export type TgMessage = {
  message_id: number;
  from?: TgUser;
  chat: TgChat;
  text?: string;
  date: number;
};
export type TgCallbackQuery = {
  id: string;
  from: TgUser;
  message?: TgMessage;
  data?: string;
};
export type TgUpdate = {
  update_id: number;
  message?: TgMessage;
  callback_query?: TgCallbackQuery;
};
