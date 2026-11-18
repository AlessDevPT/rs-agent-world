/**
 * Everything the model-profile screen decides, as functions that can be read.
 *
 * Pure, and in a `.ts` module for the reason the other presentation modules
 * give: Vitest here runs `environment: "node"` with no jsdom and collects
 * only `.test.ts` files, so a `.tsx` cannot be asserted on. The component
 * next to the page is therefore a shell - it holds form state and calls
 * these - and every rule worth stating lives here where
 * `tests/model-profile-presentation.test.ts` can state it.
 *
 * **The rule this file mostly exists for.** A profile names a provider and
 * may name a credential, and the API refuses the pair unless the credential
 * was issued for that same provider. It checks the row a request would leave
 * behind rather than the fields the request mentions, so changing the
 * provider alone can be refused because of a credential the form never sent.
 * `profileEdits` builds the PATCH body with that in mind: absent where
 * nothing changed, `null` only where somebody cleared a credential on
 * purpose, and a refusal before the request where the pair is knowably wrong.
 *
 * Nothing here ever sees credential material. The API has no route that
 * returns a secret, so the most this module handles is a credential's name
 * and the provider it was issued for.
 */

import type { Sentence } from "@/lib/agents/presentation";
import type { JsonObject } from "@/lib/api/agents";
import type { Credential } from "@/lib/api/credentials";
import type {
  CreateModelProfileBody,
  ModelProfile,
  UpdateModelProfileBody,
} from "@/lib/api/model-profiles";
import { roleAtLeast, type MemberRole } from "@/lib/api/tenancy";

/**
 * Whether this viewer may change anything here.
 *
 * Reading is `viewer` on the API and writing is `admin`, so this page shows
 * the list to everyone who can see the workspace and the forms to admins -
 * unlike the credentials page, which is admin-only end to end. This is a
 * rendering decision and not the authorization boundary: every write goes
 * through a server action to an endpoint that checks the role again.
 */
export function canManageProfiles(role: MemberRole): boolean {
  return roleAtLeast(role, "admin");
}

/** What to say when a workspace has no profiles at all. */
export function profilesEmptyState(): Sentence {
  return {
    headline: "No model profiles yet",
    detail:
      "A profile names the provider and model a managed agent calls, and the credential " +
      "it calls with. An agent cannot be set to run until one exists.",
  };
}

/**
 * What a viewer who cannot edit should be told, once.
 *
 * Said rather than implied by missing buttons: somebody who can see the list
 * but not change it should learn why from the page instead of wondering
 * whether it failed to load.
 */
export function readOnlyNotice(role: MemberRole): string {
  return (
    `Changing a model profile needs the admin role; you are a ${role}. ` +
    "A profile decides which vendor a workspace's key is sent to, so who may " +
    "edit one is held more narrowly than who may see it."
  );
}

// ------------------------------------------------------------- parameters ---

/**
 * The parameters object, as text somebody can edit.
 *
 * Pretty-printed with two spaces, and `{}` for an empty one rather than an
 * empty box - a blank textarea reads as "nothing to say here", and this field
 * is never nothing: it is an object that happens to be empty.
 */
export function formatParameters(parameters: JsonObject): string {
  return JSON.stringify(parameters, null, 2);
}

export type ParsedParameters =
  | { readonly ok: true; readonly value: JsonObject }
  | { readonly ok: false; readonly message: string };

/**
 * Text back into an object, refusing anything the API would not store.
 *
 * **A free-form object rather than a set of fields.** The API stores
 * `parameters` as an open JSON object and reads exactly two keys from it at
 * dispatch (`base_url` and `max_output_tokens`, in `dispatch._plan`), passing
 * everything else through untouched. A form with one input per known key
 * would therefore have to drop the keys it has no input for, which is the one
 * thing it must not do - so this is a JSON editor, and its job is to refuse
 * what cannot be stored rather than to guess at a shape.
 *
 * An array and a bare scalar are refused by name, because `{...}` is what the
 * column holds and `[1, 2]` would be a 422 from pydantic with a message about
 * a dict.
 */
export function parseParameters(text: string): ParsedParameters {
  const trimmed = text.trim();
  if (trimmed === "") {
    return {
      ok: false,
      message: "Parameters must be a JSON object. Use {} for none.",
    };
  }

  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch (error) {
    return {
      ok: false,
      message: `Parameters are not valid JSON: ${error instanceof Error ? error.message : "unparseable"}`,
    };
  }

  if (Array.isArray(parsed)) {
    return { ok: false, message: "Parameters must be a JSON object, not a list." };
  }
  if (typeof parsed !== "object" || parsed === null) {
    return { ok: false, message: "Parameters must be a JSON object, not a single value." };
  }
  return { ok: true, value: parsed as JsonObject };
}

