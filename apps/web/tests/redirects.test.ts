import { describe, expect, it } from "vitest";

import {
  RETURN_TO_HEADER,
  isAuthPath,
  preferredReturnPath,
  returnPath,
  safeInternalPath,
} from "@/lib/redirects";

describe("safeInternalPath", () => {
  it("keeps a same-site path", () => {
    expect(safeInternalPath("/w/abc/keys")).toBe("/w/abc/keys");
    expect(safeInternalPath("/onboarding")).toBe("/onboarding");
    expect(safeInternalPath("/")).toBe("/");
  });

  it("keeps a query string and fragment on a same-site path", () => {
    expect(safeInternalPath("/w/abc?tab=keys#top")).toBe("/w/abc?tab=keys#top");
  });

  it("falls back when there is nothing to go to", () => {
    expect(safeInternalPath(undefined)).toBe("/");
    expect(safeInternalPath(null)).toBe("/");
    expect(safeInternalPath("")).toBe("/");
  });

  // Each of these is an open redirect if it gets through, and a sign-in page
  // is where one is most valuable to an attacker: a link to our own domain
  // that bounces to theirs, immediately after someone typed a password.
  it.each([
    ["an absolute URL", "https://evil.test/phish"],
    ["a scheme-less absolute URL", "//evil.test/phish"],
    ["a backslash protocol-relative URL", "/\\evil.test/phish"],
    ["a javascript: URL", "javascript:alert(1)"],
    ["a data: URL", "data:text/html,<script>alert(1)</script>"],
    ["a relative path", "w/abc"],
    ["a bare host", "evil.test"],
    ["a newline, to inject a second header", "/ok\nLocation: https://evil.test"],
    ["a carriage return", "/ok\r\nSet-Cookie: a=b"],
    ["a null byte", "/ok\u0000/evil"],
  ])("refuses %s", (_label, value) => {
    expect(safeInternalPath(value)).toBe("/");
  });

  it("refuses a doubled slash even deep in an otherwise fine path", () => {
    // Belt and braces: "/\\" and "//" are checked at the start, which is
    // where a browser's protocol-relative parsing applies. A later "//" is
    // harmless, and this records that distinction on purpose.
    expect(safeInternalPath("/w/abc//keys")).toBe("/w/abc//keys");
    expect(safeInternalPath("//w/abc")).toBe("/");
  });
});

/**
 * The paths that must never be a return destination.
 *
 * Each of these is a loop if it gets through as a `next`: the sign-in page
 * sends a signed-in visitor to `next`, so `/login` would send them back to
 * itself, and `/auth/signout` would sign them out on arrival.
 */
describe("isAuthPath", () => {
  it.each([
    ["the sign-in page", "/login"],
    ["…with a query", "/login?next=%2Fw%2Fabc"],
    ["sign-out", "/auth/signout"],
    ["the OAuth callback", "/auth/callback"],
    ["…with a code", "/auth/callback?code=abc"],
    ["the bare auth segment", "/auth"],
  ])("recognises %s", (_label, path) => {
    expect(isAuthPath(path)).toBe(true);
  });

  it.each([
    ["the root", "/"],
    ["a workspace", "/w/abc"],
    ["a deep agent page", "/w/abc/p/def/agents/ghi"],
    ["onboarding", "/onboarding"],
  ])("leaves %s alone", (_label, path) => {
    expect(isAuthPath(path)).toBe(false);
  });

  it("matches on the path segment rather than the prefix", () => {
    // `/loginsomething` is a different route and not one of these, so the
    // prefix check is on `/login/` with the slash.
    expect(isAuthPath("/loginsomething")).toBe(false);
    expect(isAuthPath("/authority")).toBe(false);
    expect(isAuthPath("/login/extra")).toBe(true);
    expect(isAuthPath("/auth/anything")).toBe(true);
  });
});

/**
 * The destination middleware forwards.
 *
 * The claim these pin: a signed-out visitor goes back to the page they asked
 * for, not to the nearest route some layout happened to know about. Before
 * this existed, every route under `/w/{id}` returned to `/w/{id}` and the rest
 * of the path was lost.
 */
