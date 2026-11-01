import { ApiError } from "@/lib/api/client";
import {
  listApiKeys,
  listProjects,
  roleAtLeast,
  type ApiKey,
  type Project,
} from "@/lib/api/tenancy";
import { requireSession } from "@/lib/session";

import { ApiKeyManager } from "./api-key-manager";

export const dynamic = "force-dynamic";

export const metadata = { title: "API keys · AgentWorld" };

/**
 * API keys for one workspace.
 *
 * Admin and above only, which is the API's rule too - this page just avoids
 * rendering a form that would be refused.
 */
export default async function ApiKeysPage({
  params,
}: {
  readonly params: Promise<{ workspaceId: string }>;
}) {
  const { workspaceId } = await params;
  const { token, me } = await requireSession(`/w/${workspaceId}/keys`);

  const membership = me.workspaces.find((entry) => entry.workspace.id === workspaceId);
  const role = membership?.role ?? "viewer";

  if (!roleAtLeast(role, "admin")) {
    return (
      <section className="stack">
        <div>
          <h1 className="title">API keys</h1>
        </div>
        <div className="card">
          <h2 className="card__title">Not available to your role</h2>
          <p className="muted">
            Managing API keys needs the admin role; you are a {role}. Keys can act on
            this workspace&apos;s data, so the ability to mint them is held more
            narrowly than the ability to use the product.
          </p>
        </div>
      </section>
    );
  }

  let keys: ApiKey[] = [];
  let projects: Project[] = [];
  let error: string | null = null;
  try {
    [keys, projects] = await Promise.all([
      listApiKeys(token, workspaceId),
      listProjects(token, workspaceId),
    ]);
  } catch (caught) {
    error = caught instanceof ApiError ? caught.problem.detail : "Could not load keys";
  }

  return (
    <section className="stack">
      <div>
        <h1 className="title">API keys</h1>
        <p className="muted">
          A key is how a monitored agent talks to AgentWorld. Only the prefix is stored;
          the secret half is hashed and cannot be shown again.
        </p>
      </div>

      {error !== null && (
        <div className="card">
          <p className="form__error" role="alert">
            {error}
          </p>
        </div>
      )}

      <ApiKeyManager workspaceId={workspaceId} initialKeys={keys} projects={projects} />
    </section>
  );
}
