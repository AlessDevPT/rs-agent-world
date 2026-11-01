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

import { ApiError } from "@/lib/api/client";
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
    return { ok: false, message: error.problem.detail, requestId: error.requestId };
  }
  return {
    ok: false,
    message: error instanceof Error ? error.message : "Something went wrong",
  };
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
