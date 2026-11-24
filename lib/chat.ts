// Telegram <-> Jonny (coordinator) bridge.
//
// One Managed Agents session per local day holds the Telegram conversation
// (fresh context and a fresh spend cap each day; durable state lives in the
// database, which the agents read through MCP tools). A user message is sent
// to that session, its event stream is followed until the agent goes idle,
// and every agent.message is forwarded to Telegram exactly once. The
// scheduler's tick re-runs syncReplies() as a backstop for replies that
// finish after the webhook's function has stopped.

import { anthropic, getAgentIds, type AgentIds } from "./agents";
import { BUDGETS } from "./areas";
import { claimKey, getSetting, releaseKey, setSetting } from "./db";
import { logMessage, notify, recentMessages, unseenOutbound } from "./store";
import { sendMessage, sendTyping } from "./telegram";
import { nowLabel, todayKey } from "./time";

const STREAM_DEADLINE_MS = 270_000; // keep under the route's maxDuration (300s)

type ChatSession = { date: string; id: string };

async function getChatSession(ids: AgentIds): Promise<{ sessionId: string; isNew: boolean }> {
  const today = todayKey();
  const current = await getSetting<ChatSession>("chat_session");
  if (current?.date === today) return { sessionId: current.id, isNew: false };

  const session = await anthropic().beta.sessions.create({
    agent: ids.coordinatorId!,
    environment_id: ids.environmentId!,
    vault_ids: [ids.vaultId!],
    title: `Telegram · ${today}`,
    budget: { type: "limit", max_list_cost: { amount: BUDGETS.chatSessionPerDayCents, currency: "USD" } },
    metadata: { app: "jonny", kind: "chat", date: today },
  });
  await setSetting("chat_session", { date: today, id: session.id } satisfies ChatSession);
  return { sessionId: session.id, isNew: true };
}

/** A new day's session starts empty; hand it the tail of the recent conversation. */
async function recentContext(): Promise<string> {
  const msgs = (await recentMessages(12)).reverse().filter((m) => m.source === "user" || m.source === "chat");
  if (!msgs.length) return "";
  const lines = msgs.map((m) => `${m.direction === "in" ? "user" : "Jonny"}: ${m.text.slice(0, 400)}`);
  return `(Earlier conversation, for context only:\n${lines.join("\n")})\n\n`;
}

/** What the user was sent from outside this chat (check-ins, reminders) since they last wrote. */
async function sinceLastExchange(): Promise<string> {
  const msgs = await unseenOutbound();
  if (!msgs.length) return "";
  const lines = msgs.map((m) => `- ${m.source === "reminder" ? "reminder" : "check-in"}: ${m.text.slice(0, 600)}`);
  return `(Since your last exchange the user was sent:\n${lines.join("\n")})\n\n`;
}

function textOf(content: Array<{ type: string; text?: string }>): string {
  return content
    .filter((b) => b.type === "text" && b.text)
    .map((b) => b.text!.trim())
    .join("\n\n")
    .trim();
}

/** Forward one agent.message to Telegram, at most once across all callers. */
async function forwardAgentMessage(event: { id: string; content: Array<{ type: string; text?: string }> }): Promise<boolean> {
  const text = textOf(event.content);
  if (!text) return false;
  const key = `agent-msg:${event.id}`;
  if (!(await claimKey(key))) return false;
  try {
    await sendMessage(text);
    await logMessage({ direction: "out", source: "chat", text });
    return true;
  } catch (err) {
    await releaseKey(key); // let the next sync retry
    throw err;
  }
}

/** Forward any agent replies from the last few hours that haven't been sent yet. */
export async function syncReplies(sessionId: string): Promise<number> {
  const since = new Date(Date.now() - 3 * 3600_000).toISOString();
  const page = await anthropic().beta.sessions.events.list(sessionId, {
    types: ["agent.message"],
    order: "desc",
    limit: 25,
    "created_at[gte]": since,
  });
  let forwarded = 0;
  for (const event of [...page.data].reverse()) {
    if (event.type !== "agent.message") continue;
    if (await forwardAgentMessage(event)) forwarded++;
  }
  return forwarded;
}

export async function syncTodayReplies(): Promise<number> {
  const current = await getSetting<ChatSession>("chat_session");
  return current ? syncReplies(current.id) : 0;
}

async function messageProcessed(sessionId: string, eventId: string | undefined): Promise<boolean> {
  if (!eventId) return true;
  const page = await anthropic().beta.sessions.events.list(sessionId, {
    types: ["user.message"],
    order: "desc",
    limit: 10,
  });
  const mine = page.data.find((e) => e.id === eventId);
  return !mine || Boolean((mine as { processed_at?: string | null }).processed_at);
}

/** Handle a free-text message from the owner: run it through Jonny and relay the reply. */
export async function chatWithJonny(userText: string): Promise<void> {
  const outsideContext = await sinceLastExchange(); // read before logging this message
  await logMessage({ direction: "in", source: "user", text: userText });
  const ids = await getAgentIds();
  if (!ids?.coordinatorId || !ids.environmentId || !ids.vaultId) {
    await notify("I'm not set up yet: open the dashboard's Setup page and press “Sync agents”.", { source: "system" });
    return;
  }

  await sendTyping();
  const client = anthropic();
  const { sessionId, isNew } = await getChatSession(ids);
  const body = `${isNew ? await recentContext() : ""}${outsideContext}[${nowLabel()}] ${userText}`;

  // Stream first, then send, so no early events are missed.
  const stream = await client.beta.sessions.events.stream(sessionId);
  const timer = setTimeout(() => stream.controller.abort(), STREAM_DEADLINE_MS);
  try {
    let sentEventId: string | undefined;
    try {
      const sent = await client.beta.sessions.events.send(sessionId, {
        events: [{ type: "user.message", content: [{ type: "text", text: body }] }],
      });
      sentEventId = sent.data?.[0]?.id;
    } catch (err) {
      console.error("send to session failed", err);
      await notify(
        "I couldn't reach my agent just now (the day's chat budget may be used up). Try again shortly, or raise BUDGETS.chatSessionPerDayCents.",
        { source: "system" },
      );
      return;
    }

    for await (const event of stream) {
      if (event.type === "agent.message") {
        await forwardAgentMessage(event);
        await sendTyping();
      } else if (event.type === "session.status_idle") {
        if (event.stop_reason.type === "requires_action") continue;
        if (event.stop_reason.type === "budget_reached") {
          await notify("Today's chat budget is used up, so I've paused. It resets tomorrow (or raise BUDGETS.chatSessionPerDayCents).", {
            source: "system",
          });
          break;
        }
        // An idle that ends an *earlier* turn can arrive before ours has started.
        if (await messageProcessed(sessionId, sentEventId)) break;
      } else if (event.type === "session.status_terminated") {
        break;
      } else if (event.type === "session.error") {
        console.error("session error", JSON.stringify(event));
      }
    }
  } catch (err) {
    if (!stream.controller.signal.aborted) console.error("stream failed", err);
  } finally {
    clearTimeout(timer);
    stream.controller.abort();
  }

  // Anything the stream missed (or that arrives later) is picked up here or by the tick.
  await syncReplies(sessionId).catch((err) => console.error("syncReplies failed", err));
}
