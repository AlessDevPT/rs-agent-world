"use server";

/**
 * Server actions for the Phase 1 forms.
 *
 * They exist so a form can post without a client-side fetch and without an
 * API route that would only forward: the action already runs on the server,
 * where the access token lives in a cookie, so the token never has to reach
 * the browser's JavaScript at all.
 *
 * Each one returns a result object rather than throwing. A thrown error in an
 * action becomes a generic "an error occurred" in production builds, which
 * would discard exactly the part worth showing - the API's problem-details
 * `detail`, written for the person who typed the thing that was rejected.
 */

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";

import { createFailureField } from "@/lib/agents/create";
import { sentFields, unsupportedField, unsupportedNull } from "@/lib/agents/edit";
import {
  archiveAgent,
  createAgent,
  restoreAgent,
  updateAgent,
  type Agent,
  type CreateAgentBody,
  type UpdateAgentBody,
} from "@/lib/api/agents";
import { ApiError, type Problem } from "@/lib/api/client";
import {
  createCredential,
  deleteCredential,
  revokeCredential,
  rotateCredential,
  type Credential,
  type CredentialKind,
} from "@/lib/api/credentials";
import {
  createModelProfile,
  deleteModelProfile,
  updateModelProfile,
  type CreateModelProfileBody,
  type ModelProfile,
  type UpdateModelProfileBody,
} from "@/lib/api/model-profiles";
import {
  createApiKey,
  createProject,
  createWorkspace,
  revokeApiKey,
  type ApiKeyScope,
  type MintedApiKey,
} from "@/lib/api/tenancy";
import { requireSession } from "@/lib/session";

export interface ActionFailure {
  readonly ok: false;
  readonly message: string;
  // `| undefined` explicitly, because `exactOptionalPropertyTypes` draws a
  // distinction between "absent" and "present and undefined", and the
  // failure() helper below produces the latter.
  readonly requestId?: string | undefined;
  /**
   * The request field the API blamed, when it named one.
   *
   * Carried so a form can put the message against the input rather than
   * above the whole card. It is the API's `field` locator verbatim, which
   * means it is the API's spelling of the field - `provider_slug`, not
   * `providerSlug`.
   */
  readonly field?: string | undefined;
  /**
   * The HTTP status the API answered with, when the failure came from it.
   *
   * Absent for a refusal this layer made without asking - an empty PATCH,
   * a missing name - which is the distinction worth keeping: a caller can
   * tell "the server said no" from "we did not bother the server".
   *
   * Carried because the lifecycle actions have refusals that differ only by
   * status. Archiving an agent somebody else archived a moment ago is a 409
   * and arguably a success from the operator's point of view; the same call
   * from a member is a 403 and is not. The sentences are the API's and are
   * worth showing either way, but only the status tells them apart reliably.
   */
  readonly status?: number | undefined;
}

/**
 * `T` defaults to `object` so that an action with no payload is written
 * `ActionResult` rather than `ActionResult<Record<string, never>>` - which
 * does not mean "nothing" at all: intersecting it with `{ ok: true }` makes
 * `ok` incompatible with the index signature and the success case
 * unconstructable.
 */
export type ActionResult<T = object> = ({ readonly ok: true } & T) | ActionFailure;

/** Turn any thrown value into something worth putting on the screen. */
function failure(error: unknown): ActionFailure {
  if (error instanceof ApiError) {
    return {
      ok: false,
      message: problemMessage(error.problem),
      requestId: error.requestId,
      field: error.problem.field,
      status: error.problem.status,
    };
  }
  return {
    ok: false,
    message: error instanceof Error ? error.message : "Something went wrong",
  };
}

/**
 * The sentence to show for a problem document.
 *
 * `detail` alone is the answer for a refusal the application wrote, which is
 * most of them. It is **not** the answer for a schema failure: FastAPI's
 * request-validation handler sets `detail` to "2 field(s) failed validation"
 * and puts the two in `errors`, so showing `detail` alone told somebody that
 * something was wrong and refused to say what.
 *
 * Appended rather than substituted, and only for entries that carry their own
 * `detail` - a workflow publish refusal puts findings of another shape in the
 * same array, and the original sentence is never worth losing.
 */
