/**
 * Which requests middleware runs on.
 *
 * This is a security test, not a configuration test. Middleware is what
 * replaces the `x-agentworld-return-to` header with the real destination; a
 * request it skips is one where a client's own header reaches the render
 * untouched. The matcher is therefore the boundary of that guarantee, and
 * these pin where it falls.
 *
 * **The bug this exists for.** The first matcher entry excludes any path
 * ending in an image extension, anywhere in the tree - and a dynamic route
 * parameter is entitled to end that way. `/w/x.png` is a workspace id and
 * `/agents/agent.png` is an agent detail page, and both skipped middleware.
 * Measured live before the fix: a forged header came back unreplaced.
 *
 * **Why it compiles the real thing.** The patterns are read from
 * `middleware.ts` itself rather than copied, and run through Next's own
 * build-time compiler and its own runtime matcher - the same two functions the
 * framework uses, not a regex written here that resembles them. A test that
 * re-implemented the semantics could agree with itself while disagreeing with
 * the server. No dependency is added for this: `next` is already one.
 */

import { createRequire } from "node:module";
import nextConfig from "../next.config";

import { describe, expect, it } from "vitest";

import { config } from "@/middleware";

const require = createRequire(import.meta.url);

interface CompiledMatcher {
  readonly regexp: string;
}

const { getMiddlewareMatchers } = require(
  "next/dist/build/analysis/get-page-static-info.js",
) as {
  getMiddlewareMatchers: (
    matchers: readonly string[],
    nextConfig: Record<string, unknown>,
  ) => CompiledMatcher[];
};

const { getMiddlewareRouteMatcher } = require(
  "next/dist/shared/lib/router/utils/middleware-route-matcher.js",
) as {
  getMiddlewareRouteMatcher: (
    matchers: readonly CompiledMatcher[],
  ) => (pathname: string, req?: unknown, query?: unknown) => boolean;
};

/** True when middleware would run for this path, by Next's own reckoning. */
/**
 * Compiled against the app's real `next.config.ts`, not against `{}`.
 *
 * `getMiddlewareMatchers` reads `basePath` and `i18n` out of the config it is
 * handed, so an empty object asks Next to compile the matchers for an app
 * that is not this one. It agrees today because neither option is set - which
 * is exactly why the placeholder was invisible, and why the day somebody adds
 * a `basePath` this test would keep passing while middleware stopped running
 * on every path below it.
 */
const runsOn = getMiddlewareRouteMatcher(
  getMiddlewareMatchers(config.matcher, nextConfig as unknown as Record<string, unknown>),
);

const WORKSPACE = "d59e337d-0663-482a-bd97-d0c329a2c9aa";
const PROJECT = "a3882559-1b0b-4855-abc1-1a9e556c5b59";

describe("the middleware matcher", () => {
  it("is the two entries the file declares, in order", () => {
    // The first entry is pinned whole rather than by its prefix: every
    // extension in that alternation is a decision, and dropping one would
    // start running middleware on that kind of asset with nothing here to
    // notice. The second entry is the whole fix.
    expect(config.matcher).toHaveLength(2);
    expect(config.matcher[0]).toBe(
      "/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico)$).*)",
    );
    expect(config.matcher[1]).toBe("/w/:path*");
  });

  it.each([
    ["a workspace id that ends in .png", "/w/x.png"],
    ["…in .svg", "/w/x.svg"],
    ["…in .webp", "/w/x.webp"],
    ["…in .jpeg", "/w/x.jpeg"],
    ["…in .ico", "/w/x.ico"],
    ["an agent id that ends in .png", `/w/${WORKSPACE}/p/${PROJECT}/agents/agent.png`],
  ])("covers %s", (_label, path) => {
    // Each of these is a real application route whose last segment happens to
    // end like a picture. Before the second entry, every one was skipped.
    expect(runsOn(path)).toBe(true);
  });

  it.each([
    ["a workspace", `/w/${WORKSPACE}`],
    ["its keys page", `/w/${WORKSPACE}/keys`],
    ["a project's agents", `/w/${WORKSPACE}/p/${PROJECT}/agents`],
    ["onboarding", "/onboarding"],
    ["the sign-in page", "/login"],
    ["the root", "/"],
  ])("still covers %s", (_label, path) => {
    expect(runsOn(path)).toBe(true);
  });

  it.each([
    ["the build output", "/_next/static/chunks/x.js"],
    ["the image optimiser", "/_next/image"],
    ["the favicon", "/favicon.ico"],
    ["a real static asset at the root", "/logo.png"],
  ])("still skips %s", (_label, path) => {
    // The point of the exclusions: refreshing a token on every asset request
    // would mean dozens of refreshes per page. Adding an entry scoped to `/w/`
    // cannot reach any of these, which is why the regex above was left alone.
    expect(runsOn(path)).toBe(false);
  });
});