/**
 * Whether two parameter objects say the same thing.
 *
 * Compared by value with object keys sorted, so reformatting the JSON in the
 * textarea - which is most of what editing it does - is not mistaken for a
 * change and does not put `parameters` in a PATCH body that otherwise only
 * renames the profile.
 */
export function sameParameters(a: unknown, b: unknown): boolean {
  return canonical(a) === canonical(b);
}

function canonical(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value) ?? "null";
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  const entries = Object.entries(value as Record<string, unknown>)
    .filter(([, v]) => v !== undefined)
    .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0));
  return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${canonical(v)}`).join(",")}}`;
}

// ------------------------------------------------------------ credentials ---

/** Whether this credential may be attached to a profile naming that provider. */
export function isCompatible(credential: Credential, providerSlug: string): boolean {
  return credential.provider_slug === providerSlug;
}

export type CredentialAvailability =
  /** The list could not be loaded, so nothing below is known. */
  | { readonly state: "unknown" }
  /** The workspace holds no credentials at all. */
  | { readonly state: "none" }
  /** It holds some, but none issued for this provider. */
  | { readonly state: "none-for-provider"; readonly providerSlug: string }
  | { readonly state: "available"; readonly credentials: readonly Credential[] };

/**
 * Which credentials may be offered for a provider, and why there are none.
 *
 * Three empty cases, kept apart because the answer differs. "The list did not
 * load" must never be shown as "you have no credentials" - that would send
 * somebody to create a second key they already have. "None for this provider"
 * is the interesting one: it is the case the API's pairing rule produces, and
 * the fix is a new credential rather than a different profile.
 *
 * `null` for `credentials` means not loaded or not permitted: listing
 * credentials is `admin` on the API, so a `viewer` reading this page never
 * has them.
 */
export function credentialAvailability(
  credentials: readonly Credential[] | null,
  providerSlug: string,
): CredentialAvailability {
  if (credentials === null) return { state: "unknown" };
  if (credentials.length === 0) return { state: "none" };
  const matching = credentials.filter((credential) => isCompatible(credential, providerSlug));
  if (matching.length === 0) return { state: "none-for-provider", providerSlug };
  return { state: "available", credentials: matching };
}

/** The sentence under the credential field for each of those cases. */
export function availabilityHint(availability: CredentialAvailability): string {
  switch (availability.state) {
    case "unknown":
      return "The credential list could not be loaded, so none can be offered here.";
    case "none":
      return "This workspace holds no credentials yet. A profile can be saved without one, but a managed agent cannot run on it.";
    case "none-for-provider":
      return `No credential in this workspace was issued for '${availability.providerSlug}'. Store one on the credentials page, or leave this profile without a key for now.`;
    case "available":
      return "Only credentials issued for the selected provider are listed: the API refuses any other pairing.";
  }
}

/**
 * How a credential is named in a list of choices.
 *
 * Its own name plus the provider it was issued for, and "revoked" when it
 * is - a revoked credential can still be attached, deliberately, because an
 * operator mid-rotation points at one. Nothing here is material: a name is
 * what an operator typed, and the slug is metadata about who the key is for.
 */
export function credentialLabel(credential: Credential): string {
  const revoked = credential.revoked_at === null ? "" : " · revoked";
  return `${credential.name} (${credential.provider_slug})${revoked}`;
}

/**
 * What the list says about a profile's credential.
 *
 * Four answers, and the fourth is the one worth having. A profile may hold an
 * id that no longer resolves - deleting a credential destroys its material
 * and keeps a tombstone, and nothing checks whether a profile points at it -
 * so "attached to something that is gone" is a real state and the page says
 * so rather than rendering a blank.
 */
export function credentialSummary(
  profile: ModelProfile,
  credentials: readonly Credential[] | null,
): string {
  if (profile.credential_id === null) return "No credential attached";
  if (credentials === null) return "A credential is attached";
  const found = credentials.find((credential) => credential.id === profile.credential_id);
  if (found === undefined) {
    return "Attached to a credential that is no longer available";
  }
  return credentialLabel(found);
}

/**
 * Whether the stored pair is one the API would now refuse.
 *
 * A row can predate a rule or outlive a build: a profile may name a provider
 * this build no longer serves, or hold a credential issued for another one.
 * Neither is swept by the write-time checks, and both make every managed run
 * on this profile fail - so the list flags them where they can be fixed
 * rather than leaving the discovery to a dead execution.
 */