function problemMessage(problem: Problem): string {
  const described = (problem.errors ?? []).filter(
    (entry) => typeof entry.detail === "string" && entry.detail !== "",
  );
  if (described.length === 0) return problem.detail;
  return `${problem.detail}: ${described
    .map((entry) => `${entry.pointer === "" ? "the request" : entry.pointer} ${entry.detail}`)
    .join("; ")}`;
}

function text(form: FormData, field: string): string {
  const value = form.get(field);
  return typeof value === "string" ? value.trim() : "";
}

// ------------------------------------------------------------- workspaces ---

export async function createWorkspaceAction(form: FormData): Promise<ActionResult> {
  const name = text(form, "name");
  if (!name) return { ok: false, message: "A workspace needs a name" };

  const { token } = await requireSession();
  let workspaceId: string;
  try {
    const slug = text(form, "slug");
    const workspace = await createWorkspace(token, {
      name,
      ...(slug ? { slug } : {}),
    });
    workspaceId = workspace.id;
  } catch (error) {
    return failure(error);
  }

  // Outside the try: `redirect` works by throwing, so catching around it
  // would turn a successful navigation into a reported failure.
  redirect(`/w/${workspaceId}`);
}

// ---------------------------------------------------------------- projects ---

export async function createProjectAction(
  workspaceId: string,
  form: FormData,
): Promise<ActionResult> {
  const name = text(form, "name");
  if (!name) return { ok: false, message: "A project needs a name" };

  const { token } = await requireSession();
  try {
    const slug = text(form, "slug");
    const description = text(form, "description");
    await createProject(token, workspaceId, {
      name,
      ...(slug ? { slug } : {}),
      ...(description ? { description } : {}),
    });
  } catch (error) {
    return failure(error);
  }

  revalidatePath(`/w/${workspaceId}`);
  return { ok: true };
}

// ---------------------------------------------------------------- api keys ---

export async function createApiKeyAction(
  workspaceId: string,
  form: FormData,
): Promise<ActionResult<{ minted: MintedApiKey }>> {
  const name = text(form, "name");
  if (!name) return { ok: false, message: "A key needs a name" };

  const scope = text(form, "scope") as ApiKeyScope;
  const projectId = text(form, "project_id");
  const allowlist = text(form, "agent_key_allowlist")
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);

  const { token } = await requireSession();
  try {
    const minted = await createApiKey(token, workspaceId, {
      name,
      scope,
      project_id: projectId || null,
      // An empty list would mean "no agent may use this key", which the API
      // rejects. Omitting the field is how you say "any agent".
      agent_key_allowlist: allowlist.length > 0 ? allowlist : null,
    });
    revalidatePath(`/w/${workspaceId}/keys`);
    // The plaintext is returned to the caller exactly once, and the caller is
    // a client component that shows it and then forgets it. It is never
    // written to a cookie, a cache or the URL.
    return { ok: true, minted };
  } catch (error) {
    return failure(error);
  }
}

export async function revokeApiKeyAction(
  workspaceId: string,
  apiKeyId: string,
): Promise<ActionResult> {
  const { token } = await requireSession();
  try {
    await revokeApiKey(token, apiKeyId);
  } catch (error) {
    return failure(error);
  }
  revalidatePath(`/w/${workspaceId}/keys`);
  return { ok: true };
}

// ------------------------------------------------------------ credentials ---
//
// **The secret arrives here and stops here.** A server action runs on the
// server, so material typed into the form reaches this process and the API
// over the server-to-server call - it is never held in client state, never
// put in a URL, and never returned: every action below answers with metadata
// or with nothing at all. There is no API route that could echo it back even
// if one of these asked.

