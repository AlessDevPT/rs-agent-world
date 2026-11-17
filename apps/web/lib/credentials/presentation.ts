/**
 * Turning a credential's metadata into something an operator can read.
 *
 * Pure, and in a `.ts` module for the reason the other presentation modules
 * give: Vitest here runs `environment: "node"` with no jsdom, so a `.tsx`
 * cannot be asserted on and anything worth stating lives where
 * `tests/credential-presentation.test.ts` can state it.
 *
 * **Nothing here ever sees material.** The API has no route that returns a
 * secret, so the most this module handles is a four-character hint the server
 * derived, and its job is mostly to explain the absence of one.
 */

import { type Tone, formatTimestamp } from "@/lib/agents/presentation";
import type { Credential } from "@/lib/api/credentials";

/**
 * How long before expiry a credential is worth warning about.
 *
 * Seven days: long enough that somebody can act in a working week, short
 * enough that the warning still means something when it appears.
 */
export const EXPIRY_WARNING_DAYS = 7;

const DAY_MS = 24 * 60 * 60 * 1000;

export type CredentialState = "active" | "expiring" | "expired" | "revoked" | "unusable";

/**
 * What state a credential is in, and why.
 *
 * The order matters. `revoked` first, because a revoked credential that also
 * expired is still revoked - that is the act somebody took. Then expiry,
 * which is a fact in the row. Then `unusable`, which is the server saying
 * "no" for a reason this view cannot see: `is_usable` folds in `deleted_at`,
 * which `CredentialView` does not carry. Reporting that as `expired` would be
 * inventing a cause.
 *
 * Expiry is compared against the server's own clock elsewhere, and against
 * `now` here, so a listing can disagree with a dispatch by seconds. Neither
 * is an authentication decision: the provider is the authority on whether its
 * own key still works.
 */
export function credentialState(credential: Credential, now: Date): CredentialState {
  if (credential.revoked_at !== null) return "revoked";

  const expires = credential.expires_at === null ? null : new Date(credential.expires_at);
  if (expires !== null && !Number.isNaN(expires.getTime())) {
    if (expires.getTime() <= now.getTime()) return "expired";
    if (!credential.is_usable) return "unusable";
    if (expires.getTime() - now.getTime() <= EXPIRY_WARNING_DAYS * DAY_MS) return "expiring";
    return "active";
  }

  return credential.is_usable ? "active" : "unusable";
}

export function credentialStateLabel(state: CredentialState): string {
  switch (state) {
    case "active":
      return "usable";
    case "expiring":
      return "expiring soon";
    case "expired":
      return "expired";
    case "revoked":
      return "revoked";
    case "unusable":
      return "not usable";
  }
}

/**
 * The colour each state earns.
 *
 * `revoked` is neutral rather than bad: somebody revoked it on purpose, and
 * colouring a completed action red makes the list look like a problem.
 */
export function credentialStateTone(state: CredentialState): Tone {
  switch (state) {
    case "active":
      return "ok";
    case "expiring":
      return "warn";
    case "expired":
    case "unusable":
      return "bad";
    case "revoked":
      return "neutral";
  }
}

/**
 * The hint, or why there is none - which is the more interesting half.
 *
 * The server derives a hint **only for `api_key` material**, and only when it
 * is long enough that four trailing characters are not a meaningful share of
 * it. Every other kind is refused one deliberately: `basic` material contains
 * a human-chosen password, `oauth2` material is a token set whose last
 * characters belong to whichever field serialised last, and `custom` is by
 * definition unknown. Saying so beats a silent blank, because an operator
 * looking for the hint they see in the provider's dashboard needs to know it
 * is not coming.
 */
export function hintSentence(credential: Credential): string {
  if (credential.display_hint !== null) return `Ends ${credential.display_hint}`;
  if (credential.kind !== "api_key") {
    return "No hint: only API-key material earns one, and the rest would give away more than they show.";
  }
  return "No hint: this material is too short for four characters to be safe to show.";
}

/** When it was last used for an outbound call, or that it has not been. */
export function lastUsedSentence(credential: Credential): string {
  return credential.last_used_at === null
    ? "Never used"
    : `Last used ${formatTimestamp(credential.last_used_at)}`;
}

/** The expiry line, which says "none" rather than nothing. */
export function expirySentence(credential: Credential): string {
  return credential.expires_at === null
    ? "No expiry set"
    : `Expires ${formatTimestamp(credential.expires_at)}`;
}

/** What to say when a workspace has no credentials at all. */
export function credentialsEmptyState(): { readonly headline: string; readonly detail: string } {
  return {
    headline: "No credentials yet",
    detail:
      "A credential is a provider's key, stored encrypted. A model profile points at one, " +
      "and a managed agent runs on that profile - so this is the first of the three.",
  };
}
