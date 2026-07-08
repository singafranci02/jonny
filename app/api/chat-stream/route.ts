import { NextRequest, NextResponse } from "next/server";
import { brainFetch } from "@/lib/brain";

export const maxDuration = 120;

// Proxy the Mac's SSE stream: text deltas + per-sentence MP3 audio arrive
// while the model is still generating.

export async function POST(request: NextRequest) {
  const body = await request.json().catch(() => null);
  const message: string = typeof body?.message === "string" ? body.message : "";
  if (!message || message.length > 8000) {
    return NextResponse.json({ error: "bad request" }, { status: 400 });
  }
  try {
    const res = await brainFetch("/chat_stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
    if (!res.ok || !res.body) {
      return NextResponse.json({ error: "brain offline" }, { status: 502 });
    }
    return new Response(res.body, {
      headers: {
        "content-type": "text/event-stream",
        "cache-control": "no-cache",
      },
    });
  } catch {
    return NextResponse.json(
      { error: "can't reach the Mac — is it on and the tunnel running?" },
      { status: 502 },
    );
  }
}