export function profileWarning(
  profile: ModelProfile,
  providers: readonly string[] | null,
  credentials: readonly Credential[] | null,
): string | null {
  if (providers !== null && !providers.includes(profile.provider_slug)) {
    return `This build cannot serve '${profile.provider_slug}'. An agent running on this profile fails when its step is dispatched.`;
  }
  if (profile.credential_id !== null && credentials !== null) {
    const found = credentials.find((credential) => credential.id === profile.credential_id);
    if (found === undefined) {
      return "The attached credential is no longer available. Attach another or clear it.";
    }
    if (!isCompatible(found, profile.provider_slug)) {
      return `The attached credential was issued for '${found.provider_slug}', not '${profile.provider_slug}'.`;
    }
  }
  return null;
}

/**
 * The provider slugs to offer, with a stored one that is no longer served.
 *
 * `/v1/meta` lists what this build can serve and the API refuses anything
 * else, so that list is the whole of what a new profile may name. An existing
 * profile is the exception: its stored slug may have been retired, and the
 * API deliberately keeps such a row renamable and detachable rather than
 * trapping it. Dropping the slug from its own edit form would make the select
 * fall to the first option, so a rename would silently migrate the provider.
 */
export function providerChoices(
  providers: readonly string[],
  storedSlug: string | null,
): readonly string[] {
  if (storedSlug === null || storedSlug === "" || providers.includes(storedSlug)) {
    return providers;
  }
  return [storedSlug, ...providers];
}

// ------------------------------------------------------------------ forms ---

/** The fields of either form, as the component holds them. */
export interface ProfileDraft {
  readonly name: string;
  readonly providerSlug: string;
  readonly modelId: string;
  /** Raw textarea text, parsed by `parseParameters`. */
  readonly parameters: string;
  /** A credential id, or `""` for none. */
  readonly credentialId: string;
  readonly isDefault: boolean;
}

/** A draft that cannot be sent, and which field to say so against. */
export interface DraftRejected {
  readonly ok: false;
  readonly message: string;
  readonly field: keyof ProfileDraft;
}

export type CreateBuilt = { readonly ok: true; readonly body: CreateModelProfileBody } | DraftRejected;

export type EditBuilt =
  | { readonly ok: true; readonly body: UpdateModelProfileBody; readonly changed: boolean }
  | DraftRejected;

/**
 * A draft into a POST body.
 *
 * Everything is sent, because `create` has no absent-field semantics to
 * preserve: an omitted `parameters` is `{}` and an omitted `credential_id` is
 * null, which are the same rows the form would produce anyway. Being explicit
 * keeps the request readable in a log beside the form that made it.
 */
export function createBody(draft: ProfileDraft, credentials: readonly Credential[] | null): CreateBuilt {
  const basics = checkBasics(draft);
  if (basics !== null) return basics;

  const parameters = parseParameters(draft.parameters);
  if (!parameters.ok) return { ok: false, message: parameters.message, field: "parameters" };

  const pairing = checkPairing(draft.credentialId, draft.providerSlug, credentials);
  if (pairing !== null) return pairing;

  return {
    ok: true,
    body: {
      name: draft.name.trim(),
      provider_slug: draft.providerSlug,
      model_id: draft.modelId.trim(),
      parameters: parameters.value,
      credential_id: draft.credentialId === "" ? null : draft.credentialId,
      is_default: draft.isDefault,
    },
  };
}

/**
 * A draft into a PATCH body carrying only what moved.
 *
 * **Absent, not null.** The API distinguishes the two by `model_fields_set`,
 * and only `credential_id` may be null - so a field equal to what is stored
 * is left out entirely, and `null` appears exactly once: when a profile had a
 * credential and the form now says none. Renaming a profile therefore cannot
 * detach its key, which is the mistake this function exists to make
 * impossible.
 *
 * `changed` is false when nothing moved. The caller should send nothing at
 * all in that case: `ModelProfileService.update` records an audit row for
 * every request it accepts, including one that writes no columns, so an
 * empty PATCH would leave a trail of edits that never happened.
 *
 * **The pairing pre-check runs only when a half of the pair moves**, which is
 * the API's own rule and not a simplification of it. A row stored before that
 * rule may hold a credential issued for another provider, and the API
 * deliberately lets such a row be renamed and detached - the two edits that
 * repair it. Checking the pair on every submit would make exactly those two
 * impossible here while the API allowed them, which is the UI inventing a
 * refusal. The check is a courtesy either way: the server is the authority,
 * and where the credential list is unknown this says nothing at all.
 */
