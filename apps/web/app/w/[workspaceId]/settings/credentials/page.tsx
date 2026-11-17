import { ApiError } from "@/lib/api/client";
import { listCredentials, type Credential } from "@/lib/api/credentials";
import { listProjects, roleAtLeast, type Project } from "@/lib/api/tenancy";
import { requireSession } from "@/lib/session";

import { CredentialManager } from "./credential-manager";

export const dynamic = "force-dynamic";

export const metadata = { title: "Credentials · AgentWorld" };

/**
 * The provider keys a workspace holds.
 *
 * Admin and above, which is the API's rule for all five credential routes -
 * this page just avoids rendering a form that would be refused. The refusal
 * explains itself rather than 404ing, the way the API keys page does: a
 * viewer who lands here should learn why, not wonder whether the link was
 * broken.
 *
 * **Revoked rows are asked for deliberately.** They cannot be used, and they
 * are exactly what somebody checks when a profile stopped working - so the
 * list includes them, marked, rather than hiding the answer to the question
 * that brought the reader here.
 *
 * Projects are loaded alongside, for the optional pin on a new credential.
 * Failing to load them is not failing to load the page: the pin is optional,
 * so the form degrades to workspace-wide rather than disappearing.
 */
export default async function CredentialsPage({
  params,
}: {
  readonly params: Promise<{ workspaceId: string }>;
}) {
  const { workspaceId } = await params;
  const { token, me } = await requireSession(`/w/${workspaceId}/settings/credentials`);

  const membership = me.workspaces.find((entry) => entry.workspace.id === workspaceId);
  const role = membership?.role ?? "viewer";

  if (!roleAtLeast(role, "admin")) {
    return (
      <section className="stack">
        <div>
          <h1 className="title">Credentials</h1>
        </div>
        <div className="card">
          <h2 className="card__title">Not available to your role</h2>
          <p className="muted">
            Managing credentials needs the admin role; you are a {role}. A credential
            spends money and reaches outside AgentWorld on this workspace&apos;s behalf,
            so who may add one is held more narrowly than who may use the product.
          </p>
        </div>
      </section>
    );
  }

  let credentials: Credential[] = [];
  let projects: Project[] = [];
  let error: string | null = null;
  try {
    credentials = await listCredentials(token, workspaceId, { includeRevoked: true });
  } catch (caught) {
    error = caught instanceof ApiError ? caught.problem.detail : "Could not load credentials";
  }
  try {
    projects = await listProjects(token, workspaceId);
  } catch {
    // The project pin is optional, so a failure here costs the dropdown and
    // nothing else. Reporting it beside the credentials would imply the list
    // above is in doubt, which it is not.
  }

  return (
    <section className="stack">
      <div>
        <h1 className="title">Credentials</h1>
        <p className="muted">
          A provider&apos;s key, stored encrypted under a key of its own. Nothing reads it
          back - not this page, not an owner, not a platform operator - so what you see
          here is metadata and, for an API key, its last four characters.
        </p>
      </div>

      {error !== null && (
        <div className="card">
          <p className="form__error" role="alert">
            {error}
          </p>
        </div>
      )}

      <CredentialManager
        workspaceId={workspaceId}
        credentials={credentials}
        projects={projects}
      />
    </section>
  );
}
