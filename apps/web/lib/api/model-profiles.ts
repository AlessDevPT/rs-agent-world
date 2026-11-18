/**
 * Typed calls for the model-profile endpoints.
 *
 * A model profile is the row that says *which model a managed agent calls and
 * with whose key*, so it sits between the two things that already have
 * screens: a credential it may point at, and an agent that points at it.
 *
 * **Reading is `viewer`; every write is `admin`.** That asymmetry is the
 * API's (`test_role_matrix.py` rows for these four routes) and the reason
 * `listModelProfiles` is called for anyone who can see the workspace while
 * the other three are only ever reached from an admin's form.
 *
 * Nested under the workspace, including the item routes, for the reason
 * `credentials.ts` gives: resolving a profile id to a workspace before
 * authorizing the caller would be a cross-tenant lookup.
 *
 * Hand-written and deliberately thin, like `credentials.ts` and `agents.ts` -
 * Phase 5 replaces all three with a generated client.
 */

import type { JsonObject } from "./agents";
import { apiFetch } from "./client";

/**
 * A model profile, in full. There is nothing withheld here.
 *
 * `credential_id` names a credential whose material nothing in the
 * application can read; the id is the whole of what this view carries about
 * it, and turning that id into something an operator recognises is
 * `listCredentials`' job, not this one's.
 *
 * `parameters` is an open object rather than a known shape, because the API
 * stores it as one. Two keys in it are read at dispatch - `base_url` and
 * `max_output_tokens`, in `dispatch._plan` - and every other key is carried
 * untouched, so a form that edits this must preserve what it does not
 * understand.
 */
export interface ModelProfile {
  readonly id: string;
  readonly name: string;
  readonly provider_slug: string;
  readonly model_id: string;
  readonly parameters: JsonObject;
  readonly credential_id: string | null;
  readonly is_default: boolean;
}

export interface CreateModelProfileBody {
  readonly name: string;
  readonly provider_slug: string;
  readonly model_id: string;
  readonly parameters?: JsonObject;
  readonly credential_id?: string | null;
  readonly is_default?: boolean;
}

/**
 * A PATCH body: absent means "leave it", and only `credential_id` is
 * nullable.
 *
 * The optionality here is load-bearing and matches `ModelProfileUpdate` on
 * the API, which distinguishes an absent field from an explicit `null` by
 * `model_fields_set`. A `null` for anything but `credential_id` is a 422
 * naming the field, so the types below allow it nowhere else - and building
 * one of these is `profileEdits` in `lib/model-profiles/presentation.ts`,
 * which is where the absent-versus-null decision is made and tested.
 */
export interface UpdateModelProfileBody {
  readonly name?: string;
  readonly provider_slug?: string;
  readonly model_id?: string;
  readonly parameters?: JsonObject;
  /** `null` detaches the credential. Omit to leave it attached. */
  readonly credential_id?: string | null;
  readonly is_default?: boolean;
}

/** Every profile in the workspace. `viewer` and above. */
export function listModelProfiles(token: string, workspaceId: string): Promise<ModelProfile[]> {
  return apiFetch<ModelProfile[]>(`/v1/workspaces/${workspaceId}/model-profiles`, { token });
}

/**
 * Create one. `admin`, and 422 on `provider_slug` for a provider this build
 * cannot serve or on `credential_id` for a credential issued for another.
 */
export function createModelProfile(
  token: string,
  workspaceId: string,
  body: CreateModelProfileBody,
): Promise<ModelProfile> {
  return apiFetch<ModelProfile>(`/v1/workspaces/${workspaceId}/model-profiles`, {
    token,
    method: "POST",
    body,
  });
}

/**
 * Re-point one: its name, model, parameters, key or default-ness.
 *
 * The API checks the provider and the credential against the row the request
 * would leave behind rather than the fields it mentions, so moving either
 * half re-checks the pair.
 */
export function updateModelProfile(
  token: string,
  workspaceId: string,
  profileId: string,
  body: UpdateModelProfileBody,
): Promise<ModelProfile> {
  return apiFetch<ModelProfile>(
    `/v1/workspaces/${workspaceId}/model-profiles/${profileId}`,
    { token, method: "PATCH", body },
  );
}

/**
 * Delete one. 204, and nothing to parse.
 *
 * A profile still used by an agent is a 409 rather than a cascade: the API
 * refuses rather than silently un-configuring every agent that ran on it.
 */
export function deleteModelProfile(
  token: string,
  workspaceId: string,
  profileId: string,
): Promise<void> {
  return apiFetch<void>(`/v1/workspaces/${workspaceId}/model-profiles/${profileId}`, {
    token,
    method: "DELETE",
  });
}
