import { NextRequest, NextResponse } from "next/server";
import { COOKIE_NAME, isValidSession } from "@/lib/auth";

// These routes authenticate themselves instead of using the password cookie:
//   /api/telegram  - Telegram's secret-token header
//   /api/mcp       - bearer MCP_TOKEN (hosted agents, later Jarvis)
//   /api/cron      - bearer CRON_SECRET (scheduler)
const SELF_AUTHED = ["/login", "/api/login", "/api/telegram", "/api/mcp", "/api/cron"];

export async function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;
  if (SELF_AUTHED.some((p) => pathname === p || pathname.startsWith(`${p}/`))) {
    return NextResponse.next();
  }
  const ok = await isValidSession(request.cookies.get(COOKIE_NAME)?.value);
  if (ok) return NextResponse.next();

  if (pathname.startsWith("/api/")) {
    return NextResponse.json({ error: "unauthorized" }, { status: 401 });
  }
  const url = request.nextUrl.clone();
  url.pathname = "/login";
  return NextResponse.redirect(url);
}

export const config = {
  // protect everything except Next internals and static assets
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
