import { createMcpHandler } from "mcp-handler";
import { bearerMatches } from "@/lib/config";
import { registerLifeTools } from "@/lib/mcp-tools";

// MCP server (Streamable HTTP) for the hosted agents — and later Jarvis.
// Auth: "Authorization: Bearer $MCP_TOKEN" (the middleware skips this path).

export const runtime = "nodejs";
export const maxDuration = 60;

const mcp = createMcpHandler(registerLifeTools, {
  serverInfo: { name: "jonny-life", version: "1.0.0" },
});

async function handler(request: Request): Promise<Response> {
  if (!bearerMatches(request.headers.get("authorization"), process.env.MCP_TOKEN)) {
    return new Response(JSON.stringify({ error: "unauthorized" }), {
      status: 401,
      headers: { "Content-Type": "application/json", "WWW-Authenticate": 'Bearer realm="jonny"' },
    });
  }
  return mcp(request);
}

export { handler as GET, handler as POST, handler as DELETE };
