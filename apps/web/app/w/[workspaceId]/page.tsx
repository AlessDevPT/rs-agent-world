import Link from "next/link";

import { ApiError } from "@/lib/api/client";
import { listProjects, roleAtLeast, type Project } from "@/lib/api/tenancy";
import { requireSession } from "@/lib/session";

import { CreateProjectForm } from "./create-project-form";

export const dynamic = "force-dynamic";

/**
 * The project list for one workspace.
 *
 * A project is the unit agents are registered into and events are scoped to,
 * so this is the screen everything else in later phases hangs off.
 */
export default async function ProjectsPage({
  params,
}: {
  readonly params: Promise<{ workspaceId: string }>;
}) {
  const { workspaceId } = await params;
  const { token, me } = await requireSession(`/w/${workspaceId}`);

  const membership = me.workspaces.find((entry) => entry.workspace.id === workspaceId);
  // The layout has already established membership; this is only to read the
  // role, and TypeScript cannot know the layout ran.
  const role = membership?.role ?? "viewer";
  const mayCreate = roleAtLeast(role, "member");

  let projects: Project[] = [];
  let error: string | null = null;
  try {
    projects = await listProjects(token, workspaceId);
  } catch (caught) {
    error = caught instanceof ApiError ? caught.problem.detail : "Could not load projects";
  }

  return (
    <section className="stack">
      <div>
        <h1 className="title">Projects</h1>
        <p className="muted">
          Agents register into a project, and events, tasks and keys are scoped to one.
        </p>
      </div>

      {error !== null && (
        <div className="card">
          <p className="form__error" role="alert">
            {error}
          </p>
        </div>
      )}

      {projects.length === 0 && error === null ? (
        <div className="card">
          <h2 className="card__title">No projects yet</h2>
          <p className="muted">
            {mayCreate
              ? "Create one below. You can mint an API key for it afterwards."
              : "Ask an admin to create one - a viewer cannot."}
          </p>
        </div>
      ) : (
        <ul className="list">
          {projects.map((project) => (
            <li className="list__item" key={project.id}>
              <div>
                <Link
                  className="list__title"
                  href={`/w/${workspaceId}/p/${project.id}/agents`}
                >
                  {project.name}
                </Link>
                <span className="muted">
                  {" "}
                  <code>{project.slug}</code>
                </span>
                {project.description !== null && (
                  <p className="muted">{project.description}</p>
                )}
              </div>
              {/* A link rather than a count. The API has no aggregate to
                  count with, so a number here would mean one request per
                  project - and an invented zero would read as a fact. */}
              <Link
                className="workspace-bar__link"
                href={`/w/${workspaceId}/p/${project.id}/agents`}
              >
                Agents →
              </Link>
            </li>
          ))}
        </ul>
      )}

      {mayCreate && (
        <div className="card">
          <h2 className="card__title">New project</h2>
          <CreateProjectForm workspaceId={workspaceId} />
        </div>
      )}
    </section>
  );
}
