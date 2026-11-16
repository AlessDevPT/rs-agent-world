/**
 * Validating a redirect target that came from a query string.
 *
 * The sign-in flow carries `?next=` so that signing in returns the visitor to
 * where they were headed. That value is attacker-controlled, and a sign-in
 * page is the single most useful place to have an open redirect: a link to
 * our own domain that bounces to theirs, arriving immediately after the
 * visitor typed a password, is a convincing way to collect another one.
 *
 * So: same-site absolute paths only. Everything else becomes "/".
 */

/** The fallback for anything that does not pass. */
export const HOME = "/";

export function safeInternalPath(value: string | null | undefined): string {
  if (!value) return HOME;

  // Must be an absolute path on this origin.
  if (!value.startsWith("/")) return HOME;

  // "//evil.test" and "/\evil.test" are protocol-relative URLs: a browser
  // reads both as "go to another host", and the second slips past a check
  // that only looks for a doubled forward slash.
  if (value.startsWith("//") || value.startsWith("/\\")) return HOME;

  // A control character can be used to break a Location header across lines
  // and inject one of the attacker's own. Checked by code point rather than
  // with a character class: literal control characters in a regex are what
  // `no-control-regex` exists to catch, and a suppression comment here would
  // read as "this rule is inconvenient" rather than as a deliberate check.
  for (let position = 0; position < value.length; position += 1) {
    const code = value.charCodeAt(position);
    if (code <= 0x1f || code === 0x7f) return HOME;
  }

  return value;
}

/**
 * Is this one of the paths whose job is to get a visitor signed in or out?
 *
 * Those are the paths that must never be a return destination. `/login` as a
 * `next` would send a signed-in visitor from the sign-in page back to the
 * sign-in page, and `/auth/signout` would sign them out on arrival - both
 * loops, and both reachable from a query parameter anybody can type.
 *
 * The query string is cut off first, so `/login?next=x` is recognised as
 * `/login`. Prefix matching is on `/login/` and `/auth/` with the slash,
 * because `/loginsomething` is a different route and not one of these.
 */
export function isAuthPath(value: string): boolean {
  const query = value.indexOf("?");
  const path = query === -1 ? value : value.slice(0, query);
  return (
    path === "/login" ||
    path.startsWith("/login/") ||
    path === "/auth" ||
    path.startsWith("/auth/")
  );
}

// ------------------------------------------------- carrying the destination ---

/**
 * The request header middleware uses to tell a render where the visitor was
 * actually headed.
 *
 * It exists because nothing else in a server render knows. A layout receives
 * its own params and its ancestors', never a child's, and a server component
 * has no pathname at all - so before this, the deepest `requireSession` a
 * route had was the one whose redirect lost. The workspace layout's
 * `/w/{id}` won every route beneath it and the rest of the path was dropped.
 *
 * **A navigation hint and nothing else.** It decides where a visitor lands
 * after signing in. It grants nothing, and every page, layout and loader goes
 * on making its own checks against the API.
 *
 * A client can send a header of this name, and middleware overwrites it on
 * every request it runs on - which the matcher now makes every request under
 * `/w/`. It did not always: the first matcher entry excludes any path ending
 * in an image extension, and a route parameter is entitled to end that way, so
 * `/w/x.png` and `/agents/agent.png` once skipped middleware and a forged
 * header survived. `middleware.ts` has the second entry that closes that, and
 * `tests/middleware-matcher.test.ts` holds it closed.
 *
 * Even then it went no further than the sender: the value is validated again
 * before use, so it can only ever be a path on this origin, and nobody can set
 * the headers on somebody else's request. Both still hold, and both are why
 * this is a hint rather than something to be trusted.
 */
export const RETURN_TO_HEADER = "x-agentworld-return-to";

/**
 * A request's pathname and query as a return path, or "/".
 *
 * Deliberately free of `next/headers`, `NextRequest` and every other runtime
 * API: it takes two strings, so `tests/redirects.test.ts` can exercise it in
 * the node-only vitest environment that cannot load the framework at all.
 *
 * No fragment, because there is never one to take - a browser does not send
 * what follows `#`. "The exact page" therefore means the path and its query,
 * and a visitor deep-linked to `#section` returns to the page without it.
 */
export function returnPath(pathname: string, search?: string | null): string {
  if (!pathname) return HOME;
  return safeInternalPath(`${pathname}${withoutInternalParams(search)}`);
}

/**
 * The query string a visitor would recognise as their own.
 *
 * `_rsc` is Next's cache-buster on a client navigation, not something anybody
 * typed. An expired session in an open tab is exactly when a navigation turns
 * into an RSC fetch, so dropping it here is what stops `?_rsc=1a2b3c` being
 * carried through sign-in and pasted back into the address bar afterwards.
 *
 * Round-tripping through `URLSearchParams` re-encodes as it goes - a literal
 * space comes back as `+` - which is a different spelling of the same query
 * rather than a different query.
 */
function withoutInternalParams(search: string | null | undefined): string {
  if (!search) return "";
  const params = new URLSearchParams(search.startsWith("?") ? search.slice(1) : search);
  params.delete("_rsc");
  const rendered = params.toString();
  return rendered === "" ? "" : `?${rendered}`;
}

/**
 * Where to send a visitor after sign-in, or null to ask for nothing.
 *
 * The forwarded path wins when there is one, because it is the page the
 * visitor actually asked for; `fallback` is what a caller knew about itself,
 * which for any route under a layout is less specific than the truth. Null
 * rather than "/" for "nothing to offer", so that the caller can leave `next`
 * off the sign-in URL entirely, exactly as it did before this existed.
 *
 * A forwarded value is taken only if it survives `safeInternalPath`
 * *unchanged*. Sanitising it instead would turn a forged `//evil.test` into
 * "/" and then prefer that "/" over a perfectly good fallback, which is how a
 * hostile header would get to quietly downgrade a legitimate return path.
 */
export function preferredReturnPath(
  forwarded: string | null | undefined,
  fallback?: string | null,
): string | null {
  if (forwarded && safeInternalPath(forwarded) === forwarded) return forwarded;
  return fallback ?? null;
}
