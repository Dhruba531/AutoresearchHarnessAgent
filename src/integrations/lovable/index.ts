// Bridges the OAuth provider flow to Supabase's session store. Two separate
// auth systems have to be stitched together, and this adapter is the seam.

import { createLovableAuth } from "@lovable.dev/cloud-auth-js";
import { supabase } from "../supabase/client";
const lovableAuth = createLovableAuth();

/**
 * `redirect_uri` — where to send the user after the provider approves them.
 * `extraParams` — arbitrary extra query parameters for the provider's auth URL
 * (scopes, prompt behaviour, and so on). Both optional.
 */
type SignInOptions = {
  redirect_uri?: string;
  extraParams?: Record<string, string>;
};

// Exported as a nested object literal (`lovable.auth.signInWithOAuth`) rather
// than a flat function, so it mirrors the shape of `supabase.auth.*`. Calling
// code then reads consistently regardless of which system it is talking to.
export const lovable = {
  auth: {
    /**
     * Start an OAuth sign-in, then hand the resulting tokens to Supabase.
     *
     * The `provider` parameter is a literal union, so only these four strings
     * are accepted — the compiler catches a typo like "gooogle" immediately.
     */
    signInWithOAuth: async (provider: "google" | "apple" | "microsoft" | "lovable", opts?: SignInOptions) => {
      const result = await lovableAuth.signInWithOAuth(provider, {
        // `opts?.redirect_uri` — optional chaining, because `opts` itself may be
        // undefined. Yields undefined rather than throwing.
        redirect_uri: opts?.redirect_uri,
        extraParams: {
          // Spreading a possibly-undefined value is safe: `{...undefined}` is
          // simply `{}`. So this produces an empty object when no extras were
          // passed, rather than erroring.
          ...opts?.extraParams,
        },
      });

      // Three outcomes are possible, checked in order. The first two return
      // early, which is what leaves the happy path unindented at the bottom.

      // 1. The browser is navigating away to the provider's login page. Nothing
      //    more to do here — this JavaScript context is about to be discarded.
      if (result.redirected) {
        return result;
      }

      // 2. Lovable reported a failure. Pass it straight back to the caller and
      //    do NOT touch the Supabase session — there are no valid tokens.
      if (result.error) {
        return result;
      }

      // 3. Success: we have tokens. This next line is the actual purpose of the
      //    whole function. Lovable performed the OAuth handshake, but the rest
      //    of the app authenticates through Supabase, so the tokens have to be
      //    written into Supabase's session store. Without this the user would
      //    have completed login and still appear signed out everywhere.
      try {
        await supabase.auth.setSession(result.tokens);
      } catch (e) {
        // Normalise whatever was thrown into a real Error. JavaScript allows
        // throwing any value, so `e` might be a string or an object; callers
        // expect something with `.message` and a stack. The `instanceof` check
        // avoids double-wrapping something that is already an Error.
        return { error: e instanceof Error ? e : new Error(String(e)) };
      }
      return result;
    },
  },
};
