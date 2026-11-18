/**
 * The model-profile screen's rules, which all live in one pure module.
 *
 * Vitest here runs `environment: "node"` and collects only `.test.ts`, so no
 * component is rendered anywhere in this suite - the manager is a shell that
 * calls these functions, and this file is where the behaviour it shows is
 * actually pinned. What that costs is stated in the report rather than hidden:
 * markup and event wiring are not covered by any test in this repository.
 *
 * **The PATCH builder is the reason most of this exists.** The API
 * distinguishes an absent field from an explicit `null`, and only
 * `credential_id` may be null - so "renaming a profile must not detach its
 * key" is a property of `profileEdits`, and it is asserted here rather than
 * discovered by an operator.
 */

import { describe, expect, it } from "vitest";

import type { Credential } from "@/lib/api/credentials";
import type { ModelProfile } from "@/lib/api/model-profiles";
import {
  availabilityHint,
  canManageProfiles,
  createBody,
  credentialAfterProviderChange,
  credentialAvailability,
  credentialLabel,
  credentialSummary,
  defaultHint,
  draftFieldFor,
  draftOf,
  formatParameters,
  isCompatible,
  parseParameters,
  profileEdits,
  profileWarning,
  profilesEmptyState,
  providerChoices,
  readOnlyNotice,
  sameParameters,
  type ProfileDraft,
} from "@/lib/model-profiles/presentation";

function aProfile(over: Partial<ModelProfile> = {}): ModelProfile {
  return {
    id: "p-1",
    name: "Production writer",
    provider_slug: "anthropic",
    model_id: "claude-opus-5",
    parameters: {},
    credential_id: null,
    is_default: false,
    ...over,
  };
}

function aCredential(over: Partial<Credential> = {}): Credential {
  return {
    id: "c-1",
    workspace_id: "w-1",
    project_id: null,
    name: "Anthropic, production",
    kind: "api_key",
    provider_slug: "anthropic",
    key_id: "kek-1",
    display_hint: "9f3c",
    scopes: null,
    expires_at: null,
    refresh_after: null,
    created_at: null,
    last_used_at: null,
    revoked_at: null,
    is_usable: true,
    ...over,
  };
}

describe("who may change a profile", () => {
  it("lets an admin and an owner edit, and no one below", () => {
    // Reading is `viewer` on the API and writing is `admin`, so this gate
    // decides whether forms render - not whether the list does.
    expect(canManageProfiles("owner")).toBe(true);
    expect(canManageProfiles("admin")).toBe(true);
    expect(canManageProfiles("member")).toBe(false);
    expect(canManageProfiles("viewer")).toBe(false);
  });

  it("says which role the reader has, so the absence of forms is explained", () => {
    expect(readOnlyNotice("member")).toContain("you are a member");
    expect(readOnlyNotice("member")).toContain("admin");
  });
});

describe("the list", () => {
  it("has an empty state that says what a profile is for", () => {
    const empty = profilesEmptyState();
    expect(empty.headline).toBe("No model profiles yet");
    expect(empty.detail).toContain("managed agent");
  });

  it("reports a profile with no credential as having none", () => {
    expect(credentialSummary(aProfile(), [])).toBe("No credential attached");
  });

  it("names the credential when it can be resolved", () => {
    const credential = aCredential();
    const summary = credentialSummary(aProfile({ credential_id: "c-1" }), [credential]);

    expect(summary).toBe("Anthropic, production (anthropic)");
    expect(summary).not.toContain("c-1");
  });

  it("says an attached credential is gone rather than rendering a blank", () => {
    // Deleting a credential destroys its material and keeps a tombstone that
    // no listing returns, and nothing stops a profile pointing at it.
    expect(credentialSummary(aProfile({ credential_id: "c-9" }), [aCredential()])).toBe(
      "Attached to a credential that is no longer available",
    );
  });

  it("does not claim a credential is gone when the list was never loaded", () => {
    // `null` is a viewer, who may not list credentials at all, or an admin
    // whose load failed. Either way the id resolves to nothing knowable.
    expect(credentialSummary(aProfile({ credential_id: "c-1" }), null)).toBe(
      "A credential is attached",
    );
  });

  it("marks a credential as revoked where it is, because one may still be attached", () => {
    expect(credentialLabel(aCredential({ revoked_at: "2026-01-01T00:00:00Z" }))).toBe(
      "Anthropic, production (anthropic) · revoked",
    );
  });
});

