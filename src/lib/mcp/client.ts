// Small fetch helper for MCP tools. Reads env lazily inside handlers so the
// MCP entry stays import-safe (no top-level env reads, no I/O, no throws).
//
// "Import-safe" is the design constraint driving this file. Build tooling
// imports the MCP module to extract its manifest, in an environment that has no
// real configuration. If this module read `process.env` at the top level and
// threw on a missing value, that extraction would crash. Deferring every
// environment read into `getBase()` — called only when a tool actually runs —
// keeps importing the module completely inert.
//
// This is the same lazy-initialisation principle as the Proxy in
// `integrations/supabase/client.ts`, achieved more simply: just put the work
// inside a function.

function getBase(): string {
  // Two variable names tried in order, so the MCP server can point at a
  // different backend than the web app if needed, while falling back to the
  // app's own API base when it does not. `?? ""` yields a falsy value that the
  // caller checks, instead of `undefined` propagating into a URL as the string
  // "undefined".
  const base = process.env.AGENTLAB_API_BASE ?? process.env.VITE_API_BASE ?? "";
  // Strip one trailing slash. The regex `/\/$/` is: `\/` a literal slash,
  // anchored by `$` to the end of the string. Without this, a base ending in
  // "/" combined with a path starting with "/" produces a double slash
  // ("https://api.example.com//health"), which some servers 404 on.
  return base.replace(/\/$/, "");
}

/**
 * Make an authenticated request to the AgentLab FastAPI backend.
 *
 * Returns `unknown` rather than a specific type — an honest signature, since
 * this function cannot know what any given endpoint returns. Callers must
 * narrow or cast, which forces them to think about the shape rather than
 * trusting a fabricated one.
 *
 * `init: RequestInit = {}` defaults to an empty object so the common
 * `agentlabFetch("/health")` call needs no second argument.
 */
export async function agentlabFetch(path: string, init: RequestInit = {}): Promise<unknown> {
  const base = getBase();
  if (!base) {
    // Fail loudly and with a fix, not just a symptom. A message naming the
    // exact variable and where to set it saves far more time than
    // "fetch failed".
    throw new Error(
      "AGENTLAB_API_BASE is not configured. Set it in the Worker environment to point at the AgentLab FastAPI backend.",
    );
  }

  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    // Spread caller headers AFTER the default so callers can override
    // Content-Type when they need to. Later keys win in object spread.
    ...(init.headers as Record<string, string> | undefined),
  };

  // Attach the session cookie only if one is configured AND the caller did not
  // already supply their own. The `!headers["Cookie"]` guard is what makes this
  // a default rather than an override.
  const cookie = process.env.AGENTLAB_SESSION_COOKIE;
  if (cookie && !headers["Cookie"]) headers["Cookie"] = cookie;

  // Spread `init` first, then `headers` — so the merged headers object built
  // above wins over `init.headers`, rather than being silently replaced by it.
  // Reversing these two would discard the Content-Type and Cookie work entirely.
  const res = await fetch(`${base}${path}`, { ...init, headers });

  // Read the body as TEXT before checking `res.ok`, not after. This is
  // deliberate: on an error response the body usually holds the server's
  // explanation, and reading it first lets that detail go into the error
  // message below. Parsing as JSON up front would instead throw on any
  // non-JSON error page and lose the message entirely.
  const text = await res.text();

  if (!res.ok) {
    // `res.ok` is true only for 2xx statuses. `.slice(0, 300)` caps the excerpt
    // — some backends return entire HTML error pages, and a multi-kilobyte
    // error message helps nobody.
    throw new Error(`HTTP ${res.status}: ${text.slice(0, 300)}`);
  }

  // An empty body (a 204, typically) is success with nothing to return.
  if (!text) return null;

  // Try JSON, fall back to the raw string. The bodyless `catch {}` — no error
  // parameter — is valid modern syntax for "I do not care what went wrong".
  // Here that is appropriate: a parse failure simply means the endpoint
  // returned plain text, which is a legitimate response, not an error.
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}