export function profileEdits(
  original: ModelProfile,
  draft: ProfileDraft,
  credentials: readonly Credential[] | null,
): EditBuilt {
  const basics = checkBasics(draft);
  if (basics !== null) return basics;

  const parameters = parseParameters(draft.parameters);
  if (!parameters.ok) return { ok: false, message: parameters.message, field: "parameters" };

  const body: {
    name?: string;
    provider_slug?: string;
    model_id?: string;
    parameters?: JsonObject;
    credential_id?: string | null;
    is_default?: boolean;
  } = {};

  const name = draft.name.trim();
  if (name !== original.name) body.name = name;

  if (draft.providerSlug !== original.provider_slug) body.provider_slug = draft.providerSlug;

  const modelId = draft.modelId.trim();
  if (modelId !== original.model_id) body.model_id = modelId;

  if (!sameParameters(parameters.value, original.parameters)) body.parameters = parameters.value;

  const credentialId = draft.credentialId === "" ? null : draft.credentialId;
  if (credentialId !== original.credential_id) body.credential_id = credentialId;

  if (draft.isDefault !== original.is_default) body.is_default = draft.isDefault;

  // Only now, and only if a half moved: the effective pair is the draft's,
  // because a draft is the whole resulting profile rather than a patch.
  if (body.provider_slug !== undefined || body.credential_id !== undefined) {
    const pairing = checkPairing(draft.credentialId, draft.providerSlug, credentials);
    if (pairing !== null) return pairing;
  }

  return { ok: true, body, changed: Object.keys(body).length > 0 };
}

/** The draft an edit form opens with: exactly what is stored. */
export function draftOf(profile: ModelProfile): ProfileDraft {
  return {
    name: profile.name,
    providerSlug: profile.provider_slug,
    modelId: profile.model_id,
    parameters: formatParameters(profile.parameters),
    credentialId: profile.credential_id ?? "",
    isDefault: profile.is_default,
  };
}

function checkBasics(draft: ProfileDraft): DraftRejected | null {
  if (draft.name.trim() === "") {
    return { ok: false, message: "A profile needs a name", field: "name" };
  }
  if (draft.providerSlug === "") {
    return { ok: false, message: "Choose the provider this profile calls", field: "providerSlug" };
  }
  if (draft.modelId.trim() === "") {
    return {
      ok: false,
      message: "Name the model to call, as the provider spells it",
      field: "modelId",
    };
  }
  return null;
}

/**
 * The pair, checked here only where the answer is already known.
 *
 * Silent when the credential is not in the list: it may be revoked and
 * filtered out, or belong to a listing this viewer never loaded, and guessing
 * "incompatible" would block an edit the API would accept.
 */
function checkPairing(
  credentialId: string,
  providerSlug: string,
  credentials: readonly Credential[] | null,
): DraftRejected | null {
  if (credentialId === "" || credentials === null) return null;
  const found = credentials.find((credential) => credential.id === credentialId);
  if (found === undefined || isCompatible(found, providerSlug)) return null;
  return {
    ok: false,
    message: `That credential was issued for '${found.provider_slug}', and this profile names '${providerSlug}'. A profile sends its credential to the provider it names, so the two have to agree.`,
    field: "credentialId",
  };
}

/**
 * What a changed provider does to a chosen credential.
 *
 * Cleared when the two no longer agree, rather than left to be refused: the
 * API checks the pair the request would leave behind, so a provider change
 * carrying an incompatible credential is a 422 on a field the form did not
 * even touch. Returning the new selection keeps that decision here, where it
 * is tested, instead of inside an event handler.
 */
export function credentialAfterProviderChange(
  credentialId: string,
  providerSlug: string,
  credentials: readonly Credential[] | null,
): string {
  if (credentialId === "" || credentials === null) return credentialId;
  const found = credentials.find((credential) => credential.id === credentialId);
  if (found === undefined) return credentialId;
  return isCompatible(found, providerSlug) ? credentialId : "";
}

/**
 * The form field an API refusal is about, in the form's own spelling.
 *
 * The API names its own fields - `provider_slug`, `credential_id` - and a
 * 422 carries that name in the problem document's `field`. Mapping it here
 * means a refusal the server made lands against the same input a refusal
 * this module made would, instead of above the whole card. An unmapped or
 * absent name is `null`, which is the honest answer for a message that
 * belongs to the form as a whole.
 */
export function draftFieldFor(field: string | undefined): keyof ProfileDraft | null {
  switch (field) {
    case "name":
      return "name";
    case "provider_slug":
      return "providerSlug";
    case "model_id":
      return "modelId";
    case "parameters":
      return "parameters";
    case "credential_id":
      return "credentialId";
    default:
      return null;
  }
}

/** What the default field means, said where it can be read in one place. */
export function defaultHint(isDefault: boolean, hasOtherDefault: boolean): string {
  if (isDefault) {
    return "The workspace default. Clearing it leaves the workspace with no default until another is chosen.";
  }
  return hasOtherDefault
    ? "Making this the default stands the current default down in the same write."
    : "This workspace has no default profile. One is what a new managed agent starts from.";
}
