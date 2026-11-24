// Managed Agents setup: one coordinator ("Jonny") plus one agent per life
// area, all reading and writing the organizer through this app's MCP server
// (/api/mcp). Scheduled check-ins are Managed Agents deployments.
//
// syncAgents() is idempotent: it creates what's missing and updates what
// exists (an update that changes an agent publishes a new version). IDs are
// stored in the settings table, never in code.

import Anthropic from "@anthropic-ai/sdk";
import { AREAS, BUDGETS, COORDINATOR, MODELS, type Area, type AreaId, type CheckIn } from "./areas";
import { mcpUrl, ownerName, requireEnv, timezone } from "./config";
import { getSetting, setSetting } from "./db";

let anthropicClient: Anthropic | null = null;
export function anthropic(): Anthropic {
  if (!anthropicClient) anthropicClient = new Anthropic(); // reads ANTHROPIC_API_KEY
  return anthropicClient;
}

export const MCP_SERVER_NAME = "life";

export type AgentIds = {
  environmentId?: string;
  vaultId?: string;
  credentialId?: string;
  credentialUrl?: string;
  credentialTokenHash?: string;
  areaAgents: Partial<Record<AreaId, string>>;
  coordinatorId?: string;
  deployments: Record<string, string>; // check-in key -> deployment id
  retiredDeployments?: string[];
  syncedAt?: string;
};

export async function getAgentIds(): Promise<AgentIds | null> {
  return getSetting<AgentIds>("agents");
}

// ---------------- prompts ----------------

function sharedRules(): string {
  const owner = ownerName();
  return `## Tools and data
Everything about ${owner}'s organised life lives behind the "${MCP_SERVER_NAME}" tools. Never invent data: read it with get_overview (or get_logs for longer history) before you summarise, plan, or answer questions about it.
- Time: ${owner} lives in ${timezone()}. get_overview returns the current local time. Pass date-times as local wall-clock "YYYY-MM-DDTHH:mm" and dates as "YYYY-MM-DD".
- Areas: work, projects, fitness, health, life. File everything under the best-fitting area.
- Use a reminder for anything time-bound (it arrives on Telegram with Done / Snooze buttons), a task for things to do without a fixed time, a goal for an outcome being worked towards, and a note for reference information.
- When you create or change something, say exactly what and when.

## How messages reach ${owner}
${owner} talks to you through Telegram.
- A message that starts with "[scheduled]" is an automated check-in and nobody is waiting on a reply. To tell ${owner} something, call send_telegram exactly once with the finished message. If there is genuinely nothing worth saying, send nothing.
- Any other message comes from ${owner} or from Jonny: answer it in your reply text. Do not call send_telegram for normal replies; your reply is delivered for you.

## Style
Telegram plain text: short lines, no markdown headings, tables or bold, at most a few emoji. Warm, direct and practical. Default to brief.`;
}

function coordinatorSystem(): string {
  const owner = ownerName();
  const roster = AREAS.map((a) => `- ${a.emoji} ${a.name}: ${a.blurb}`).join("\n");
  return `You are ${COORDINATOR.name}, ${owner}'s chief of staff for organising life. ${owner} talks to you on Telegram. You keep the whole picture across five areas, each run by a specialist agent you can delegate to:
${roster}

## Doing vs delegating
- Handle quick captures and questions yourself, straight away: "remind me…", "add a task…", "log 8h sleep", "what's on today?".
- Delegate to an area's agent when the request needs that area's judgement or planning: a training plan, a project review, health trends, planning the work week. The agent cannot see this conversation, so give it a self-contained task: what ${owner} asked, the relevant constraints, and what to report back. Then pass on the result in your own words.
- For requests that span several areas, delegate each piece to its agent in parallel and combine the reports.
- Only for the weekly review and plan across all five areas, start a workflow run that has each area agent review and plan its own area in parallel. Do not start workflow runs for anything else.
- Messages may start with a note listing what ${owner} was sent since your last exchange (scheduled check-ins, reminders with their ids). Use it to understand replies such as "7h, ran 30 min, mood 4" or "done".

${sharedRules()}`;
}

function areaSystem(area: Area): string {
  const owner = ownerName();
  return `You are the ${area.name} agent in ${owner}'s life organiser, working with ${COORDINATOR.name}, the chief of staff who talks to ${owner} on Telegram.
${area.focus}
Your area id is "${area.id}". Stay within it: if something belongs to another area, mention it in your report rather than acting on it, unless you were explicitly asked to.
When ${COORDINATOR.name} delegates a task to you, do it and report back concisely: what you found, what you changed (with ids and times), and anything ${COORDINATOR.name} should ask ${owner}.

${sharedRules()}`;
}

