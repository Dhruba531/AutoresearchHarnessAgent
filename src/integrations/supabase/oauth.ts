// Provider OAuth sign-in, run through Supabase Auth.
//
// Supabase owns the whole handshake: it redirects the browser to the provider,
// handles the callback, and writes the session into its own store. There is no
// second auth system to stitch together afterwards — on return, the auth
// listener in `__root.tsx` already sees a signed-in user.
//
// Configure the provider and its redirect URL in the Supabase dashboard under
// Authentication → Providers, or the callback will be rejected.

import {
  supabase,
  isSupabaseConfigured,
  getSupabaseCredentials,
  SUPABASE_UNCONFIGURED_MESSAGE,
} from "./client";

/** Human-readable provider names, for error messages. */
const PROVIDER_LABEL: Record<string, string> = {
  google: "Google",
  apple: "Apple",
  azure: "Microsoft",
};

/**
 * Is `provider` actually configured on the Supabase project?
 *
 * WHY THIS EXISTS. `signInWithOAuth` does not validate anything — it builds
 * the /auth/v1/authorize URL and navigates the browser straight to it. When the
 * provider has no client ID and secret, Supabase answers that navigation with a
 * bare JSON body:
 *
 *   {"code":400,"error_code":"validation_failed",
 *    "msg":"Unsupported provider: missing OAuth secret"}
 *
 * The user is now staring at raw JSON on a supabase.co URL, outside the app
 * entirely, with no way back but the Back button. Checking first lets the
 * failure surface as an ordinary in-app error instead.
 *
 * RETURNS THREE-VALUED, deliberately: true, false, or null for "could not
 * determine". Only a definitive `false` blocks the redirect. A network blip or
 * an unexpected response shape must never stop a sign-in that would have
 * worked — a pre-flight check that produces false negatives is worse than no
 * check at all, because it breaks the working path to protect the broken one.
 */
async function isProviderEnabled(provider: string): Promise<boolean | null> {
  const creds = getSupabaseCredentials();
  if (!creds) return null;
  try {
    // /auth/v1/settings is unauthenticated but still requires the apikey
    // header. It reports `external` as a map of provider name -> enabled.
    const res = await fetch(`${creds.url}/auth/v1/settings`, {
      headers: { apikey: creds.key },
    });
    if (!res.ok) return null;
    const body: unknown = await res.json();
    const external = (body as { external?: unknown } | null)?.external;
    if (!external || typeof external !== "object") return null;
    // Compare against `true` explicitly rather than coercing: a provider that
    // is absent from the map yields undefined, which must read as "unknown"
    // only if the map itself was missing — here the map exists, so an absent
    // provider genuinely is not enabled.
    return (external as Record<string, unknown>)[provider] === true;
  } catch {
    // Offline, CORS, DNS — all "could not determine", never "disabled".
    return null;
  }
}

/**
 * `redirect_uri` — where to send the user once the provider approves them.
 * `extraParams` — extra query parameters for the provider's auth URL (scopes,
 * prompt behaviour, and so on). Both optional.
 */
type SignInOptions = {
  redirect_uri?: string;
  extraParams?: Record<string, string>;
};

export const oauth = {
  /**
   * Start an OAuth sign-in.
   *
   * The `provider` parameter is a literal union, so only these strings are
   * accepted — the compiler catches a typo like "gooogle" immediately.
   */
  signInWithOAuth: async (provider: "google" | "apple" | "azure", opts?: SignInOptions) => {
    // Returned in the same `{ redirected, error }` shape as a provider refusal,
    // so the caller's existing failure path renders it — no extra branch, and
    // no raw configuration error escaping to the console.
    if (!isSupabaseConfigured()) {
      return { redirected: false, error: new Error(SUPABASE_UNCONFIGURED_MESSAGE) };
    }

    // Only a definitive "not enabled" stops us; see isProviderEnabled.
    if ((await isProviderEnabled(provider)) === false) {
      const label = PROVIDER_LABEL[provider] ?? provider;
      return {
        redirected: false,
        error: new Error(
          `${label} sign-in is not enabled on this Supabase project. Add the ${label} provider's client ID and secret under Authentication → Providers, then try again.`,
        ),
      };
    }

    const { error } = await supabase.auth.signInWithOAuth({
      provider,
      options: {
        redirectTo: opts?.redirect_uri,
        queryParams: opts?.extraParams,
      },
    });

    // Failure: no navigation happened, so the caller stays on the page and
    // needs to surface the message.
    if (error) return { redirected: false, error };

    // Success means the browser is navigating away to the provider. This
    // JavaScript context is about to be discarded, so report the redirect and
    // let the caller leave its spinner running.
    return { redirected: true, error: undefined };
  },
};
