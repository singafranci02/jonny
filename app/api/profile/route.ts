import { NextRequest, NextResponse } from "next/server";
import { brainFetch } from "@/lib/brain";

// Read / write the "About Me" profile stored on the Mac.

export async function GET() {
  try {
    const res = await brainFetch("/profile");
    if (!res.ok) return NextResponse.json({ error: "unavailable" }, { status: 502 });
    return NextResponse.json(await res.json());
  } catch {
    return NextResponse.json({ error: "unreachable" }, { status: 502 });
  }
}

export async function PUT(request: NextRequest) {
  const body = await request.json().catch(() => null);
  const content: string = typeof body?.content === "string" ? body.content : "";
  if (content.length > 20000) {
    return NextResponse.json({ error: "too long" }, { status: 400 });
  }
  try {
    const res = await brainFetch("/profile", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content }),
    });
    if (!res.ok) return NextResponse.json({ error: "save failed" }, { status: 502 });
    return NextResponse.json({ ok: true });
  } catch {
    return NextResponse.json({ error: "unreachable" }, { status: 502 });
  }
}