export async function createCredentialAction(
  workspaceId: string,
  form: FormData,
): Promise<ActionResult<{ credential: Credential }>> {
  const name = text(form, "name");
  if (!name) return { ok: false, message: "A credential needs a name" };

  const providerSlug = text(form, "provider_slug");
  if (!providerSlug) return { ok: false, message: "Name the provider this key belongs to" };

  // Not trimmed: leading or trailing whitespace can be part of material, and
  // silently removing it would store a key that fails for a reason nobody
  // could see. The API refuses an empty one.
  const secret = form.get("secret");
  if (typeof secret !== "string" || secret === "") {
    return { ok: false, message: "There is no material to store" };
  }

  const projectId = text(form, "project_id");
  const expiresAt = text(form, "expires_at");
  const scopes = text(form, "scopes")
    .split(",")
    .map((entry) => entry.trim())
    .filter(Boolean);

  const { token } = await requireSession();
  try {
    const credential = await createCredential(token, workspaceId, {
      name,
      kind: text(form, "kind") as CredentialKind,
      provider_slug: providerSlug,
      secret,
      project_id: projectId || null,
      scopes: scopes.length > 0 ? scopes : null,
      // A date input gives `2026-12-01`; the API wants a timestamp. End of
      // that day in UTC, so "expires on the first" does not expire at
      // midnight the night before for anyone west of here.
      expires_at: expiresAt ? `${expiresAt}T23:59:59Z` : null,
    });
    revalidatePath(`/w/${workspaceId}/settings/credentials`);
    return { ok: true, credential };
  } catch (error) {
    return failure(error);
  }
}

export async function rotateCredentialAction(
  workspaceId: string,
  credentialId: string,
  form: FormData,
): Promise<ActionResult<{ credential: Credential }>> {
  const secret = form.get("secret");
  if (typeof secret !== "string" || secret === "") {
    return { ok: false, message: "There is no replacement material to store" };
  }
  const expiresAt = text(form, "expires_at");

  const { token } = await requireSession();
  try {
    const credential = await rotateCredential(token, workspaceId, credentialId, {
      secret,
      expires_at: expiresAt ? `${expiresAt}T23:59:59Z` : null,
    });
    revalidatePath(`/w/${workspaceId}/settings/credentials`);
    return { ok: true, credential };
  } catch (error) {
    return failure(error);
  }
}

export async function revokeCredentialAction(
  workspaceId: string,
  credentialId: string,
): Promise<ActionResult> {
  const { token } = await requireSession();
  try {
    await revokeCredential(token, workspaceId, credentialId);
  } catch (error) {
    return failure(error);
  }
  revalidatePath(`/w/${workspaceId}/settings/credentials`);
  return { ok: true };
}

export async function deleteCredentialAction(
  workspaceId: string,
  credentialId: string,
): Promise<ActionResult> {
  const { token } = await requireSession();
  try {
    await deleteCredential(token, workspaceId, credentialId);
  } catch (error) {
    return failure(error);
  }
  revalidatePath(`/w/${workspaceId}/settings/credentials`);
  return { ok: true };
}

// --------------------------------------------------------- model profiles ---
//
// **Typed bodies rather than FormData.** The credential actions above take a
// form because material has to reach the server without passing through
// client state. A profile holds no secret, and its PATCH body is a decision
// rather than a transcription: which fields moved, and whether a cleared
// credential means `null` or "leave it alone". That decision is `profileEdits`
// in `lib/model-profiles/presentation.ts`, where it is tested; these actions
// carry its answer to the API and add nothing to it.
//
// The role is the API's to enforce either way. All three routes are `admin`,
// and the page's own gate only decides whether a form is worth rendering - a
// body posted at one of these actions without the role is refused by the
// endpoint, not by the absence of a button.

export async function createModelProfileAction(
  workspaceId: string,
  body: CreateModelProfileBody,
): Promise<ActionResult<{ profile: ModelProfile }>> {
  const { token } = await requireSession();
  try {
    const profile = await createModelProfile(token, workspaceId, body);
    revalidatePath(`/w/${workspaceId}/settings/model-profiles`);
    return { ok: true, profile };
  } catch (error) {
    return failure(error);
  }
}

/**
 * Change a profile, including making it the workspace default.
 *
 * An empty body is refused here rather than sent. `ModelProfileService.update`
 * accepts one and records an audit row for it, so a PATCH that writes no
 * column would still leave a trail of an edit that never happened.
 */
export async function updateModelProfileAction(
  workspaceId: string,
  profileId: string,
  body: UpdateModelProfileBody,
): Promise<ActionResult<{ profile: ModelProfile }>> {
  if (Object.keys(body).length === 0) {
    return { ok: false, message: "Nothing was changed" };
  }

  const { token } = await requireSession();
  try {
    const profile = await updateModelProfile(token, workspaceId, profileId, body);
    revalidatePath(`/w/${workspaceId}/settings/model-profiles`);
    return { ok: true, profile };
  } catch (error) {
    return failure(error);
  }
}

