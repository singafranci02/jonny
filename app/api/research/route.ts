import { NextRequest, NextResponse } from "next/server";
import { brainFetch } from "@/lib/brain";

// Poll a background research job running on the Mac.

export async function GET(request: NextRequest) {
  const jobId = request.nextUrl.searchParams.get("job");
  if (!jobId) return NextResponse.json({ error: "no job id" }, { status: 400 });
  try {
    const res = await brainFetch(`/research/${encodeURIComponent(jobId)}`);
    if (!res.ok) return NextResponse.json({ error: "unavailable" }, { status: 502 });
    return NextResponse.json(await res.json());
  } catch {
    return NextResponse.json({ error: "unreachable" }, { status: 502 });
  }
}
