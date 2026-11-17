import Link from "next/link";

export const dynamic = "force-dynamic";

/**
 * The settings shell: workspace-wide configuration, grouped.
 *
 * **Workspace-scoped, not project-scoped**, which is a correction to
 * `docs/proposals/2026-11-ecosystem-builder/03-API-AND-FRONTEND.md` §6. That
 * tree draws `settings` under a project, but every endpoint behind these
 * screens is nested under the workspace - credentials, model profiles,
 * members and API keys alike - so a project-scoped URL would imply a scope
 * the data does not have, and two projects would show the same credential
 * list under different addresses.
 *
 * It adds no access check of its own. The workspace layout above has already
 * refused a workspace the viewer is not a member of, and each page below
 * holds its own `admin` gate - which is the arrangement the project layout
 * documents and relies on: a layout and its page execute concurrently, so a
 * check here would not gate anything underneath it.
 *
 * API keys are linked rather than moved. Their page predates this group and
 * its path is reached from elsewhere; pointing at it costs nothing and says
 * where it belongs, while moving it is a separate change with a redirect in
 * it.
 */
export default async function SettingsLayout({
  children,
  params,
}: {
  readonly children: React.ReactNode;
  readonly params: Promise<{ workspaceId: string }>;
}) {
  const { workspaceId } = await params;

  return (
    <div className="stack">
      <nav className="settings-nav" aria-label="Settings">
        <Link className="workspace-bar__link" href={`/w/${workspaceId}/settings/credentials`}>
          Credentials
        </Link>
        <Link className="workspace-bar__link" href={`/w/${workspaceId}/keys`}>
          API keys
        </Link>
      </nav>

      {children}
    </div>
  );
}
