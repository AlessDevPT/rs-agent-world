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
import { guardDestination, kindForApiFailure } from "./auth/destination";
import { preferredReturnPath, RETURN_TO_HEADER } from "./redirects";
import { isAuthConfigured } from "./supabase/config";
import { getAccessToken } from "./supabase/server";

export type SessionState =
  | { readonly kind: "unconfigured" }
  /** No Supabase session at all. */
  | { readonly kind: "signed-out" }
  /**
   * A Supabase session the API will not accept.
   *
   * Kept apart from `signed-out` because it is a different fact and the two
   * need different handling: there is a cookie, nothing in a server render can
   * remove it, and the sign-in page must render rather than trust it. Folding
   * this into `signed-out` is what made `/` and `/login` redirect to each
   * other indefinitely.
   */
  | { readonly kind: "token-rejected" }
  | {
      readonly kind: "api-unreachable";
      readonly message: string;
      readonly requestId?: string | undefined;
    }
  | { readonly kind: "signed-in"; readonly token: string; readonly me: Me };

/**
 * Resolve the viewer without redirecting.
 *
 * Returns a state rather than throwing, because the four failure modes mean
 * different things to the person looking at the screen: no Supabase project is
 * a setup instruction, no session is a sign-in prompt, a session the API
 * refuses is a sign-in prompt that has to explain itself, and an unreachable
 * API is "the backend is down", which is not the user's problem to fix.
 *
 * **This is the only place the question is asked.** Everything that needs to
 * know consults this - the sign-in page included, which used to read the
 * cookie directly and reach the opposite conclusion.
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
    const status = error instanceof ApiError ? error.problem.status : null;
    if (kindForApiFailure(status) === "token-rejected") {
      // A token Supabase still accepts but the API does not - a mismatched
      // audience or issuer, or an API pointed at another Supabase project.
      // Deliberately not `signed-out`: there *is* a session, and saying
      // otherwise is what let the sign-in page send the visitor straight back
      // out again on the strength of the cookie that caused this.
      return { kind: "token-rejected" };
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
 * For pages that have nothing to show a stranger. The redirect to sign-in -
 * for no session and for one the API refuses alike - carries the current path
 * so signing in returns them to where they were headed.
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
  if (session.kind === "signed-in") return { token: session.token, me: session.me };

  // Validated here rather than trusted. Middleware overwrites the header on
  // every request it runs on, and the matcher covers every route there is -
  // but a matcher is a list, and this line should not be the thing that breaks
  // when one is added to it. The validation keeps whatever arrives to a path
  // on this origin.
  const forwarded = (await headers()).get(RETURN_TO_HEADER);
  const destination = guardDestination(
    session.kind,
    preferredReturnPath(forwarded, returnTo),
  );

  // `return redirect(...)`, because `redirect` is typed `never` - it works by
  // throwing - and returning it states the control flow instead of leaving a
  // bare call that reads as falling through.
  return redirect(destination.to);
}
