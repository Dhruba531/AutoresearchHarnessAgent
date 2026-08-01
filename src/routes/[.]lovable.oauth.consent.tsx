// THE OAUTH CONSENT SCREEN — the "Allow <app> to access your account?" page.
//
// This is the human half of the MCP authentication story documented in
// `src/lib/mcp/index.ts`. When an AI client (Claude, ChatGPT) wants to call
// this app's MCP tools, the user is sent here to approve it. Nothing in
// `src/lib/mcp/` works without this page.
//
// The square brackets in the filename escape the leading dot so the URL can be
// `/.lovable/oauth/consent` — see `src/routes/mcp.ts` for that convention.
//
// This route is also the best example in the codebase of a `loader`: data is
// fetched BEFORE the component renders, so `Route.useLoaderData()` below never
// has to handle a loading state.

import { createFileRoute, redirect } from "@tanstack/react-router";
import { useState } from "react";
import { supabase } from "@/integrations/supabase/client";

// OAuth consent screen for the MCP server. Supabase's authorization server
// redirects the user here (via /.lovable/oauth/consent) to approve or deny an
// MCP client (e.g. Claude, ChatGPT) connecting to this app.

// The Supabase JS auth.oauth namespace is beta; type a minimal shim locally
// rather than depending on unstable exports.
//
// A sound instinct worth recognising: rather than importing types from an
// unstable API surface, this declares the minimum shape it actually uses. A
// breaking change upstream then surfaces here as one obvious mismatch instead
// of cascading through the file. The cost is that these types are asserted, not
// verified — if the library changes shape, TypeScript will not notice.
interface AuthorizationDetails {
  client?: { name?: string | null; logo_uri?: string | null } | null;
  redirect_url?: string | null;
  redirect_to?: string | null;
  scopes?: string[] | null;
}
interface AuthorizationResult {
  redirect_url?: string | null;
  redirect_to?: string | null;
}
interface OAuthAuthClient {
  getAuthorizationDetails(id: string): Promise<{
    data: AuthorizationDetails | null;
    error: { message: string } | null;
  }>;
  approveAuthorization(id: string): Promise<{
    data: AuthorizationResult | null;
    error: { message: string } | null;
  }>;
  denyAuthorization(id: string): Promise<{
    data: AuthorizationResult | null;
    error: { message: string } | null;
  }>;
}
// The double cast (`as unknown as X`) is how you force a conversion TypeScript
// would otherwise reject. Necessary because `supabase.auth` has no declared
// `oauth` property in the current typings. Isolating it in one function means
// exactly one unchecked assertion in the file, rather than one per call site.
function oauthClient(): OAuthAuthClient {
  return (supabase.auth as unknown as { oauth: OAuthAuthClient }).oauth;
}