/** The kickoff message for a scheduled run. */
export function scheduledTask(checkIn: CheckIn, area?: Area): string {
  const scope = area ? ` Start by calling get_overview with area "${area.id}".` : " Start by calling get_overview.";
  return `[scheduled] ${checkIn.label}. ${checkIn.task}${scope}`;
}

// ---------------- sync ----------------

const lifeTools = [
  {
    type: "mcp_toolset" as const,
    mcp_server_name: MCP_SERVER_NAME,
    // This is our own first-party server and scheduled runs have no one
    // attached to approve calls, so tools run without confirmation.
    default_config: { permission_policy: { type: "always_allow" as const } },
  },
];

async function sha256(text: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return Array.from(new Uint8Array(digest), (b) => b.toString(16).padStart(2, "0")).join("");
}

function budget(cents: string) {
  return { type: "limit" as const, max_list_cost: { amount: cents, currency: "USD" as const } };
}

export async function syncAgents(): Promise<string[]> {
  const client = anthropic();
  const log: string[] = [];
  const url = mcpUrl();
  if (!url) throw new Error("No public URL: set APP_URL (or deploy on Vercel) before syncing agents.");
  const mcpToken = requireEnv("MCP_TOKEN");

  const ids: AgentIds = (await getAgentIds()) ?? { areaAgents: {}, deployments: {} };
  ids.areaAgents ??= {};
  ids.deployments ??= {};
  const save = () => setSetting("agents", ids);

  // 1. Environment (cloud). The agents only use MCP tools, so no sandbox
  //    egress is opened beyond reaching MCP servers.
  if (!ids.environmentId) {
    const env = await client.beta.environments.create({
      name: "jonny-life-hq",
      config: { type: "cloud", networking: { type: "limited", allow_mcp_servers: true, allow_package_managers: false } },
    });
    ids.environmentId = env.id;
    log.push(`created environment ${env.id}`);
    await save();
  }

  // 2. Vault + bearer credential for our MCP server.
  if (!ids.vaultId) {
    const vault = await client.beta.vaults.create({ display_name: "Jonny Life HQ" });
    ids.vaultId = vault.id;
    log.push(`created vault ${vault.id}`);
    await save();
  }
  const tokenHash = await sha256(mcpToken);
  if (ids.credentialId && ids.credentialUrl !== url) {
    await client.beta.vaults.credentials.archive(ids.credentialId, { vault_id: ids.vaultId });
    log.push(`archived credential for old URL ${ids.credentialUrl}`);
    ids.credentialId = undefined;
  }
  if (!ids.credentialId) {
    const cred = await client.beta.vaults.credentials.create(ids.vaultId, {
      display_name: "Jonny MCP token",
      auth: { type: "static_bearer", mcp_server_url: url, token: mcpToken },
    });
    Object.assign(ids, { credentialId: cred.id, credentialUrl: url, credentialTokenHash: tokenHash });
    log.push(`created MCP credential for ${url}`);
    await save();
  } else if (ids.credentialTokenHash !== tokenHash) {
    await client.beta.vaults.credentials.update(ids.credentialId, {
      vault_id: ids.vaultId,
      auth: { type: "static_bearer", token: mcpToken },
    });
    ids.credentialTokenHash = tokenHash;
    log.push("rotated MCP credential token");
    await save();
  }

  const mcpServers = [{ type: "url" as const, name: MCP_SERVER_NAME, url }];

  // 3. Area agents.
  for (const area of AREAS) {
    const config = {
      name: `${area.name} agent`,
      description: area.agentDescription,
      model: { id: MODELS.area.id, effort: MODELS.area.effort },
      system: areaSystem(area),
      mcp_servers: mcpServers,
      tools: lifeTools,
      metadata: { app: "jonny", role: "area", area: area.id },
    };
    const existing = ids.areaAgents[area.id];
    if (existing) {
      const agent = await client.beta.agents.update(existing, config);
      log.push(`${area.name} agent → v${agent.version}`);
    } else {
      const agent = await client.beta.agents.create(config);
      ids.areaAgents[area.id] = agent.id;
      log.push(`created ${area.name} agent ${agent.id}`);
      await save();
    }
  }

  // 4. Coordinator with the area agents as its roster.
  const roster = AREAS.map((a) => ids.areaAgents[a.id]!).filter(Boolean);
  const coordinatorConfig = {
    name: COORDINATOR.name,
    description: "Chief of staff: talks to the user on Telegram and coordinates the area agents.",
    model: { id: MODELS.coordinator.id, effort: MODELS.coordinator.effort },
    system: coordinatorSystem(),
    mcp_servers: mcpServers,
    tools: lifeTools,
    multiagent: {
      type: "multiagent_20261001" as const,
      subagents: { type: "enabled" as const, predefined_agents: roster, inline_agents: { type: "disabled" as const } },
      // Workflow runs only for the weekly cross-area review (see system prompt).
      workflows: { type: "enabled" as const, predefined_agents: roster, inline_agents: { type: "disabled" as const } },
    },
    metadata: { app: "jonny", role: "coordinator" },
  };
  if (ids.coordinatorId) {
    const agent = await client.beta.agents.update(ids.coordinatorId, coordinatorConfig);
    log.push(`${COORDINATOR.name} → v${agent.version}`);
  } else {
    const agent = await client.beta.agents.create(coordinatorConfig);
    ids.coordinatorId = agent.id;
    log.push(`created ${COORDINATOR.name} ${agent.id}`);
    await save();
  }

  // 5. Scheduled check-ins (one deployment each).
  const wanted: { checkIn: CheckIn; agentId: string; area?: Area }[] = [
    ...COORDINATOR.checkIns.map((c) => ({ checkIn: c, agentId: ids.coordinatorId! })),
    ...AREAS.flatMap((a) => a.checkIns.map((c) => ({ checkIn: c, agentId: ids.areaAgents[a.id]!, area: a }))),
  ];
  for (const { checkIn, agentId, area } of wanted) {
    const params = {
      name: `${area ? area.name : COORDINATOR.name} · ${checkIn.label}`,
      agent: agentId, // bare id = latest agent version, re-resolved on every sync
      environment_id: ids.environmentId!,
      vault_ids: [ids.vaultId!],
      initial_events: [{ type: "user.message" as const, content: [{ type: "text" as const, text: scheduledTask(checkIn, area) }] }],
      schedule: { type: "cron" as const, expression: checkIn.cron, timezone: timezone() },
      budget: budget(checkIn.budgetCents ?? BUDGETS.scheduledRunCents),
      metadata: { app: "jonny", checkin: checkIn.key },
    };
    const existing = ids.deployments[checkIn.key];
    if (existing) {
      await client.beta.deployments.update(existing, params);
      log.push(`updated schedule "${params.name}"`);
    } else {
      const dep = await client.beta.deployments.create(params);
      ids.deployments[checkIn.key] = dep.id;
      log.push(`created schedule "${params.name}"`);
      await save();
    }
  }

  // Check-ins removed from areas.ts: pause (reversible), never archive.
  const wantedKeys = new Set(wanted.map((w) => w.checkIn.key));
  for (const [key, depId] of Object.entries(ids.deployments)) {
    if (wantedKeys.has(key)) continue;
    await client.beta.deployments.pause(depId).catch(() => {});
    delete ids.deployments[key];
    ids.retiredDeployments = [...(ids.retiredDeployments ?? []), depId];
    log.push(`paused removed schedule ${key} (${depId})`);
  }

  ids.syncedAt = new Date().toISOString();
  await save();
  return log;
}

export type ScheduleStatus = {
  key: string;
  name: string;
  status: string;
  nextRun?: string;
  deploymentId: string;
};

export async function scheduleStatuses(ids: AgentIds): Promise<ScheduleStatus[]> {
  const client = anthropic();
  const out: ScheduleStatus[] = [];
  for (const [key, id] of Object.entries(ids.deployments ?? {})) {
    try {
      const d = await client.beta.deployments.retrieve(id);
      out.push({
        key,
        name: d.name,
        status: d.status,
        nextRun: d.schedule?.upcoming_runs_at?.[0],
        deploymentId: id,
      });
    } catch {
      out.push({ key, name: key, status: "unreachable", deploymentId: id });
    }
  }
  return out;
}

export async function runCheckInNow(key: string): Promise<string> {
  const ids = await getAgentIds();
  const id = ids?.deployments?.[key];
  if (!id) throw new Error(`no deployment for ${key}`);
  const run = await anthropic().beta.deployments.run(id);
  return run.session_id ?? run.id;
}
