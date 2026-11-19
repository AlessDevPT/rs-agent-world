/**
 * Where a render sends a visitor it cannot serve, and why that terminates.
 *
 * **The bug this module exists for.** Two layers used to answer "is this
 * visitor signed in?" with different authorities. The sign-in page asked the
 * cookie - `getSession()` reads it and checks `expires_at` without verifying
 * the signature - and said yes. Every other page asked the API, got a 401, and
 * said no. Neither removed the cookie they disagreed about, so a session whose
 * token the API rejects produced `/` -> `/login` -> `/` forever. Measured, not
 * inferred: twelve hops with no `Set-Cookie` on any of them, and the deep link
 * dropped at the second.
 *
 * So the question is asked once, by `readSession`, and both sides of the
 * decision are made here - in a module with no framework imports, because the
 * claim worth protecting is a property of the whole table rather than of any
 * one branch: **every redirect must land somewhere that renders for the same
 * session state.** That is what `tests/auth-destination.test.ts` asserts, over
 * every state, including the pair that used to point at each other.
 *
 * There is no retry here, and that is deliberate rather than minimal. The API
 * consumes the Supabase JWT directly - there is no second token to refresh -
 * and `getSession()` already refreshes an expired one inside the render before
 * the API is ever called. A refreshed token carries the same issuer, audience
 * and signing key, so it cannot fix the causes that persist: an audience or
 * issuer mismatch, or an API pointed at a different Supabase project. Retrying
 * would add a round trip and change nothing.
 */

import { HOME, isAuthPath, safeInternalPath } from "@/lib/redirects";

/**
 * What `readSession` concluded, as a bare tag.
 *
 * The tag rather than the state itself, so this module needs nothing from
 * `lib/session.ts` - which imports `next/headers` and cannot be loaded in the
 * node-only vitest environment at all.
 */
export type SessionKind =
  | "unconfigured"
  | "signed-out"
  | "token-rejected"
  /**
   * The API accepted the token and refused the caller anyway.
   *
   * Its own state because the alternative was calling it `api-unreachable`,
   * which put "the backend is down" on screen for an account that is working
   * perfectly and simply not allowed - a sentence that sends somebody to
   * check a service that is fine. Signing out and in again cannot help
   * either, so it does not go to the sign-in form.
   */
  | "forbidden"
  | "api-unreachable"
  | "signed-in";

export interface Redirect {
  readonly kind: "redirect";
  readonly to: string;
}

export type Destination = { readonly kind: "render" } | Redirect;

const RENDER: Destination = { kind: "render" };

/** The sign-in page, with the destination attached when there is one. */
function toLogin(next: string | null): Redirect {
  const target = returnTarget(next);
  if (target === HOME) return { kind: "redirect", to: "/login" };
  return { kind: "redirect", to: `/login?next=${encodeURIComponent(target)}` };
}

/**
 * Which state an API failure means.
 *
 * `status` is the problem document's status, or null for anything that was not
 * an `ApiError` at all - a `TypeError` from an unreachable host, say.
 *
 * **Only 401 is about the credential.** A 403 is the API saying the caller is
 * authenticated and still not allowed, which is not a reason to send anybody
 * to a sign-in form; a 503 is the identity provider's key set being
 * unreachable, or the API having no `SUPABASE_JWKS_URL`, and neither is the
 * visitor's problem to fix. All three land on the start page, which explains
 * the state it is in.
 *
 * **And a 403 is now its own answer rather than being folded into
 * `api-unreachable`.** The redirect was always right; the word was not. A
 * caller the API refuses is looking at a working backend, and telling them it
 * is down sends them to check a service that is fine - while saying nothing
 * about the only thing that would help, which is somebody granting them
 * access.
 */
export function kindForApiFailure(
  status: number | null,
): "token-rejected" | "forbidden" | "api-unreachable" {
  if (status === 401) return "token-rejected";
  if (status === 403) return "forbidden";
  return "api-unreachable";
}

/**
 * Where a page that has nothing to show a stranger sends one.
 *
 * `signed-in` is absent from the input on purpose: a guard that is still
 * deciding has already returned for that case, and a `Redirect` return type
 * means the caller needs no narrowing and no assertion to read `.to`.
 */
export function guardDestination(
  kind: Exclude<SessionKind, "signed-in">,
  next: string | null,
): Redirect {
  switch (kind) {
    case "signed-out":
    case "token-rejected":
      return toLogin(next);
    case "unconfigured":
    case "forbidden":
    case "api-unreachable":
      // All three are states the start page explains properly, and it renders
      // for them rather than redirecting again - which is what keeps them out
      // of the cycle the sign-in page was in.
      return { kind: "redirect", to: HOME };
  }
}

/**
 * What the sign-in page does.
 *
 * It renders for every state that is not signed in, including
 * `token-rejected`, and that single line is the fix: a page reached *because*
 * the API refused the token must not bounce a visitor back out on the strength
 * of the cookie that caused it. The notice it shows instead is derived from
 * the state, not from a `?reason=` parameter a visitor could type.
 */
export function loginDestination(kind: SessionKind, next: string | null): Destination {
  switch (kind) {
    case "signed-in":
      return { kind: "redirect", to: returnTarget(next) };
    case "unconfigured":
    case "forbidden":
    case "api-unreachable":
      // Signing in would work - the form talks to Supabase, not to us - and
      // then every page behind it would fail just the same. `forbidden` sits
      // here for a sharper version of the same reason: the credential is not
      // what is wrong, so offering a new one would be an invitation to try
      // the one thing that cannot help. The start page says which it is.
      return { kind: "redirect", to: HOME };
    case "signed-out":
    case "token-rejected":
      return RENDER;
  }
}

/**
 * The path a visitor should land on once they are signed in.
 *
 * Validated by identity rather than sanitised: a value that `safeInternalPath`
 * would rewrite is a value somebody tampered with, and the home page is a
 * better answer than a quietly altered one.
 *
 * **`/login` and `/auth/*` are refused, and that is load-bearing.** The
 * sign-in page now redirects a signed-in visitor to `next` instead of always
 * to `/`, so a `next` of `/login` would be a new loop of exactly the kind this
 * module exists to end - and `/auth/signout` would sign them out again on
 * arrival.
 */
export function returnTarget(next: string | null): string {
  if (!next) return HOME;
  if (safeInternalPath(next) !== next) return HOME;
  if (isAuthPath(next)) return HOME;
  return next;
}
