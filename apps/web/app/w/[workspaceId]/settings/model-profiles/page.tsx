import { ApiError, getMeta } from "@/lib/api/client";
import { listCredentials, type Credential } from "@/lib/api/credentials";
import { listModelProfiles, type ModelProfile } from "@/lib/api/model-profiles";
import { canManageProfiles, readOnlyNotice } from "@/lib/model-profiles/presentation";
import { requireSession } from "@/lib/session";

import { ModelProfileManager } from "./model-profile-manager";

export const dynamic = "force-dynamic";

export const metadata = { title: "Model profiles · AgentWorld" };

/**
 * Which model a workspace's managed agents call, and with whose key.
 *
 * **Readable by any member, editable by an admin**, which is the API's own
 * split: `GET` on these routes is `viewer` and the other three are `admin`.
 * So this page does not refuse a non-admin the way the credentials page does
 * - it shows the list and says why the forms are missing. A viewer looking at
 * an agent that will not run should be able to see what it is configured to
 * call.
 *
 * Three loads, and only the first one is the page. The profiles are what this
 * screen is; the provider list and the credentials are what the forms need,
 * and either failing costs a form rather than the page. They are also both
 * `null`-when-unknown rather than empty-when-unknown, because "no credentials
 * exist" and "we could not ask" are different sentences and the second must
 * never be shown as the first.
 */
export default async function ModelProfilesPage({
  params,
}: {
  readonly params: Promise<{ workspaceId: string }>;
}) {
  const { workspaceId } = await params;
  const { token, me } = await requireSession(`/w/${workspaceId}/settings/model-profiles`);

  const membership = me.workspaces.find((entry) => entry.workspace.id === workspaceId);
  const role = membership?.role ?? "viewer";
  const mayManage = canManageProfiles(role);

  let profiles: ModelProfile[] = [];
  let error: string | null = null;
  try {
    profiles = await listModelProfiles(token, workspaceId);
  } catch (caught) {
    error = caught instanceof ApiError ? caught.problem.detail : "Could not load model profiles";
  }

  let providers: readonly string[] | null = null;
  try {
    providers = (await getMeta()).providers;
  } catch {
    // The provider list is what the create form offers as choices. Without
    // it the form cannot be filled honestly, so the manager says so rather
    // than offering a free-text box the API would refuse.
  }

  // Listing credentials is `admin` on the API, so a viewer is never asked for
  // them - a 403 logged on every page view would be noise about a request
  // that should not have been made.
  let credentials: readonly Credential[] | null = null;
  if (mayManage) {
    try {
      // Revoked rows included: a revoked credential may still be attached to
      // a profile, deliberately, and a row that could not be shown in its own
      // edit form would fall back to the first option and detach itself.
      credentials = await listCredentials(token, workspaceId, { includeRevoked: true });
    } catch {
      // Left null: the forms degrade to "cannot offer credentials" instead of
      // claiming the workspace has none.
    }
  }

  return (
    <section className="stack">
      <div>
        <h1 className="title">Model profiles</h1>
        <p className="muted">
          A profile names the provider and model a managed agent calls, and the credential
          it calls with. The provider must be one this build serves, and the credential
          must be one issued for that same provider.
        </p>
      </div>

      {error !== null && (
        <div className="card">
          <p className="form__error" role="alert">
            {error}
          </p>
        </div>
      )}

      {!mayManage && (
        <div className="card">
          <h2 className="card__title">Read only for your role</h2>
          <p className="muted">{readOnlyNotice(role)}</p>
        </div>
      )}

      <ModelProfileManager
        workspaceId={workspaceId}
        profiles={profiles}
        providers={providers}
        credentials={credentials}
        mayManage={mayManage}
      />
    </section>
  );
}
