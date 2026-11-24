import { NextRequest, NextResponse } from "next/server";
import { lifePost } from "@/lib/life";

// "Ask Jarvis" from the dashboard. A plain route (not a server action):
// Next runs server actions one at a time, so a several-minute question
// (Jarvis bringing in area agents) would freeze every other button.
// Protected by the password cookie via middleware.

export const maxDuration = 300;

export async function POST(request: NextRequest) {
  const body = await request.json().catch(() => null);
  const text = typeof body?.text === "string" ? body.text.trim() : "";
  if (!text) return NextResponse.json({ error: "empty message" }, { status: 400 });
  const res = await lifePost<{ text: string }>("/life/ask", { text }, 280_000);
  return res.ok
    ? NextResponse.json({ text: res.data.text })
    : NextResponse.json({ error: res.error }, { status: 502 });
}
