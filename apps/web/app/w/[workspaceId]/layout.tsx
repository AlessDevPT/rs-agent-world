import Link from "next/link";
import { notFound } from "next/navigation";

import { requireSession } from "@/lib/session";

import { WorkspaceSwitcher } from "./workspace-switcher";

export const dynamic = "force-dynamic";

/**
 * The authenticated shell: workspace switcher, nav, sign out.
 *
 * The workspace id in the path is checked against the caller's own
 * memberships here rather than trusted. The API would 404 it anyway - every
 * endpoint is tenant-scoped - but checking once in the layout means the
 * switcher and nav are never rendered for a workspace the viewer cannot see.
 */
export default async function WorkspaceLayout({
  children,
  params,
}: {
  readonly children: React.ReactNode;
  readonly params: Promise<{ workspaceId: string }>;
}) {
  const { workspaceId } = await params;
  const { me } = await requireSession(`/w/${workspaceId}`);

  const membership = me.workspaces.find((entry) => entry.workspace.id === workspaceId);
  // Not 403: a workspace the viewer is not a member of is indistinguishable
  // from one that does not exist, which is the same rule the API applies.
  if (membership === undefined) notFound();

  return (
    <div className="stack">
      <div className="workspace-bar">
        <WorkspaceSwitcher
          current={membership.workspace}
          memberships={me.workspaces}
          role={membership.role}
        />

        <nav className="workspace-bar__nav" aria-label="Workspace">
          <Link className="workspace-bar__link" href={`/w/${workspaceId}`}>
            Projects
          </Link>
          <Link className="workspace-bar__link" href={`/w/${workspaceId}/keys`}>
            API keys
          </Link>
        </nav>

        <div className="workspace-bar__user">
          <span className="muted">{me.user.email}</span>
          {/* A form, not a link: signing out is a POST so that no other site
              can trigger it with an image tag. */}
          <form action="/auth/signout" method="post">
            <button className="button button--quiet" type="submit">
              Sign out
            </button>
          </form>
        </div>
      </div>

      {children}
    </div>
  );
}
