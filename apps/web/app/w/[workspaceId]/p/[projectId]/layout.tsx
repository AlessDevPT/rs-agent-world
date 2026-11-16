import Link from "next/link";
import { notFound } from "next/navigation";

import { getProject } from "@/lib/api/agents";
import { ApiError } from "@/lib/api/client";
import { requireSession } from "@/lib/session";

export const dynamic = "force-dynamic";

/**
 * The project shell: everything under one project hangs off this.
 *
 * Two access checks, and they catch different things. The membership check is
 * the same one the workspace layout makes - a workspace the viewer does not
 * belong to is `notFound()`, never 403, because "you may not see this" and
 * "this does not exist" must be indistinguishable to someone guessing ids.
 * The project fetch is the second: the API is tenant-scoped, so a project in
 * someone else's workspace 404s on its own, and the explicit `workspace_id`
 * comparison closes the remaining case - a project the viewer really can read,
 * reached through the wrong workspace in the path. Letting that render would
 * show one workspace's data under another's name.
 *
 * A failure that is not a 404 is re-thrown rather than rendered: an outage
 * belongs on an error boundary, not behind a shell that looks like a working
 * project. "The API is down" is not the same answer as "no such project".
 *
 * **None of this gates the page below.** A layout and its page execute
 * concurrently - `notFound()` here decides what is sent to the browser, not
 * what runs underneath - so every page under this one repeats the checks it
 * depends on. These exist for the shell this layout itself renders.
 *
 * The path passed to `requireSession` is the deepest route this layout can
 * name, and that is a limit rather than a choice: a layout receives its own
 * params and its ancestors', never a child's, so `agentId` does not arrive
 * here - and a server layout has no pathname to read either.
 *
 * It no longer decides where a signed-out visitor returns to, and does not
 * need to. Middleware forwards the real destination in a request header and
 * `requireSession` prefers it, so whichever layer redirects first now builds
 * the same `?next=` - the page that was actually asked for. This argument is
 * the fallback for a render the header never reached.
 */
export default async function ProjectLayout({
  children,
  params,
}: {
  readonly children: React.ReactNode;
  readonly params: Promise<{ workspaceId: string; projectId: string }>;
}) {
  const { workspaceId, projectId } = await params;
  const { token, me } = await requireSession(`/w/${workspaceId}/p/${projectId}/agents`);

  const membership = me.workspaces.find((entry) => entry.workspace.id === workspaceId);
  if (membership === undefined) notFound();

  let project;
  try {
    project = await getProject(token, projectId);
  } catch (caught) {
    if (caught instanceof ApiError && caught.problem.status === 404) notFound();
    throw caught;
  }

  // The project exists and the viewer may read it - but not necessarily here.
  if (project.workspace_id !== workspaceId) notFound();

  return (
    <section className="stack">
      <nav aria-label="Project">
        <Link className="workspace-bar__link" href={`/w/${workspaceId}`}>
          ← Projects
        </Link>{" "}
        <span className="muted">
          / <code>{project.slug}</code>
        </span>
      </nav>

      {children}
    </section>
  );
}
