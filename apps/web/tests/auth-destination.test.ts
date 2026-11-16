/**
 * That the authentication redirects terminate, for every session state.
 *
 * **The bug these exist for.** A Supabase session whose token the API rejects
 * used to bounce `/` -> `/login` -> `/` indefinitely: the sign-in page read the
 * cookie and concluded "signed in", every other page asked the API and
 * concluded "signed out", and nothing removed the cookie they disagreed about.
 * Measured live before the fix - twelve hops, no `Set-Cookie` on any of them,
 * and the deep link dropped at the second.
 *
 * So the claim worth testing is not any single branch's answer; it is a
 * property of the whole table: **every redirect lands somewhere that renders
 * for the same session state.** `walk` below follows the redirects until
 * something renders, and refuses to stop at a page it has already seen. The
 * last test proves the walk can actually detect the original bug, by walking
 * the old decision and watching it loop - a test that passes against both the
 * bug and the fix would be worth nothing here.
 */

import { describe, expect, it } from "vitest";

import {
  guardDestination,
  kindForApiFailure,
  loginDestination,
  returnTarget,
  type Destination,
  type SessionKind,
} from "@/lib/auth/destination";

const KINDS = [
  "unconfigured",
  "signed-out",
  "token-rejected",
  "api-unreachable",
  "signed-in",
] as const satisfies readonly SessionKind[];

const DEEP = "/w/abc/p/def/agents/ghi";

describe("kindForApiFailure", () => {
  it("treats only a 401 as a question about the credential", () => {
    expect(kindForApiFailure(401)).toBe("token-rejected");
  });

  it.each([
    ["a 403, which is authorization and not authentication", 403],
    ["a 404", 404],
    ["a 429", 429],
    ["a 500", 500],
    ["a 503, which is the identity provider's key set being unreachable", 503],
  ])("does not send %s to a sign-in form", (_label, status) => {
    // The constraint this pins: an outage must never be reported as a signed-
    // out state, and a 403 is the API saying the caller is authenticated and
    // still not allowed.
    expect(kindForApiFailure(status)).toBe("api-unreachable");
  });

  it("treats a thrown value with no status as an outage", () => {
    // What `fetch` itself throws when there is nothing listening: no problem
    // document, no status to read.
    expect(kindForApiFailure(null)).toBe("api-unreachable");
  });
});

describe("guardDestination", () => {
  it.each([
    ["signed-out", "signed-out"],
    ["token-rejected", "token-rejected"],
  ] as const)("sends %s to sign in, carrying the destination", (_label, kind) => {
    expect(guardDestination(kind, DEEP)).toEqual({
      kind: "redirect",
      to: `/login?next=${encodeURIComponent(DEEP)}`,
    });
    expect(guardDestination(kind, null)).toEqual({ kind: "redirect", to: "/login" });
  });

  it.each([
    ["unconfigured", "unconfigured"],
    ["api-unreachable", "api-unreachable"],
  ] as const)("sends %s to the start page, which explains it", (_label, kind) => {
    // No `next` on these: the start page is not a sign-in form and has nowhere
    // to carry a destination to.
    expect(guardDestination(kind, DEEP)).toEqual({ kind: "redirect", to: "/" });
    expect(guardDestination(kind, null)).toEqual({ kind: "redirect", to: "/" });
  });

  it("refuses a destination that was tampered with, rather than sanitising it", () => {
    for (const forged of ["//evil.test", "/\\evil.test", "https://evil.test", "/ok\nX: y"]) {
      expect(guardDestination("signed-out", forged)).toEqual({
        kind: "redirect",
        to: "/login",
      });
    }
  });
});

describe("loginDestination", () => {
  it("renders for a session the API rejected, rather than bouncing", () => {
    // The fix, in one assertion. This returning a redirect is the bug.
    expect(loginDestination("token-rejected", null)).toEqual({ kind: "render" });
    expect(loginDestination("token-rejected", DEEP)).toEqual({ kind: "render" });
  });

  it("renders for a visitor with no session", () => {
    expect(loginDestination("signed-out", DEEP)).toEqual({ kind: "render" });
  });

  it("sends a signed-in visitor to where they were headed", () => {
    expect(loginDestination("signed-in", DEEP)).toEqual({ kind: "redirect", to: DEEP });
    expect(loginDestination("signed-in", null)).toEqual({ kind: "redirect", to: "/" });
  });

  it.each([
    ["unconfigured", "unconfigured"],
    ["api-unreachable", "api-unreachable"],
  ] as const)("sends %s to the start page instead of a form that leads nowhere", (_l, kind) => {
    expect(loginDestination(kind, DEEP)).toEqual({ kind: "redirect", to: "/" });
  });

  it("never sends a signed-in visitor back to an authentication route", () => {
    // Otherwise `next=/login` would be a fresh loop, and `next=/auth/signout`
    // would sign them out on arrival.
    for (const next of ["/login", "/login?next=/", "/auth/signout", "/auth/callback"]) {
      expect(loginDestination("signed-in", next)).toEqual({ kind: "redirect", to: "/" });
    }
  });
});