describe("the warnings on a stored row", () => {
  it("is silent about a profile this build can serve", () => {
    expect(profileWarning(aProfile(), ["anthropic"], [])).toBeNull();
  });

  it("flags a provider this build no longer serves", () => {
    // Such a row cannot be created any more, but one stored before the rule
    // survives - and every managed run on it dies at dispatch.
    const warning = profileWarning(aProfile({ provider_slug: "aurora" }), ["anthropic"], []);
    expect(warning).toContain("aurora");
    expect(warning).toContain("dispatched");
  });

  it("flags a credential that no longer resolves", () => {
    expect(profileWarning(aProfile({ credential_id: "c-9" }), ["anthropic"], [])).toContain(
      "no longer available",
    );
  });

  it("flags a pair the API would now refuse", () => {
    const foreign = aCredential({ id: "c-2", provider_slug: "aurora" });
    expect(
      profileWarning(aProfile({ credential_id: "c-2" }), ["anthropic"], [foreign]),
    ).toContain("issued for 'aurora'");
  });

  it("says nothing when neither list is known", () => {
    // Warning on unknown data would put a red line under every row for a
    // viewer, who cannot list credentials in the first place.
    expect(profileWarning(aProfile({ credential_id: "c-9" }), null, null)).toBeNull();
  });
});

describe("the provider choices", () => {
  it("offers what the API said it serves", () => {
    expect(providerChoices(["anthropic"], null)).toEqual(["anthropic"]);
  });

  it("keeps a stored slug the build no longer serves, first", () => {
    // Without it the select falls to its first option and a rename would
    // silently migrate the profile to another vendor.
    expect(providerChoices(["anthropic"], "aurora")).toEqual(["aurora", "anthropic"]);
  });

  it("does not duplicate a stored slug that is served", () => {
    expect(providerChoices(["anthropic", "aurora"], "aurora")).toEqual([
      "anthropic",
      "aurora",
    ]);
  });
});

describe("which credentials may be offered", () => {
  it("offers only the ones issued for the chosen provider", () => {
    const mine = aCredential();
    const theirs = aCredential({ id: "c-2", provider_slug: "aurora" });

    const availability = credentialAvailability([mine, theirs], "anthropic");

    expect(availability.state).toBe("available");
    expect(availability.state === "available" && availability.credentials).toEqual([mine]);
  });

  it("tells the three empty cases apart", () => {
    expect(credentialAvailability(null, "anthropic").state).toBe("unknown");
    expect(credentialAvailability([], "anthropic").state).toBe("none");
    expect(
      credentialAvailability([aCredential({ provider_slug: "aurora" })], "anthropic").state,
    ).toBe("none-for-provider");
  });

  it("explains each one differently, and never as the others", () => {
    const unknown = availabilityHint({ state: "unknown" });
    const none = availabilityHint({ state: "none" });
    const wrong = availabilityHint({ state: "none-for-provider", providerSlug: "aurora" });

    expect(unknown).toContain("could not be loaded");
    // The failure must not be dressed up as an empty workspace: that would
    // send somebody to store a second copy of a key they already have.
    expect(unknown).not.toContain("holds no credentials");
    expect(none).toContain("holds no credentials");
    expect(wrong).toContain("aurora");
  });

  it("compares the provider exactly", () => {
    expect(isCompatible(aCredential(), "anthropic")).toBe(true);
    expect(isCompatible(aCredential(), "Anthropic")).toBe(false);
  });
});

describe("changing the provider in a form", () => {
  it("drops a credential that no longer matches", () => {
    // The API checks the pair the request would leave behind, so a provider
    // change carrying the old key is a 422 on a field the form never touched.
    expect(credentialAfterProviderChange("c-1", "aurora", [aCredential()])).toBe("");
  });

  it("keeps one that still matches", () => {
    expect(credentialAfterProviderChange("c-1", "anthropic", [aCredential()])).toBe("c-1");
  });

  it("keeps a credential it cannot resolve, and lets the API answer", () => {
    expect(credentialAfterProviderChange("c-9", "aurora", [aCredential()])).toBe("c-9");
    expect(credentialAfterProviderChange("c-1", "aurora", null)).toBe("c-1");
  });
});

