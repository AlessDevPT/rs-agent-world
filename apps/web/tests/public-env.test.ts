/**
 * The web app's environment lives in apps/web, and only public values go in it.
 *
 * Next.js loads .env files from its own project directory - the one holding
 * next.config.ts - not from the repository root. Both files documented the
 * NEXT_PUBLIC_* variables as belonging to the root .env, which is read by
 * nothing: the API has no use for them and Next never looks there. The
 * symptom is silent, because lib/env.ts falls back to defaults rather than
 * throwing, so a developer who had configured Supabase correctly in the root
 * .env still saw the "Almost there" setup page and nothing said why.
 *
 * These tests pin the two halves of the correction: the variables are
 * declared where Next reads them, and nothing server-only is given a
 * NEXT_PUBLIC_ name - the prefix is what publishes a value to every visitor.
 */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { describe, expect, it, vi } from "vitest";

import { readPublicEnv } from "@/lib/env";

const WEB = fileURLToPath(new URL("..", import.meta.url));
const ROOT = fileURLToPath(new URL("../../..", import.meta.url));

function read(path: string): string {
  return readFileSync(path, "utf8");
}

/** The assignment targets in a .env file, ignoring comments and blank lines. */
function declaredNames(body: string): string[] {
  return body
    .split("\n")
    .map((line) => line.trim())
    .filter((line) => line !== "" && !line.startsWith("#"))
    .map((line) => line.split("=", 1)[0]!.trim())
    .filter((name) => /^[A-Z][A-Z0-9_]*$/.test(name));
}

const webExample = read(`${WEB}.env.example`);
const rootExample = read(`${ROOT}.env.example`);

describe("the public environment is declared where Next.js reads it", () => {
  it("declares every NEXT_PUBLIC_ name that lib/env.ts reads", () => {
    const source = read(`${WEB}lib/env.ts`);
    const used = new Set(
      [...source.matchAll(/process\.env\.(NEXT_PUBLIC_[A-Z0-9_]+)/g)].map((m) => m[1]!),
    );

    expect(used.size).toBeGreaterThan(0);
    expect([...used].sort()).toEqual(
      declaredNames(webExample)
        .filter((name) => name.startsWith("NEXT_PUBLIC_"))
        .sort(),
    );
  });

  it("keeps the root .env.example free of NEXT_PUBLIC_ variables", () => {
    // The defect itself: a developer filling these in at the root gets no
    // error and no effect. The root file may still *mention* them in a
    // comment pointing here, which is why only declarations are checked.
    expect(declaredNames(rootExample).filter((n) => n.startsWith("NEXT_PUBLIC_"))).toEqual(
      [],
    );
  });

  it("does not declare the same variable in both files", () => {
    const web = new Set(declaredNames(webExample));
    const both = declaredNames(rootExample).filter((name) => web.has(name));

    expect(both).toEqual([]);
  });

  it("tells the reader which file to use on the setup page", () => {
    // Reverting this sends the next developer back to the root .env.
    expect(read(`${WEB}app/page.tsx`)).toContain("apps/web/.env.local");
  });

  it("names the file Next reads in the error a misconfigured build throws", () => {
    // Asserted by calling it rather than by grepping the source: a first
    // version of this test only checked that lib/env.ts *contained* the
    // path, which the corrected comment satisfied on its own - so reverting
    // the message alone went unnoticed. The message is the thing a developer
    // actually sees, so the message is what gets asserted.
    vi.stubEnv("NEXT_PUBLIC_API_URL", "");
    try {
      expect(() => readPublicEnv()).toThrowError(/apps\/web\/\.env\.local/);
    } finally {
      vi.unstubAllEnvs();
    }
  });
});

describe("nothing server-only is published through the prefix", () => {
  it("declares only NEXT_PUBLIC_ names in the web environment", () => {
    // A name without the prefix in this file is either dead - Next would not
    // expose it to the browser - or a secret somebody expected to be private
    // in a file whose whole purpose is the public bundle.
    const stray = declaredNames(webExample).filter((n) => !n.startsWith("NEXT_PUBLIC_"));

    expect(stray).toEqual([]);
  });

  it("gives no secret a NEXT_PUBLIC_ name anywhere in the app", () => {
    const forbidden = [
      "NEXT_PUBLIC_SUPABASE_SERVICE_ROLE_KEY",
      "NEXT_PUBLIC_SUPABASE_SERVICE_KEY",
      "NEXT_PUBLIC_API_KEY_PEPPER",
      "NEXT_PUBLIC_CREDENTIAL_KEKS",
      "NEXT_PUBLIC_CREDENTIAL_ACTIVE_KEK_ID",
      "NEXT_PUBLIC_DATABASE_URL",
      "NEXT_PUBLIC_MIGRATION_DATABASE_URL",
      "NEXT_PUBLIC_ANTHROPIC_API_KEY",
    ];
    const haystack = [webExample, read(`${WEB}lib/env.ts`), read(`${WEB}next.config.ts`)].join(
      "\n",
    );

    for (const name of forbidden) {
      expect(haystack).not.toContain(name);
    }
  });

  it("names the anon key and not the service-role key", () => {
    expect(webExample).toContain("NEXT_PUBLIC_SUPABASE_ANON_KEY");
    expect(webExample.toLowerCase()).not.toContain("service_role_key=");
  });
});
