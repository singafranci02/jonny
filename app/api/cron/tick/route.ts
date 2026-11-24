import { NextRequest, NextResponse } from "next/server";
import { syncTodayReplies } from "@/lib/chat";
import { bearerMatches } from "@/lib/config";
import { setSetting } from "@/lib/db";
import { processDueReminders } from "@/lib/store";

// Called every few minutes by an external scheduler (GitHub Actions by
// default, see .github/workflows/tick.yml) with "Authorization: Bearer
// $CRON_SECRET". Sends due reminders and relays any late agent replies.
// No model calls happen here.

export const runtime = "nodejs";
export const maxDuration = 60;

export async function GET(request: NextRequest) {
  if (!bearerMatches(request.headers.get("authorization"), process.env.CRON_SECRET)) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  const reminders = await processDueReminders();
  let replies = 0;
  try {
    replies = await syncTodayReplies();
  } catch (err) {
    console.error("reply sync failed", err);
  }
  const result = { at: new Date().toISOString(), ...reminders, replies };
  await setSetting("last_tick", result);
  return NextResponse.json(result);
}

export const POST = GET;
