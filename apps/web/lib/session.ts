/**
 * What a page knows about its viewer, resolved once per render.
 *
 * Every authenticated page needs the same three things: the access token to
 * forward to the API, who the user is, and which workspaces they can see.
 * Resolving them in one place means a page body contains no auth plumbing,
 * and the "signed out" and "not configured" branches are handled identically
 * everywhere rather than per page.
 */

import { headers } from "next/headers";
import { redirect } from "next/navigation";

import { ApiError } from "./api/client";
import { getMe, type Me } from "./api/tenancy";
import { preferredReturnPath, RETURN_TO_HEADER } from "./redirects";
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
 *
 * **Where that path comes from, and why `returnTo` is now the fallback.** A
 * caller can only name its own route, and under a layout that is the wrong
 * answer: a layout and its page both redirect, and only one of them is served.
 * Measured, before this existed: every route under `/w/{id}` sent a visitor
 * back to `/w/{id}`, the workspace layout's path, with the rest of what they
 * asked for dropped - the keys page included, since it was written. Middleware
 * puts the real destination in a request header, and the header wins because
 * it is the only value that is the page the visitor actually asked for, not a
 * guess made by whichever layer happened to notice first.
 *
 * `returnTo` stays as the fallback rather than being deleted from its seven
 * call sites. The matcher covers every route there is today, `/w/` included
 * by its own entry - but it is a list, and a tree added outside the ones it
 * names would render without a header rather than fail loudly. A slightly
 * stale `next` is a better failure than none at all.
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
      // Validated here rather than trusted. Middleware overwrites the header
      // on every request it runs on, and the matcher covers every route there
      // is - but a matcher is a list, and this line should not be the thing
      // that breaks when one is added to it. The validation keeps whatever
      // arrives to a path on this origin.
      const forwarded = (await headers()).get(RETURN_TO_HEADER);
      const destination = preferredReturnPath(forwarded, returnTo);
      const next = destination ? `?next=${encodeURIComponent(destination)}` : "";
      return redirect(`/login${next}`);
    }
  }
}