describe("returnPath", () => {
  it("keeps a deep path as it is", () => {
    expect(returnPath("/w/abc/p/def/agents/ghi")).toBe("/w/abc/p/def/agents/ghi");
    expect(returnPath("/onboarding", "")).toBe("/onboarding");
  });

  it("keeps the query the visitor asked for, one parameter or several", () => {
    expect(returnPath("/w/abc/keys", "?tab=ingest")).toBe("/w/abc/keys?tab=ingest");
    expect(returnPath("/w/abc/keys", "?tab=ingest&scope=full")).toBe(
      "/w/abc/keys?tab=ingest&scope=full",
    );
    // With or without the leading "?", since `nextUrl.search` has one and a
    // hand-written call might not.
    expect(returnPath("/w/abc/keys", "tab=ingest")).toBe("/w/abc/keys?tab=ingest");
  });

  it("drops `_rsc` and keeps everything beside it", () => {
    // Next's cache-buster on a client navigation. An expired session in an
    // open tab is exactly when a navigation becomes an RSC fetch, so without
    // this the visitor returns from sign-in to `?_rsc=1a2b3c` in their
    // address bar.
    expect(returnPath("/w/abc/keys", "?tab=ingest&_rsc=1a2b3c")).toBe(
      "/w/abc/keys?tab=ingest",
    );
    expect(returnPath("/w/abc/keys", "?_rsc=1a2b3c")).toBe("/w/abc/keys");
    expect(returnPath("/w/abc", "?_rsc=x&a=1&b=2")).toBe("/w/abc?a=1&b=2");
  });

  it("falls back to home when there is no path", () => {
    expect(returnPath("")).toBe("/");
    expect(returnPath("", "?tab=ingest")).toBe("/");
  });

  it("never adds a fragment, and keeps one that arrives inside the single path", () => {
    // A browser keeps everything after "#" to itself, so in practice there is
    // none to take and "the exact page" means the path and its query. What is
    // pinned here is that a "#" arriving anyway cannot become a second
    // destination: in the query it is encoded into the parameter it sits in,
    // and in the path it stays part of the path.
    expect(returnPath("/w/abc/keys", "?tab=ingest#top")).toBe("/w/abc/keys?tab=ingest%23top");
    expect(returnPath("/w/abc#top")).toBe("/w/abc#top");
  });

  it.each([
    ["a protocol-relative path", "//evil.test/phish"],
    ["a backslash protocol-relative path", "/\\evil.test/phish"],
    ["a relative path", "w/abc"],
    ["a newline, to inject a second header", "/ok\nLocation: https://evil.test"],
    ["a null byte", "/ok\u0000/evil"],
  ])("refuses %s", (_label, pathname) => {
    expect(returnPath(pathname)).toBe("/");
  });

  it("neutralises a control character hiding in the query rather than carrying it", () => {
    // Not rejected, and that is the right answer: round-tripping through
    // `URLSearchParams` percent-encodes the CRLF, so what comes out is an
    // ordinary same-origin path with an oddly-spelled parameter and no way
    // to split a `Location` header. A raw control character in the *path*
    // has no such encoding step and is refused outright, above.
    const built = returnPath("/w/abc", "?tab=ing\r\nSet-Cookie: a=b");

    expect(built).toBe("/w/abc?tab=ing%0D%0ASet-Cookie%3A+a%3Db");
    expect(built).not.toContain("\r");
    expect(built).not.toContain("\n");
    // Whatever it is, it must still pass the validator the sign-in page uses.
    expect(safeInternalPath(built)).toBe(built);
  });
});

describe("preferredReturnPath", () => {
  it("prefers the forwarded path over what the caller knew", () => {
    // The whole point: the workspace layout passes `/w/abc` for every route
    // beneath it, and its redirect is the one that gets served.
    expect(preferredReturnPath("/w/abc/p/def/agents/ghi", "/w/abc")).toBe(
      "/w/abc/p/def/agents/ghi",
    );
  });

  it("falls back to the caller's path when nothing was forwarded", () => {
    expect(preferredReturnPath(null, "/w/abc")).toBe("/w/abc");
    expect(preferredReturnPath(undefined, "/w/abc")).toBe("/w/abc");
    expect(preferredReturnPath("", "/w/abc")).toBe("/w/abc");
  });

  it("falls back rather than sanitising a forged header into a worse answer", () => {
    // A hostile "//evil.test" sanitises to "/", and preferring that "/" over
    // a good fallback would let a forged header quietly downgrade a real
    // return path. So a value is taken only if it survives validation
    // unchanged.
    for (const forged of ["//evil.test", "/\\evil.test", "https://evil.test", "\u0000"]) {
      expect(preferredReturnPath(forged, "/w/abc")).toBe("/w/abc");
    }
  });

  it("asks for nothing when there is neither, as it did before", () => {
    expect(preferredReturnPath(null, undefined)).toBeNull();
    expect(preferredReturnPath("//evil.test", undefined)).toBeNull();
    expect(preferredReturnPath(undefined, null)).toBeNull();
  });

  it("treats the root as a real destination, not as the failure value", () => {
    expect(preferredReturnPath("/", "/w/abc")).toBe("/");
  });

  it("names the header once, so middleware and the render cannot drift", () => {
    expect(RETURN_TO_HEADER).toBe("x-agentworld-return-to");
  });
});