describe("the parameters field", () => {
  it("shows an empty object as an object", () => {
    expect(formatParameters({})).toBe("{}");
  });

  it("round-trips what is stored", () => {
    const stored = { base_url: "https://gateway.internal", max_output_tokens: 2048 };
    const parsed = parseParameters(formatParameters(stored));

    expect(parsed.ok && parsed.value).toEqual(stored);
  });

  it("refuses a blank box rather than guessing what it means", () => {
    // Blank is neither "unchanged" nor "clear it", so it is refused and said.
    const parsed = parseParameters("   ");
    expect(parsed.ok).toBe(false);
    expect(!parsed.ok && parsed.message).toContain("{}");
  });

  it("refuses a list and a bare value by name", () => {
    expect(parseParameters("[1, 2]")).toMatchObject({
      ok: false,
      message: "Parameters must be a JSON object, not a list.",
    });
    expect(parseParameters("7")).toMatchObject({
      ok: false,
      message: "Parameters must be a JSON object, not a single value.",
    });
    expect(parseParameters("null")).toMatchObject({ ok: false });
  });

  it("keeps the parser's own complaint about malformed JSON", () => {
    const parsed = parseParameters("{oops}");
    expect(parsed.ok).toBe(false);
    expect(!parsed.ok && parsed.message).toContain("not valid JSON");
  });

  it("compares by value with keys sorted, so reformatting is not a change", () => {
    expect(sameParameters({ a: 1, b: [1, { c: 2 }] }, { b: [1, { c: 2 }], a: 1 })).toBe(true);
    expect(sameParameters({ a: 1 }, { a: "1" })).toBe(false);
    expect(sameParameters({}, {})).toBe(true);
  });
});

describe("creating a profile", () => {
  const draft: ProfileDraft = {
    name: "  Production writer  ",
    providerSlug: "anthropic",
    modelId: "  claude-opus-5 ",
    parameters: '{"max_output_tokens": 2048}',
    credentialId: "c-1",
    isDefault: true,
  };

  it("sends every field, trimmed, as the API's own shape", () => {
    const built = createBody(draft, [aCredential()]);

    expect(built).toEqual({
      ok: true,
      body: {
        name: "Production writer",
        provider_slug: "anthropic",
        model_id: "claude-opus-5",
        parameters: { max_output_tokens: 2048 },
        credential_id: "c-1",
        is_default: true,
      },
    });
  });

  it("sends null rather than an empty string for no credential", () => {
    const built = createBody({ ...draft, credentialId: "" }, []);
    expect(built.ok && built.body.credential_id).toBeNull();
  });

  it("refuses an empty name, provider or model against the right field", () => {
    expect(createBody({ ...draft, name: "   " }, null)).toMatchObject({
      ok: false,
      field: "name",
    });
    expect(createBody({ ...draft, providerSlug: "" }, null)).toMatchObject({
      ok: false,
      field: "providerSlug",
    });
    expect(createBody({ ...draft, modelId: "" }, null)).toMatchObject({
      ok: false,
      field: "modelId",
    });
  });

  it("refuses unparseable parameters against the parameters field", () => {
    expect(createBody({ ...draft, parameters: "{" }, null)).toMatchObject({
      ok: false,
      field: "parameters",
    });
  });

  it("refuses a credential issued for another provider before asking the API", () => {
    const foreign = aCredential({ id: "c-2", provider_slug: "aurora" });
    const built = createBody({ ...draft, credentialId: "c-2" }, [foreign]);

    expect(built).toMatchObject({ ok: false, field: "credentialId" });
    expect(!built.ok && built.message).toContain("aurora");
  });

  it("stays silent about a credential it cannot resolve", () => {
    // The server is the authority on the pair; guessing "incompatible" here
    // would block an edit the API would have accepted.
    expect(createBody({ ...draft, credentialId: "c-9" }, [aCredential()]).ok).toBe(true);
    expect(createBody(draft, null).ok).toBe(true);
  });
});

