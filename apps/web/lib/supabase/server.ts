/**
 * The Supabase client for server components and route handlers.
 *
 * Cookie-based rather than token-in-memory, so a server component can read
 * the session without the browser having to hand it over on every navigation.
 * `@supabase/ssr` keeps the access token in a cookie and refreshes it, which
 * is what lets `GET /v1/me` be called during a server render.
 */

import { createServerClient } from "@supabase/ssr";
import { cookies } from "next/headers";

import { requireSupabaseConfig } from "./config";

export async function createSupabaseServerClient() {
  const { url, anonKey } = requireSupabaseConfig();
  const cookieStore = await cookies();

  return createServerClient(url, anonKey, {
    cookies: {
      getAll() {
        return cookieStore.getAll();
      },
      setAll(toSet) {
        try {
          for (const { name, value, options } of toSet) {
            cookieStore.set(name, value, options);
          }
        } catch {
          // A server *component* cannot set cookies - only a route handler or
          // middleware can. This is the documented shape for @supabase/ssr:
          // the write is attempted and ignored here, and `middleware.ts` is
          // what actually persists a refreshed token. Letting it throw would
          // break every page whose render happened to cross a refresh.
        }
      },
    },
  });
}

/**
 * The current access token, or null.
 *
 * This is the value the API's control plane wants in `Authorization: Bearer`.
 * Taken from the session rather than decoded or cached here: the API verifies
 * it against Supabase's JWKS, so the web app has no reason to look inside it
 * and no business deciding whether it is still valid.
 */
export async function getAccessToken(): Promise<string | null> {
  const supabase = await createSupabaseServerClient();
  const { data } = await supabase.auth.getSession();
  return data.session?.access_token ?? null;
}

/** The signed-in user, or null. Verified against Supabase, not just decoded. */
export async function getUser() {
  const supabase = await createSupabaseServerClient();
  // `getUser` revalidates with Supabase; `getSession` alone trusts the cookie,
  // which is fine for reading a token to forward but not for deciding whether
  // someone is signed in.
  const { data } = await supabase.auth.getUser();
  return data.user;
}