/**
 * Delete a profile.
 *
 * A 409 here is the API refusing to un-configure the agents that use it. The
 * failure is returned rather than swallowed, and the caller leaves the row on
 * screen: a profile that is still in use has not gone anywhere.
 */
export async function deleteModelProfileAction(
  workspaceId: string,
  profileId: string,
): Promise<ActionResult> {
  const { token } = await requireSession();
  try {
    await deleteModelProfile(token, workspaceId, profileId);
  } catch (error) {
    return failure(error);
  }
  revalidatePath(`/w/${workspaceId}/settings/model-profiles`);
  return { ok: true };
}

// ----------------------------------------------------------------- agents ---

/**
 * Register an agent into a project.
 *
 * **Returns only on failure.** A successful create ends in `redirect` to the
 * new agent's own page, which is the honest success state: the thing that was
 * asked for now exists and the person is looking at it. `createWorkspaceAction`
 * above does the same, for the same reason, and the `redirect` call sits
 * outside the `try` because it works by throwing - catching around it would
 * report a completed creation as a failure.
 *
 * The body is built by `lib/agents/create.ts` and sent as it arrives. Two
 * things that would be tempting to do here are deliberately not done: nothing
 * is filled in on the caller's behalf (a blank optional field was omitted by
 * the builder and must stay omitted, or it would be stored as an empty
 * string), and the role is not checked (`agent.create` is `member` and
 * `agent.configure` is `admin`, both enforced by the endpoint - a form that
 * hides a control is a convenience, not a boundary).
 */
export async function createAgentAction(
  workspaceId: string,
  projectId: string,
  body: CreateAgentBody,
): Promise<ActionResult> {
  const { token } = await requireSession();

  let agentId: string;
  try {
    const agent = await createAgent(token, projectId, body);
    agentId = agent.id;
  } catch (error) {
    const result = failure(error);
    if (error instanceof ApiError) {
      // The API locates only one of this endpoint's refusals. The rest of
      // the mapping is a rule about statuses, kept in `lib/agents/create.ts`
      // where it is tested, rather than a sentence matched here.
      const field = createFailureField(error.problem.status, error.problem.field);
      if (field !== undefined) return { ...result, field };
    }
    return result;
  }

  revalidatePath(`/w/${workspaceId}/p/${projectId}/agents`);
  redirect(`/w/${workspaceId}/p/${projectId}/agents/${agentId}`);
}

// ------------------------------------------------- editing and lifecycle ---
//
// **Typed bodies, like the model-profile actions and unlike the credential
// ones.** Nothing here is a secret that must reach the server without
// touching client state, and an agent's PATCH body is a decision - which
// fields moved, and whether an empty box means `null` or "leave it alone".
// That decision is `agentEdits` in `lib/agents/edit.ts`, where it is tested;
// these actions carry its answer and add nothing to it.
//
// **Two routes are revalidated after every success, never before one.** The
// agent's own page is the obvious one; the project's list carries the name,
// the role and the archived state, so an edit or a lifecycle change that
// refreshed only the detail page would leave the list telling a story the
// detail page had already corrected. A failed call revalidates neither:
// discarding cached data for a request that changed nothing is work for no
// reason, and it implies to the reader that something happened.
//
// The roles are the API's. `agent.update` is `member`; naming a model
// profile is also `agent.configure`, and archiving and restoring are
// `agent.archive` and `agent.restore` - all three `admin`. A page that hides
// a button is a convenience, and these actions do not re-implement the
// boundary: an unauthorised call reaches the endpoint and comes back a 403,
// which is returned rather than translated.

function agentPaths(workspaceId: string, projectId: string, agentId: string): readonly string[] {
  const list = `/w/${workspaceId}/p/${projectId}/agents`;
  return [`${list}/${agentId}`, list];
}