describe("editing a profile", () => {
  const stored = aProfile({
    credential_id: "c-1",
    parameters: { max_output_tokens: 1024 },
  });

  it("opens with exactly what is stored", () => {
    expect(draftOf(stored)).toEqual({
      name: "Production writer",
      providerSlug: "anthropic",
      modelId: "claude-opus-5",
      parameters: '{\n  "max_output_tokens": 1024\n}',
      credentialId: "c-1",
      isDefault: false,
    });
  });

  it("sends nothing when nothing moved", () => {
    // An empty PATCH is accepted by the API and still records an audit row,
    // so the caller must not send one.
    const built = profileEdits(stored, draftOf(stored), [aCredential()]);
    expect(built).toEqual({ ok: true, body: {}, changed: false });
  });

  it("sends only the field that moved", () => {
    const built = profileEdits(
      stored,
      { ...draftOf(stored), name: "Renamed" },
      [aCredential()],
    );

    expect(built).toEqual({ ok: true, body: { name: "Renamed" }, changed: true });
  });

  it("does not detach the credential when renaming", () => {
    // The whole reason this builder exists: `credential_id` absent means
    // "leave it", and sending null here would clear a key on a rename.
    const built = profileEdits(stored, { ...draftOf(stored), name: "Renamed" }, null);

    expect(built.ok && "credential_id" in built.body).toBe(false);
  });

  it("sends an explicit null only when a credential is cleared on purpose", () => {
    const built = profileEdits(stored, { ...draftOf(stored), credentialId: "" }, null);

    expect(built).toEqual({ ok: true, body: { credential_id: null }, changed: true });
  });

  it("sends no credential_id when a profile that had none still has none", () => {
    const without = aProfile();
    const built = profileEdits(without, { ...draftOf(without), name: "Renamed" }, null);

    expect(built.ok && built.body).toEqual({ name: "Renamed" });
  });

  it("carries both halves when the provider and the credential move together", () => {
    // The migration the pairing rule must not prevent.
    const aurora = aCredential({ id: "c-2", provider_slug: "aurora" });
    const built = profileEdits(
      stored,
      { ...draftOf(stored), providerSlug: "aurora", credentialId: "c-2" },
      [aCredential(), aurora],
    );

    expect(built.ok && built.body).toEqual({ provider_slug: "aurora", credential_id: "c-2" });
  });

  it("still renames a row whose stored pair the API would now refuse", () => {
    // A profile stored before the pairing rule may hold another provider's
    // credential, and the API deliberately keeps such a row renamable and
    // detachable - those two edits are how it gets repaired. Checking the
    // pair on every submit would refuse a rename the API accepts, which is
    // the UI inventing a rule.
    const legacy = aProfile({ credential_id: "c-2" });
    const foreign = aCredential({ id: "c-2", provider_slug: "aurora" });

    const renamed = profileEdits(legacy, { ...draftOf(legacy), name: "Renamed" }, [foreign]);
    expect(renamed).toEqual({ ok: true, body: { name: "Renamed" }, changed: true });

    const detached = profileEdits(legacy, { ...draftOf(legacy), credentialId: "" }, [foreign]);
    expect(detached).toEqual({ ok: true, body: { credential_id: null }, changed: true });
  });

  it("refuses to move the provider away from a credential it keeps", () => {
    const built = profileEdits(stored, { ...draftOf(stored), providerSlug: "aurora" }, [
      aCredential(),
    ]);

    expect(built).toMatchObject({ ok: false, field: "credentialId" });
  });

  it("ignores a reformatted parameters box", () => {
    const built = profileEdits(
      stored,
      { ...draftOf(stored), parameters: '{"max_output_tokens":1024}' },
      null,
    );

    expect(built).toEqual({ ok: true, body: {}, changed: false });
  });

  it("sends parameters that genuinely changed, whole", () => {
    const built = profileEdits(
      stored,
      { ...draftOf(stored), parameters: '{"max_output_tokens": 1024, "base_url": "https://g"}' },
      null,
    );

    expect(built.ok && built.body).toEqual({
      parameters: { max_output_tokens: 1024, base_url: "https://g" },
    });
  });

  it("sends the default in both directions", () => {
    const made = profileEdits(stored, { ...draftOf(stored), isDefault: true }, null);
    expect(made.ok && made.body).toEqual({ is_default: true });

    const already = aProfile({ is_default: true });
    const cleared = profileEdits(already, { ...draftOf(already), isDefault: false }, null);
    expect(cleared.ok && cleared.body).toEqual({ is_default: false });
  });

  it("refuses a blank name and unparseable parameters, naming the field", () => {
    expect(profileEdits(stored, { ...draftOf(stored), name: " " }, null)).toMatchObject({
      ok: false,
      field: "name",
    });
    expect(
      profileEdits(stored, { ...draftOf(stored), parameters: "[]" }, null),
    ).toMatchObject({ ok: false, field: "parameters" });
  });
});

describe("the default's explanation", () => {
  it("warns that clearing leaves the workspace without one", () => {
    expect(defaultHint(true, false)).toContain("no default");
  });

  it("says the current default will be stood down", () => {
    expect(defaultHint(false, true)).toContain("stands the current default down");
  });

  it("says there is none yet when there is none", () => {
    expect(defaultHint(false, false)).toContain("no default profile");
  });
});

describe("where an API refusal is shown", () => {
  it("maps the API's field names onto the form's own", () => {
    expect(draftFieldFor("provider_slug")).toBe("providerSlug");
    expect(draftFieldFor("credential_id")).toBe("credentialId");
    expect(draftFieldFor("model_id")).toBe("modelId");
    expect(draftFieldFor("name")).toBe("name");
    expect(draftFieldFor("parameters")).toBe("parameters");
  });

  it("puts a refusal it cannot place above the form rather than against a guess", () => {
    expect(draftFieldFor(undefined)).toBeNull();
    expect(draftFieldFor("workspace_id")).toBeNull();
  });
});
