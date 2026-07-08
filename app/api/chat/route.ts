import { NextRequest, NextResponse } from "next/server";
import { brainFetch } from "@/lib/brain";

// Middleware gates this behind the password cookie; it forwards the message
// to the Mac brain (same local model, tools, research, memory, profile).

export async function POST(request: NextRequest) {
  const body = await request.json().catch(() => null);
  const message: string = typeof body?.message === "string" ? body.message : "";
  if (!message || message.length > 8000) {
    return NextResponse.json({ error: "bad request" }, { status: 400 });
  }

  try {
    const res = await brainFetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message }),
    });
    if (!res.ok) {
      const detail = res.status === 503 ? "brain offline" : `brain error ${res.status}`;
      return NextResponse.json({ error: detail }, { status: 502 });
    }
    const data = await res.json();
    return NextResponse.json({
      text: data.text || "Hmm, I came up empty on that one.",
      researchJobId: data.research_job_id ?? null,
    });
  } catch {
    return NextResponse.json(
      { error: "can't reach the Mac — is it on and the tunnel running?" },
      { status: 502 },
    );
  }
}
