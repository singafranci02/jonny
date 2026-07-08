import { NextRequest, NextResponse } from "next/server";
import { brainFetch } from "@/lib/brain";

export const maxDuration = 60;

// Forward recorded browser audio to the Mac's Whisper (much more accurate
// than the browser's built-in speech recognition).

export async function POST(request: NextRequest) {
  const audio = await request.arrayBuffer();
  if (!audio.byteLength || audio.byteLength > 25_000_000) {
    return NextResponse.json({ error: "bad audio" }, { status: 400 });
  }
  try {
    const res = await brainFetch("/stt", {
      method: "POST",
      headers: { "Content-Type": "application/octet-stream" },
      body: audio,
    });
    if (!res.ok) return NextResponse.json({ error: "stt failed" }, { status: 502 });
    return NextResponse.json(await res.json());
  } catch {
    return NextResponse.json({ error: "unreachable" }, { status: 502 });
  }
}
