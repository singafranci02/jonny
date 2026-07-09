import { NextRequest, NextResponse } from "next/server";
import { brainFetch } from "@/lib/brain";

// Read-only view of the Jarvis workspace folder on the Mac.
// Listing: GET /api/workspace — file content: GET /api/workspace?name=x.md

export async function GET(request: NextRequest) {
  const name = request.nextUrl.searchParams.get("name");
  try {
    const res = await brainFetch(
      name ? `/workspace/file?name=${encodeURIComponent(name)}` : "/workspace",
    );
    if (!res.ok) return NextResponse.json({ error: "unavailable" }, { status: 502 });
    return NextResponse.json(await res.json());
  } catch {
    return NextResponse.json({ error: "unreachable" }, { status: 502 });
  }
}
