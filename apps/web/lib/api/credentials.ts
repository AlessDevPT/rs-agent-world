/**
 * Typed calls for the credential endpoints.
 *
 * **Five ways in and no way out, and this file is the same shape as the API.**
 * There is no `getCredential`, no field on `Credential` for material, and
 * nothing here that could carry a secret back: the API has no route that
 * returns one and no permission at any role that would, so a client function
 * for it would be a function that cannot exist. The only direction material
 * travels is into `createCredential` and `rotateCredential`.
 *
 * Everything is nested under the workspace, including the item routes. That
 * is the API's shape and its reason: resolving a credential id to a workspace
 * before authorizing the caller would be a cross-tenant lookup, and the
 * workspace is already in the UI's hands wherever these are called.
 *
 * Hand-written and deliberately thin, like `tenancy.ts` and `agents.ts`.
 */

import { apiFetch } from "./client";

/** What kind of material a credential holds. */
export type CredentialKind = "api_key" | "oauth2" | "basic" | "custom";

/**
 * A credential, as an operator may see it.
 *
 * Enough to tell two keys for the same provider apart, to see which is about
 * to expire and which is no longer used - and nothing that could be replayed
 * against the provider.
 *
 * `display_hint` is derived server-side and **only for `api_key` material**:
 * the last four characters of an opaque high-entropy token, which is what the
 * provider's own dashboard shows and therefore how a row here is matched
 * against one there. Every other kind gets `null` rather than a worse hint -
 * `basic` material contains a human-chosen password, and four trailing
 * characters of one are a real contribution to guessing it.
 *
 * `key_id` names the key-encryption key the row is wrapped under. It is
 * operational metadata, not material: an operator halfway through a rotation
 * needs to see which rows have moved.
 */
export interface Credential {
  readonly id: string;
  readonly workspace_id: string;
  /** Set when the credential is pinned to one project rather than the workspace. */
  readonly project_id: string | null;
  readonly name: string;
  readonly kind: CredentialKind;
  readonly provider_slug: string;
  readonly key_id: string;
  readonly display_hint: string | null;
  readonly scopes: readonly string[] | null;
  readonly expires_at: string | null;
  readonly refresh_after: string | null;
  readonly created_at: string | null;
  readonly last_used_at: string | null;
  readonly revoked_at: string | null;
  /**
   * Whether an outbound call may still use it, by this server's clock.
   *
   * Not an authentication decision: the provider is the authority on whether
   * its own key still works, and a credential that is `is_usable` can still
   * be refused by them.
   */
  readonly is_usable: boolean;
}

export interface CreateCredentialBody {
  readonly name: string;
  readonly kind: CredentialKind;
  readonly provider_slug: string;
  /** Write-only. No endpoint returns it and no role may read it back. */
  readonly secret: string;
  readonly project_id?: string | null;
  readonly scopes?: readonly string[] | null;
  readonly expires_at?: string | null;
}

export interface RotateCredentialBody {
  readonly secret: string;
  readonly expires_at?: string | null;
}

/**
 * The credentials in a workspace, metadata only.
 *
 * Revoked rows are excluded unless asked for: they cannot be used, but they
 * are what an operator checks when something stopped working.
 */
export function listCredentials(
  token: string,
  workspaceId: string,
  options: { readonly includeRevoked?: boolean } = {},
): Promise<Credential[]> {
  const query = options.includeRevoked === true ? "?include_revoked=true" : "";
  return apiFetch<Credential[]>(`/v1/workspaces/${workspaceId}/credentials${query}`, { token });
}

/** Store material. The 201 carries metadata and a hint; it does not echo the secret. */
export function createCredential(
  token: string,
  workspaceId: string,
  body: CreateCredentialBody,
): Promise<Credential> {
  return apiFetch<Credential>(`/v1/workspaces/${workspaceId}/credentials`, {
    token,
    method: "POST",
    body,
  });
}

/**
 * Replace the material behind an existing credential.
 *
 * The id, the name and everything pointing at it stay as they are, which is
 * the point: a model profile naming this credential keeps working across a
 * rotation without being edited.
 */
export function rotateCredential(
  token: string,
  workspaceId: string,
  credentialId: string,
  body: RotateCredentialBody,
): Promise<Credential> {
  return apiFetch<Credential>(
    `/v1/workspaces/${workspaceId}/credentials/${credentialId}/rotate`,
    { token, method: "POST", body },
  );
}

/** Stop it being usable, keeping the row and its history. */
export function revokeCredential(
  token: string,
  workspaceId: string,
  credentialId: string,
): Promise<Credential> {
  return apiFetch<Credential>(
    `/v1/workspaces/${workspaceId}/credentials/${credentialId}/revoke`,
    { token, method: "POST" },
  );
}

/** Destroy the material. 204, and nothing to parse. */
export function deleteCredential(
  token: string,
  workspaceId: string,
  credentialId: string,
): Promise<void> {
  return apiFetch<void>(`/v1/workspaces/${workspaceId}/credentials/${credentialId}`, {
    token,
    method: "DELETE",
  });
}