/**
 * Change an agent's name, role, description, instructions, kind, labels, or
 * the two pieces of configuration this milestone offers - its model profile
 * and its heartbeat limit.
 *
 * **An empty body is refused here rather than sent**, exactly as
 * `updateModelProfileAction` refuses one: `AgentService.update` accepts a
 * PATCH that writes no column and records an `agent.update` audit row for
 * it, so forwarding one would leave a trail of edits that never happened.
 *
 * **A field outside this milestone's set is refused, not dropped.** The
 * endpoint would also accept `interaction_mode`, `model_overrides` and
 * `dispatch`; an action that silently removed them would answer "saved" to a
 * request it did not carry out. `UpdateAgentBody` keeps them out of the
 * type, and `unsupportedField` keeps them out of the request, because an
 * action is reachable with whatever a browser posts.
 *
 * The role split is the API's and is not re-implemented here: a member who
 * sends `model_profile_id` or `heartbeat_ttl_seconds` gets a 403 for the
 * whole request. The form declines to draw those two for them; this carries
 * whatever arrives and reports what the endpoint says about it.
 *
 * **A `null` for a `NOT NULL` column is refused here too**, with the field
 * the API would have named. That check is not this layer inventing a rule -
 * it is the same rule, answered one step earlier, and the body it stops is
 * one `UpdateAgentBody` already makes unconstructable in TypeScript.
 *
 * Everything else the API refuses comes back as it was written: the 403 for
 * a member naming a model profile, the 409 for an archived agent, the 409
 * for a profile deleted mid-request and the 422 for one from another
 * workspace - the last two both carrying `field: model_profile_id`, which
 * `failure` keeps so a form can put the message on the select.
 */
export async function updateAgentAction(
  workspaceId: string,
  projectId: string,
  agentId: string,
  body: UpdateAgentBody,
): Promise<ActionResult<{ agent: Agent }>> {
  const unsupported = unsupportedField(body);
  if (unsupported !== undefined) {
    return {
      ok: false,
      message: `'${unsupported}' is not a field this form edits`,
      field: unsupported,
    };
  }
  const nulled = unsupportedNull(body);
  if (nulled !== undefined) {
    return { ok: false, message: `'${nulled}' cannot be null`, field: nulled };
  }
  if (sentFields(body).length === 0) {
    return { ok: false, message: "Nothing was changed" };
  }

  const { token } = await requireSession();
  try {
    const agent = await updateAgent(token, projectId, agentId, body);
    for (const path of agentPaths(workspaceId, projectId, agentId)) revalidatePath(path);
    return { ok: true, agent };
  } catch (error) {
    return failure(error);
  }
}

/**
 * Archive an agent. `admin`.
 *
 * Archived, not deleted: the agent keeps its runs, its history and its place
 * on the island, and `restoreAgentAction` is the way back. There is no
 * action that deletes one, because there is no endpoint that does.
 *
 * **A 409 is returned, not smoothed over.** It means the agent is already
 * archived - somebody else got there first, or a button was pressed twice -
 * and the caller decides whether that is worth showing. Turning it into
 * `{ ok: true }` here would be this layer claiming to have done something it
 * did not do.
 */
export async function archiveAgentAction(
  workspaceId: string,
  projectId: string,
  agentId: string,
): Promise<ActionResult> {
  const { token } = await requireSession();
  try {
    await archiveAgent(token, projectId, agentId);
  } catch (error) {
    return failure(error);
  }
  for (const path of agentPaths(workspaceId, projectId, agentId)) revalidatePath(path);
  return { ok: true };
}

/**
 * Restore an archived agent. `admin`.
 *
 * The agent comes back with the status and heartbeat it had when it was
 * archived - nothing derived is rewritten - so the returned agent is worth
 * handing back to the caller rather than discarding: `liveness` and
 * `seconds_since_heartbeat` on it are how a reader judges whether it is
 * really back, and they are measured by the server against the same instant.
 *
 * **A 404 here is not necessarily a missing agent.** The service answers one
 * for a live agent, an agent in another project and an unknown id alike,
 * deliberately. It is returned as it arrived; a caller that rephrased it as
 * "this agent does not exist" would be guessing at which of the three.
 */
export async function restoreAgentAction(
  workspaceId: string,
  projectId: string,
  agentId: string,
): Promise<ActionResult<{ agent: Agent }>> {
  const { token } = await requireSession();
  let agent: Agent;
  try {
    agent = await restoreAgent(token, projectId, agentId);
  } catch (error) {
    return failure(error);
  }
  for (const path of agentPaths(workspaceId, projectId, agentId)) revalidatePath(path);
  return { ok: true, agent };
}
