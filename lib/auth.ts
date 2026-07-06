// Session cookie = HMAC-SHA256("jonny-session-v1", DASHBOARD_PASSWORD).
// Web Crypto only, so it runs in both the Edge middleware and Node routes.

const SESSION_PAYLOAD = "jonny-session-v1";
export const COOKIE_NAME = "jonny_auth";

async function hmac(payload: string, secret: string): Promise<string> {
  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const sig = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(payload));
  return Array.from(new Uint8Array(sig))
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}

export async function sessionToken(): Promise<string> {
  const password = process.env.DASHBOARD_PASSWORD;
  if (!password) throw new Error("DASHBOARD_PASSWORD is not set");
  return hmac(SESSION_PAYLOAD, password);
}

export async function isValidSession(token: string | undefined): Promise<boolean> {
  if (!token || !process.env.DASHBOARD_PASSWORD) return false;
  const expected = await sessionToken();
  if (token.length !== expected.length) return false;
  // constant-time compare
  let diff = 0;
  for (let i = 0; i < expected.length; i++) {
    diff |= token.charCodeAt(i) ^ expected.charCodeAt(i);
  }
  return diff === 0;
}
