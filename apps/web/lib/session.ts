/**
 * What a page knows about its viewer, resolved once per render.
 *
 * Every authenticated page needs the same three things: the access token to
 * forward to the API, who the user is, and which workspaces they can see.
 * Resolving them in one place means a page body contains no auth plumbing,
 * and the "signed out" and "not configured" branches are handled identically
 * everywhere rather than per page.
 */

import { redirect } from "next/navigation";

import { ApiError } from "./api/client";
import { getMe, type Me } from "./api/tenancy";
import { isAuthConfigured } from "./supabase/config";
import { getAccessToken } from "./supabase/server";

export type SessionState =
  | { readonly kind: "unconfigured" }
  | { readonly kind: "signed-out" }
  | {
      readonly kind: "api-unreachable";
      readonly message: string;
      readonly requestId?: string | undefined;
    }
  | { readonly kind: "signed-in"; readonly token: string; readonly me: Me };

/**
 * Resolve the viewer without redirecting.
 *
 * Returns a state rather than throwing, because the three failure modes mean
 * different things to the person looking at the screen: no Supabase project
 * is a setup instruction, no session is a sign-in prompt, and an unreachable
 * API is "the backend is down", which is not the user's problem to fix.
 */
export async function readSession(): Promise<SessionState> {
  if (!isAuthConfigured()) return { kind: "unconfigured" };

  const token = await getAccessToken();
  if (token === null) return { kind: "signed-out" };

  try {
    // This is also the call that creates the local user row on a first
    // sign-in: there is no registration endpoint, by design.
    const me = await getMe(token);
    return { kind: "signed-in", token, me };
  } catch (error) {
    if (error instanceof ApiError && error.problem.status === 401) {
      // A token Supabase still accepts but the API does not - a mismatched
      // audience or project, usually. Treating it as signed out sends the
      // user somewhere they can act, rather than showing a 401 they cannot.
      return { kind: "signed-out" };
    }
    return {
      kind: "api-unreachable",
      message: error instanceof Error ? error.message : "Unknown error",
      requestId: error instanceof ApiError ? error.requestId : undefined,
    };
  }
}

/**
 * Resolve the viewer or send them away.
 *
 * For pages that have nothing to show a stranger. The signed-out redirect
 * carries the current path so sign-in returns to where they were headed.
 */
export async function requireSession(
  returnTo?: string,
): Promise<{ token: string; me: Me }> {
  const session = await readSession();
  switch (session.kind) {
    case "signed-in":
      return { token: session.token, me: session.me };
    // `return redirect(...)`, because `redirect` is typed `never` - it works
    // by throwing - and a bare call reads to eslint as a fall-through into
    // the next case.
    case "unconfigured":
    case "api-unreachable":
      // Both are states the start page explains properly; it would be unkind
      // to render a half-working workspace screen instead.
      return redirect("/");
    case "signed-out": {
      const next = returnTo ? `?next=${encodeURIComponent(returnTo)}` : "";
      return redirect(`/login${next}`);
    }
  }
}
