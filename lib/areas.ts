// The life areas, the agent in charge of each, and the scheduled check-ins.
// Edit this file, redeploy, then press "Sync agents & schedules" on /setup to
// push the changes to the hosted agents (each sync publishes new versions).
//
// Cadence: "one voice". Only Jonny messages on a schedule (morning brief,
// evening check-in, Sunday review). Area agents work behind him: he delegates
// to them, and in the Sunday review each one reviews and plans its own area.
// To give an area agent a slot of its own, add a check-in to its `checkIns`,
// e.g. { key: "fitness-midweek", label: "Wed 18:00 · midweek check",
//        cron: "0 18 * * 3", task: "..." }.

export const AREA_IDS = ["work", "projects", "fitness", "health", "life"] as const;
export type AreaId = (typeof AREA_IDS)[number];

export function isAreaId(value: string): value is AreaId {
  return (AREA_IDS as readonly string[]).includes(value);
}

// Jonny on Opus 5.5 (the judgement you talk to); area specialists on Sonnet
// 5.5. Change here and re-sync to switch.
export const MODELS = {
  coordinator: { id: "claude-opus-5-5", effort: "medium" },
  area: { id: "claude-sonnet-5-5", effort: "medium" },
} as const;

// Hard spend caps (USD cents at list price), enforced by the platform.
export const BUDGETS = {
  chatSessionPerDayCents: "300", // the Telegram chat session, per local day
  scheduledRunCents: "100", // default per scheduled run
};

export type CheckIn = {
  key: string; // stable id; renaming it creates a new schedule
  label: string; // shown on the dashboard and setup page
  cron: string; // minute hour day-of-month month day-of-week, in TIMEZONE
  task: string; // what the agent should do on this run
  budgetCents?: string; // overrides BUDGETS.scheduledRunCents
};

export type Area = {
  id: AreaId;
  name: string;
  emoji: string;
  blurb: string;
  agentDescription: string; // Jonny reads this to decide whom to delegate to
  focus: string; // the area agent's job, in its own system prompt
  checkIns: CheckIn[];
};

export const AREAS: Area[] = [
  {
    id: "work",
    name: "Work",
    emoji: "💼",
    blurb: "Job, meetings, deadlines, career",
    agentDescription:
      "Owns the Work area: job tasks, deadlines, meeting prep, career goals. Give it a work question or planning task; it reads and updates work items and reminders and reports back.",
    focus:
      "You run the Work area. Keep work tasks prioritised and deadlines visible, help plan the week, and turn vague intentions into concrete next actions with dates. Flag anything overdue or at risk early.",
    checkIns: [],
  },
  {
    id: "projects",
    name: "Projects",
    emoji: "🚀",
    blurb: "Side projects and personal builds",
    agentDescription:
      "Owns the Projects area: side projects, their goals, milestones and next actions. Give it a project question or planning task; it reads and updates project items and reports back.",
    focus:
      "You run the Projects area. For each active project keep a clear goal, the next milestone with a date, and one concrete next action. Notice stalled projects and help either restart them or consciously park them.",
    checkIns: [],
  },
  {
    id: "fitness",
    name: "Fitness",
    emoji: "🏋️",
    blurb: "Training, activity, sport",
    agentDescription:
      "Owns the Fitness area: training plans, workouts, activity logs and fitness goals. Give it a training question, a plan to build, or a workout to log; it reads and writes fitness data and reports back.",
    focus:
      "You are the fitness coach. Track workouts and activity from the logs, keep the training plan realistic, and encourage consistency without guilt. Log workouts as metric \"workout\" with the duration in minutes as the value and the details in the note.",
    checkIns: [],
  },
  {
    id: "health",
    name: "Health",
    emoji: "🩺",
    blurb: "Sleep, food, meds, appointments, mood",
    agentDescription:
      "Owns the Health area: sleep, nutrition, medication, appointments, mood and health metrics. Give it a health question or something to log; it reads and writes health data and reports back. It is not a doctor.",
    focus:
      "You look after the Health area: sleep, nutrition, hydration, medication, appointments, mood and body metrics. Spot trends in the logs and keep appointments and refills from slipping. Use the metrics \"sleep_hours\", \"mood\" (1-5) and \"energy\" (1-5) consistently. You are not a doctor: for symptoms or medical decisions, suggest seeing a professional.",
    checkIns: [],
  },
  {
    id: "life",
    name: "Life",
    emoji: "🏡",
    blurb: "Admin, money, home, people, errands",
    agentDescription:
      "Owns the Life area: personal admin, bills and money, home, errands, friends and family. Give it an admin or personal-life task; it reads and updates life items and reminders and reports back.",
    focus:
      "You run the Life area: personal admin, bills and finances, home, errands, birthdays, friends and family. Keep recurring obligations on reminders so nothing is forgotten, and batch small errands sensibly.",
    checkIns: [],
  },
];

export const COORDINATOR = {
  name: "Jonny",
  checkIns: [
    {
      key: "jonny-morning-brief",
      label: "Daily 07:30 · morning brief",
      cron: "30 7 * * *",
      task:
        "Morning brief. One compact message: today's reminders and deadlines grouped by area (with the area emoji), anything overdue, and the top 3 things to focus on today. On Mondays, also list this week's deadlines. If scheduler_last_tick in the overview is missing or more than 60 minutes old, add one line warning that reminders may be delayed and to check the Setup page.",
    },
    {
      key: "jonny-evening-checkin",
      label: "Daily 21:00 · evening check-in",
      cron: "0 21 * * *",
      task:
        "Evening check-in, at most four short lines. Ask how today went and invite a one-line reply covering whatever isn't logged yet today: sleep last night (hours), training today, mood and energy (1-5). Mention tomorrow's first reminder or deadline if there is one. If everything is already logged, just say good night with tomorrow's first item.",
    },
    {
      key: "jonny-weekly-review",
      label: "Sun 19:00 · weekly review & plan",
      cron: "0 19 * * 0",
      budgetCents: "250",
      task:
        "Weekly review and plan. Start a workflow run in which each area agent reviews its own area for the past 7 days and plans the coming week, creating the reminders and tasks that are clearly needed (training sessions, deadlines, bills, appointments). Then send one combined message: for each area, one or two lines on wins, what slipped and next week's focus, followed by the reminders that were created.",
    },
  ] satisfies CheckIn[],
};

export function areaById(id: string): Area | undefined {
  return AREAS.find((a) => a.id === id);
}

export function areaLabel(id: string): string {
  const a = areaById(id);
  return a ? `${a.emoji} ${a.name}` : id;
}