export const Route = createFileRoute("/.lovable/oauth/consent")({
  // Browser-only: the Supabase client reads its session from localStorage,
  // which is absent on the SSR pass.
  ssr: false,
  // The authorization server passes `?authorization_id=<id>`, which identifies
  // the pending request. Defaults to "" rather than undefined so the type stays
  // a plain string; the guard below rejects the empty case.
  validateSearch: (s: Record<string, unknown>) => ({
    authorization_id: typeof s.authorization_id === "string" ? s.authorization_id : "",
  }),
  beforeLoad: async ({ search, location }) => {
    // Without an id there is nothing to approve — most likely someone opened
    // this URL directly.
    if (!search.authorization_id) throw new Error("Missing authorization_id");
    // The user must be signed in before they can grant access on their own
    // behalf. Note the redirect preserves the FULL url including the query
    // string, so `authorization_id` survives the round trip through sign-in —
    // without `location.searchStr` the user would return here with no id and
    // hit the error above.
    const { data } = await supabase.auth.getSession();
    if (!data.session) {
      const next = location.pathname + location.searchStr;
      throw redirect({ to: "/auth", search: { redirect: next } });
    }
  },
  // A LOADER runs after `beforeLoad` and before the component renders. Its
  // return value is available synchronously via `Route.useLoaderData()`, which
  // is why `Consent` below has no loading state to handle — a genuinely
  // different model from the `useQuery` hooks used elsewhere in this app.
  loader: async ({ location }) => {
    const authorizationId =
      new URLSearchParams(location.search).get("authorization_id") ?? "";
    // Ask the authorization server what is being requested: which client, and
    // which scopes.
    const { data, error } = await oauthClient().getAuthorizationDetails(authorizationId);
    if (error) throw new Error(error.message);
    // AUTO-APPROVAL PATH. A redirect with no client details means the server
    // has already decided — typically a previously-granted consent being
    // re-confirmed. Showing a consent screen for a decision already made would
    // be pointless friction, so this bounces straight through.
    //
    // `redirect({ href })` (rather than `to`) performs a full-page navigation
    // to an EXTERNAL url, which is required here: the target belongs to the
    // authorization server, not to this app's route tree.
    const immediate = data?.redirect_url ?? data?.redirect_to;
    if (immediate && !data?.client) throw redirect({ href: immediate });
    return data;
  },
  component: Consent,
  errorComponent: ({ error }) => (
    <main className="mx-auto max-w-md p-8 font-mono text-sm">
      Could not load this authorization request:{" "}
      {String((error as Error)?.message ?? error)}
    </main>
  ),
});

function Consent() {
  // Data from the loader, already resolved — no `isLoading`, no null check.
  const details = Route.useLoaderData();
  const { authorization_id } = Route.useSearch();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  /**
   * Approve or deny, then hand control back to the authorization server.
   *
   * One function for both outcomes rather than two near-identical ones: only
   * the API call differs, and every subsequent step — error handling, reading
   * the redirect, navigating — is shared.
   */
  async function decide(approve: boolean) {
    setBusy(true);
    setError(null);
    const client = oauthClient();
    const { data, error } = approve
      ? await client.approveAuthorization(authorization_id)
      : await client.denyAuthorization(authorization_id);
    if (error) {
      setBusy(false);
      setError(error.message);
      return;
    }
    const target = data?.redirect_url ?? data?.redirect_to;
    if (!target) {
      setBusy(false);
      setError("No redirect returned by the authorization server.");
      return;
    }
    // A full-page navigation via `window.location`, NOT a router navigation.
    // The destination is the authorization server, outside this app entirely,
    // so client-side routing cannot reach it.
    //
    // Note `setBusy(false)` is deliberately not called on this path: the page
    // is about to be replaced, and clearing the flag would flash the buttons
    // back to their enabled state on the way out.
    window.location.href = target;
  }

  const name = details?.client?.name ?? "an app";

  return (
    <main className="mx-auto grid min-h-screen max-w-lg place-items-center p-8">
      <div className="w-full rounded-xl border border-border/60 bg-card/60 p-6 shadow-sm">
        <div className="mono-label mb-4">agentlab · mcp authorization</div>
        <h1 className="font-serif text-2xl text-foreground">
          Connect {name} to your account
        </h1>
        <p className="mt-3 text-sm text-muted-foreground">
          {name} is requesting access to call AgentLab MCP tools on your behalf.
          It will act as your signed-in user.
        </p>
        {error && (
          <p role="alert" className="mt-4 rounded-md border border-destructive/40 bg-destructive/10 p-3 font-mono text-xs text-destructive">
            {error}
          </p>
        )}
        <div className="mt-6 flex gap-3">
          <button
            disabled={busy}
            onClick={() => decide(true)}
            className="flex-1 rounded-md bg-primary py-2.5 font-mono text-xs uppercase tracking-wider text-primary-foreground disabled:opacity-60"
          >
            {busy ? "Working…" : "Approve"}
          </button>
          <button
            disabled={busy}
            onClick={() => decide(false)}
            className="flex-1 rounded-md border border-border py-2.5 font-mono text-xs uppercase tracking-wider text-muted-foreground hover:text-foreground disabled:opacity-60"
          >
            Deny
          </button>
        </div>
      </div>
    </main>
  );
}
