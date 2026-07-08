// The web app is a thin face onto the Mac's Jarvis brain. These serverless
// routes proxy to it over a Cloudflare tunnel, holding the shared token so
// the browser never sees it.
//
// MAC_BRAIN_URL   — the tunnel URL, e.g. https://jarvis.yourname.com
// JARVIS_TOKEN    — same long random string set in the Mac's .env

export function brainConfig(): { url: string; token: string } | null {
  let url = process.env.MAC_BRAIN_URL?.trim();
  const token = process.env.JARVIS_TOKEN?.trim();
  if (!url || !token) return null;
  // tolerate a URL entered without a scheme or with a trailing slash
  if (!/^https?:\/\//i.test(url)) url = `https://${url}`;
  return { url: url.replace(/\/+$/, ""), token };
}

export async function brainFetch(
  path: string,
  init: RequestInit = {},
): Promise<Response> {
  const cfg = brainConfig();
  if (!cfg) {
    return new Response(JSON.stringify({ error: "brain not configured" }), {
      status: 503,
      headers: { "content-type": "application/json" },
    });
  }
  return fetch(`${cfg.url}${path}`, {
    ...init,
    headers: {
      ...(init.headers || {}),
      Authorization: `Bearer ${cfg.token}`,
      // skip ngrok's free-tier browser interstitial (harmless on other tunnels)
      "ngrok-skip-browser-warning": "1",
    },
    // the Mac can take a few seconds (local model); allow generous time
    signal: AbortSignal.timeout(120_000),
  });
}
