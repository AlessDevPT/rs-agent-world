/**
 * What a credential's row is allowed to say.
 *
 * Two claims carry the weight. **An absent hint is explained rather than left
 * blank** - an operator looking for the four characters their provider's
 * dashboard shows needs to know whether none is coming or whether the key is
 * simply short. And **`is_usable` is the server's answer, not this module's**:
 * where the row cannot explain why something is unusable, it says exactly
 * that instead of guessing at expiry.
 */

import { describe, expect, it } from "vitest";

import { pillClassName } from "@/lib/agents/presentation";
import type { Credential } from "@/lib/api/credentials";
import {
  EXPIRY_WARNING_DAYS,
  credentialState,
  credentialStateLabel,
  credentialStateTone,
  credentialsEmptyState,
  expirySentence,
  hintSentence,
  lastUsedSentence,
  type CredentialState,
} from "@/lib/credentials/presentation";

const NOW = new Date("2026-11-17T12:00:00Z");
const DAY = 24 * 60 * 60 * 1000;

const STATES: readonly CredentialState[] = [
  "active",
  "expiring",
  "expired",
  "revoked",
  "unusable",
];

function credential(overrides: Partial<Credential> = {}): Credential {
  return {
    id: "c-1",
    workspace_id: "w-1",
    project_id: null,
    name: "Anthropic, production",
    kind: "api_key",
    provider_slug: "anthropic",
    key_id: "local-dev",
    display_hint: "…a4f2",
    scopes: null,
    expires_at: null,
    refresh_after: null,
    created_at: "2026-11-01T00:00:00Z",
    last_used_at: null,
    revoked_at: null,
    is_usable: true,
    ...overrides,
  };
}

function at(offsetMs: number): string {
  return new Date(NOW.getTime() + offsetMs).toISOString();
}

describe("credentialState", () => {
  it("calls a plain usable credential active", () => {
    expect(credentialState(credential(), NOW)).toBe("active");
  });

  it("warns before expiry, and only inside the window", () => {
    expect(credentialState(credential({ expires_at: at(3 * DAY) }), NOW)).toBe("expiring");
    expect(
      credentialState(credential({ expires_at: at((EXPIRY_WARNING_DAYS + 1) * DAY) }), NOW),
    ).toBe("active");
  });

  it("calls a past expiry expired", () => {
    expect(credentialState(credential({ expires_at: at(-DAY) }), NOW)).toBe("expired");
  });

  it("says revoked even when it also expired", () => {
    // Revoking is something somebody did; expiry is something that happened.
    // The act is the more useful answer.
    const both = credential({ revoked_at: at(-2 * DAY), expires_at: at(-DAY), is_usable: false });
    expect(credentialState(both, NOW)).toBe("revoked");
  });

  it("does not invent a cause when the server alone says no", () => {
    // `is_usable` folds in `deleted_at`, which `CredentialView` does not
    // carry, so a row can be unusable for a reason this view cannot see.
    // Reporting that as expiry would be a guess printed as a fact.
    expect(credentialState(credential({ is_usable: false }), NOW)).toBe("unusable");
    expect(
      credentialState(credential({ is_usable: false, expires_at: at(3 * DAY) }), NOW),
    ).toBe("unusable");
  });

  it("ignores an unparseable expiry rather than treating it as expired", () => {
    expect(credentialState(credential({ expires_at: "not a date" }), NOW)).toBe("active");
  });
});

describe("state labels and tones", () => {
  it("names every state and gives each a tone the pill helper accepts", () => {
    for (const state of STATES) {
      expect(credentialStateLabel(state).trim()).not.toBe("");
      expect(pillClassName(credentialStateTone(state))).toContain("pill");
    }
  });

  it("does not colour a deliberate revocation as a fault", () => {
    expect(credentialStateTone("revoked")).toBe("neutral");
    expect(credentialStateTone("expired")).toBe("bad");
    expect(credentialStateTone("unusable")).toBe("bad");
    expect(credentialStateTone("expiring")).toBe("warn");
    expect(credentialStateTone("active")).toBe("ok");
  });
});

describe("hintSentence", () => {
  it("shows the hint the server derived", () => {
    expect(hintSentence(credential())).toBe("Ends …a4f2");
  });

  it("explains an absent hint by the kind that cannot have one", () => {
    // Only `api_key` material earns a hint; the others would give away more
    // than they show, and silence here reads as a bug.
    for (const kind of ["oauth2", "basic", "custom"] as const) {
      const sentence = hintSentence(credential({ kind, display_hint: null }));
      expect(sentence).toContain("only API-key material");
    }
  });

  it("explains an absent hint on an API key by the material being short", () => {
    const sentence = hintSentence(credential({ display_hint: null }));
    expect(sentence).toContain("too short");
  });

  it("never prints anything but the four characters the server sent", () => {
    // The one place a fragment of material reaches this module, and it is
    // already a fragment: nothing here reconstructs or pads it.
    expect(hintSentence(credential({ display_hint: "…ZZZZ" }))).toBe("Ends …ZZZZ");
  });
});

describe("the remaining sentences", () => {
  it("says a credential has never been used rather than leaving a blank", () => {
    expect(lastUsedSentence(credential())).toBe("Never used");
    expect(lastUsedSentence(credential({ last_used_at: "2026-11-16T10:00:00Z" }))).toBe(
      "Last used 2026-11-16 10:00 UTC",
    );
  });

  it("says there is no expiry rather than nothing", () => {
    expect(expirySentence(credential())).toBe("No expiry set");
    expect(expirySentence(credential({ expires_at: "2026-12-01T23:59:59Z" }))).toBe(
      "Expires 2026-12-01 23:59 UTC",
    );
  });

  it("tells a first-time reader what a credential is for", () => {
    expect(credentialsEmptyState().detail).toContain("model profile");
  });
});
