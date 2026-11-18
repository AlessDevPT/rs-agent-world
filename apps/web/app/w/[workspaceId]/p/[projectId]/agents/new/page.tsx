import Link from "next/link";
import { notFound } from "next/navigation";

import {
  canConfigureAgents,
  canCreateAgents,
  cannotCreateNotice,
  configureNotice,
} from "@/lib/agents/create";
import { loadAgentCreation } from "@/lib/agents/load";
import { requireSession } from "@/lib/session";

import { AgentCreateForm } from "./agent-create-form";

export const dynamic = "force-dynamic";

export const metadata = { title: "New agent · AgentWorld" };

/**
 * Register an agent into a project.
 *
 * **This page authorizes itself rather than trusting the layout above**, for
 * the reason `agents/page.tsx` documents at length: a layout and its page
 * execute concurrently, so the layout's `notFound()` decides what is sent to
 * the browser and not what runs here. The sequence - project first, workspace
 * compared, only then anything else - lives in `loadAgentCreation` where a
 * test can watch the calls go out.
 *
 * Two roles matter and they are not the same one. `agent.create` is `member`,
 * so a viewer gets an explanation instead of a form, the way the credentials
 * page explains itself rather than 404ing. `agent.configure` is `admin`, and
 * `AgentService.create` demands it as soon as the body carries a
 * `model_profile_id` - so a member sees the whole form except the model, and
 * is told who can add one. Neither gate is the boundary: both are the API's,
 * checked again on the request the form posts.
 */
export default async function NewAgentPage({
  params,
}: {
  readonly params: Promise<{ workspaceId: string; projectId: string }>;
}) {
  const { workspaceId, projectId } = await params;
  const { token, me } = await requireSession(`/w/${workspaceId}/p/${projectId}/agents/new`);

  const membership = me.workspaces.find((entry) => entry.workspace.id === workspaceId);
  if (membership === undefined) notFound();
  const role = membership.role;

  const mayCreate = canCreateAgents(role);
  const mayConfigure = canConfigureAgents(role);

  // Asked for only when the viewer may attach one. Listing profiles is
  // `viewer` on the API, so this is not about permission to read them: it is
  // about not making a request whose answer the page has no use for.
  const outcome = await loadAgentCreation(token, workspaceId, projectId, { mayConfigure });
  if (outcome.kind === "not_found") notFound();

  if (outcome.kind === "error") {
    return (
      <section className="stack">
        <div>
          <h1 className="title">New agent</h1>
        </div>
        <div className="card">
          <p className="form__error" role="alert">
            {outcome.message}
            {outcome.requestId !== undefined && <> (request {outcome.requestId})</>}
          </p>
        </div>
      </section>
    );
  }

  const agents = `/w/${workspaceId}/p/${projectId}/agents`;

  if (!mayCreate) {
    return (
      <section className="stack">
        <div>
          <h1 className="title">New agent</h1>
        </div>
        <div className="card">
          <h2 className="card__title">Not available to your role</h2>
          <p className="muted">{cannotCreateNotice(role)}</p>
          <p className="muted">
            <Link href={agents}>Back to {outcome.project.name}</Link>
          </p>
        </div>
      </section>
    );
  }

  return (
    <section className="stack stack--narrow">
      <div>
        <h1 className="title">New agent in {outcome.project.name}</h1>
        <p className="muted">
          An agent is a worker this project knows about. What AgentWorld may do with it
          depends on the mode you pick, and only a managed agent is one AgentWorld runs
          itself.
        </p>
      </div>

      {!mayConfigure && (
        <div className="card">
          <h2 className="card__title">You can create it, but not give it a model</h2>
          <p className="muted">{configureNotice(role)}</p>
        </div>
      )}

      <AgentCreateForm
        workspaceId={workspaceId}
        projectId={projectId}
        profiles={outcome.profiles}
        mayConfigure={mayConfigure}
      />
    </section>
  );
}
