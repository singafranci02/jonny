import { NextResponse } from "next/server";
import { createHmac } from "crypto";
import { brainConfig } from "@/lib/brain";

// Mints a short-lived ticket the browser uses to open a WebSocket straight
// to the Mac (bypassing Vercel, which can't proxy long-lived sockets). The
// ticket is HMAC-signed with JARVIS_TOKEN and expires in 2 minutes, so the
// real token is never exposed to the browser. Middleware already checked the
// password cookie before we get here.

export async function GET() {
  const cfg = brainConfig();
  if (!cfg) {
    return NextResponse.json({ error: "brain not configured" }, { status: 503 });
  }
  const payloadB64 = Buffer.from(JSON.stringify({ exp: Date.now() / 1000 + 120 }))
    .toString("base64url");
  const sig = createHmac("sha256", cfg.token).update(payloadB64).digest("hex");
  const wsUrl = cfg.url.replace(/^http/, "ws") + "/ws";
  return NextResponse.json({ wsUrl, ticket: `${payloadB64}.${sig}` });
}