describe("returnTarget", () => {
  it("keeps a path the visitor could have asked for", () => {
    expect(returnTarget(DEEP)).toBe(DEEP);
    expect(returnTarget("/w/abc/keys?tab=ingest")).toBe("/w/abc/keys?tab=ingest");
  });

  it("falls back to home when there is nothing to return to", () => {
    expect(returnTarget(null)).toBe("/");
    expect(returnTarget("")).toBe("/");
  });

  it.each([
    ["a protocol-relative path", "//evil.test/phish"],
    ["a backslash protocol-relative path", "/\\evil.test"],
    ["an absolute URL", "https://evil.test"],
    ["a relative path", "w/abc"],
    ["a control character", "/ok\nLocation: https://evil.test"],
  ])("refuses %s", (_label, next) => {
    expect(returnTarget(next)).toBe("/");
  });

  it.each([
    ["the sign-in page itself", "/login"],
    ["the sign-in page with its own next", "/login?next=%2Fw%2Fabc"],
    ["sign-out", "/auth/signout"],
    ["the OAuth callback", "/auth/callback?code=x"],
  ])("refuses %s, which would be a loop", (_label, next) => {
    expect(returnTarget(next)).toBe("/");
  });
});

// ------------------------------------------------------ the whole table ---

type Page = "/" | "/login" | "protected";

/** Which page a redirect target is. */
function pageOf(to: string): Page {
  if (to === "/") return "/";
  if (to === "/login" || to.startsWith("/login?")) return "/login";
  return "protected";
}

/** The `next` a redirect target carries, if any. */
function nextOf(to: string): string | null {
  return new URL(to, "http://localhost").searchParams.get("next");
}

/**
 * One hop for a session state that does not change while we walk.
 *
 * `/login` and the guarded pages are the real functions. `/` is modelled from
 * `app/page.tsx`, which decides in a `switch` of its own rather than through
 * this module - so that one node is a copy, and this comment is here so a
 * reader knows to check it if `app/page.tsx` changes.
 */
function hop(
  page: Page,
  kind: SessionKind,
  next: string | null,
  login: (kind: SessionKind, next: string | null) => Destination,
): Destination {
  if (page === "protected") {
    if (kind === "signed-in") return { kind: "render" };
    return guardDestination(kind, next);
  }
  if (page === "/login") return login(kind, next);
  switch (kind) {
    case "unconfigured":
    case "api-unreachable":
      // The start page renders both of these; it is the page that explains
      // which environment state the app is in.
      return { kind: "render" };
    case "signed-out":
    case "token-rejected":
      return { kind: "redirect", to: "/login" };
    case "signed-in":
      // In the real page this is the viewer's first workspace or onboarding;
      // either way it is a page that renders for a signed-in viewer.
      return { kind: "redirect", to: "/w/abc" };
  }
}

/** Follow the redirects until something renders, or a page repeats. */
function walk(
  start: Page,
  kind: SessionKind,
  next: string | null,
  login: (kind: SessionKind, next: string | null) => Destination = loginDestination,
): string {
  const seen = new Set<Page>();
  let page = start;
  let carried = next;

  for (let hops = 0; hops < 8; hops += 1) {
    if (seen.has(page)) return `LOOP at ${page}`;
    seen.add(page);

    const destination = hop(page, kind, carried, login);
    if (destination.kind === "render") return `renders ${page}`;

    page = pageOf(destination.to);
    carried = nextOf(destination.to);
  }
  return "DID NOT TERMINATE";
}

describe("the redirect table as a whole", () => {
  it.each(KINDS.flatMap((kind) => (["/", "/login", "protected"] as const).map((p) => [kind, p])))(
    "terminates for %s starting at %s",
    (kind, start) => {
      const outcome = walk(start as Page, kind as SessionKind, DEEP);
      expect(outcome).toMatch(/^renders /);
    },
  );

  it("would have caught the bug it was written for", () => {
    // The old sign-in page: a cookie is present, so redirect to "/" - which
    // for `token-rejected` is the page that sends them straight back here.
    const old = (kind: SessionKind, next: string | null): Destination =>
      kind === "signed-out" ? { kind: "render" } : { kind: "redirect", to: next ?? "/" };

    expect(walk("/", "token-rejected", DEEP, old)).toBe("LOOP at /");
    expect(walk("/login", "token-rejected", DEEP, old)).toMatch(/^LOOP/);
    // And the state that was never broken stays unbroken under the old rule,
    // which is why the bug survived: nothing a signed-out visitor did hit it.
    expect(walk("/", "signed-out", DEEP, old)).toBe("renders /login");
  });
});
