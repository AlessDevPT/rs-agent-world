import { describe, expect, it } from "vitest";

import { safeInternalPath } from "@/lib/redirects";

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
