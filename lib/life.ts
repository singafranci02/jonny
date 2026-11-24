// Client for Life HQ on the Mac (jarvis/src/life/api.py), via the same
// password-gated, token-holding proxy as the voice face (lib/brain.ts).
// All the data and labels come from the Mac, already in the user's timezone.

import { brainConfig, brainFetch } from "./brain";

export type Area = { id: string; name: string; emoji: string; folder: string; blurb: string };
export type Reminder = { id: number; area: string; text: string; when: string; recurrence: string };
export type Awaiting = { id: number; area: string; text: string; when: string };
export type Item = {
  id: number;
  area: string;
  kind: "task" | "goal" | "note";
  title: string;
  priority: number;
  due_date: string | null;
};
export type LogEntry = {
  id: number;
  area: string;
  metric: string;
  value: number | null;
  unit: string | null;
  note: string | null;
  when: string;
};
export type Message = {
  id: number;
  direction: "in" | "out";
  source: string;
  area: string | null;
  text: string;
  when: string;
};
export type Status = {
  telegram: {
    configured: boolean;
    chat_id_set: boolean;
    telegram: string;
    bot: string | null;
    last_poll: string | null;
  };
  scheduler_last_sweep: string | null;
  scheduler_ok: boolean;
  folder: { root: string; exists: boolean; writable: boolean; error?: string; areas: Record<string, boolean> };
  full_disk_access_target: string;
  timezone: string;
  coordinator_model: string;
  area_model: string | null;
  checkins: { key: string; label: string; next: string }[];
};
export type Dashboard = {
  now: string;
  date_label: string;
  hour: number;
  areas: Area[];
  upcoming: Reminder[];
  awaiting: Awaiting[];
  items: Item[];
  logs: LogEntry[];
  messages: Message[];
  default_due: string;
  status: Status;
};

export type LifeResult<T> = { ok: true; data: T } | { ok: false; error: string };

async function call<T>(path: string, init: RequestInit = {}, timeoutMs?: number): Promise<LifeResult<T>> {
  if (!brainConfig()) {
    return { ok: false, error: "The website isn't connected to the Mac yet: set MAC_BRAIN_URL and JARVIS_TOKEN in Vercel." };
  }
  let res: Response;
  try {
    res = await brainFetch(path, init, timeoutMs);
  } catch {
    return { ok: false, error: "Jarvis's Mac isn't reachable right now (off, asleep, or the tunnel is down)." };
  }
  if (res.status === 401) return { ok: false, error: "The Mac rejected the token: JARVIS_TOKEN must match the Mac's .env." };
  const tunnelError = res.headers.get("ngrok-error-code");
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    // The tunnel answered, not Jarvis: ngrok's "endpoint offline" page (a 404)
    // or a gateway error while Jarvis is down. Both are HTML, not JSON.
    if (tunnelError || body === null) {
      return {
        ok: false,
        error: `Jarvis's Mac isn't reachable right now (${tunnelError ? `tunnel error ${tunnelError}` : `HTTP ${res.status}`}). It may be off, asleep, or restarting.`,
      };
    }
    // FastAPI's default 404: this Jarvis has no /life routes yet
    if (res.status === 404 && body.detail === "Not Found") {
      return { ok: false, error: "The Mac is running an older Jarvis without Life HQ: pull the latest code there and restart it." };
    }
    const detail =
      typeof body.detail === "string"
        ? body.detail
        : Array.isArray(body.detail) // validation errors: a list of {msg, loc}
          ? body.detail.map((d: { msg?: string }) => d?.msg).filter(Boolean).join("; ")
          : body.error;
    return { ok: false, error: detail || `The Mac answered ${res.status}.` };
  }
  if (body === null) return { ok: false, error: "The Mac sent an answer the website couldn't read." };
  return { ok: true, data: body as T };
}

export function lifeGet<T>(path: string): Promise<LifeResult<T>> {
  return call<T>(path, { cache: "no-store" });
}

export function lifePost<T>(path: string, body?: unknown, timeoutMs?: number): Promise<LifeResult<T>> {
  return call<T>(
    path,
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    },
    timeoutMs,
  );
}
