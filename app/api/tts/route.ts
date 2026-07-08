import { NextRequest, NextResponse } from "next/server";
import { brainFetch } from "@/lib/brain";

export const maxDuration = 60;

// Streams the Mac's Kokoro voice back to the browser (same voice as the
// Mac). If it fails, the client falls back to the browser's built-in voice.

export async function POST(request: NextRequest) {
  const body = await request.json().catch(() => null);
  const text: string = typeof body?.text === "string" ? body.text : "";
  if (!text) return NextResponse.json({ error: "bad request" }, { status: 400 });

  try {
    const res = await brainFetch("/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ text }),
    });
    if (!res.ok) {
      return NextResponse.json({ error: "tts unavailable" }, { status: res.status });
    }
    return new Response(res.body, {
      headers: {
        "content-type": res.headers.get("content-type") || "audio/mpeg",
      },
    });
  } catch {
    return NextResponse.json({ error: "tts unreachable" }, { status: 502 });
  }
}
