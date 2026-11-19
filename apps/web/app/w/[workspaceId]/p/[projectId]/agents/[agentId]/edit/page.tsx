import Link from "next/link";
import { notFound } from "next/navigation";

import { canConfigureAgents } from "@/lib/agents/create";
import { agentEditability, configurationNotice } from "@/lib/agents/edit";
import { loadAgentEditing } from "@/lib/agents/load";
import { requireSession } from "@/lib/session";

import { AgentEditForm } from "./agent-edit-form";

export const dynamic = "force-dynamic";

export const metadata = { title: "Edit agent · AgentWorld" };

/**
 * Change what an agent is told and, for an admin, what it runs on.
 *
 * **This page authorizes itself rather than trusting the layout above**, for
 * the reason the detail page documents: a layout and its page execute
 * concurrently, so the layout's `notFound()` decides what is sent to the
 * browser and not what runs here. The ordering lives in `loadAgentEditing`,
 * which is built on `loadAgentOverview` so that an edit form cannot be the
 * one path that asks for an agent under a workspace its project is not in.
 *
 * Two roles matter and they are not the same one. `agent.update` is
 * `member`, so a viewer gets an explanation instead of a form. The model
 * profile and the heartbeat limit are in the API's `CONFIGURATION_FIELDS`
 * and need `admin` - and because the API refuses the *whole* request when a
 * member sends one, a member's form must not draw them at all. Neither gate
 * is the boundary; both are checked again on the request the form sends.
 *
 * An archived agent gets no form. `AgentService.update` refuses a write to
 * one with a 409 about its state, so a form here could only collect an edit
 * in order to have it rejected.
 */
export default async function EditAgentPage({
  params,
}: {
  readonly params: Promise<{ workspaceId: string; projectId: string; agentId: string }>;
}) {
  const { workspaceId, projectId, agentId } = await params;
  const detail = `/w/${workspaceId}/p/${projectId}/agents/${agentId}`;
  const { token, me } = await requireSession(`${detail}/edit`);

  const membership = me.workspaces.find((entry) => entry.workspace.id === workspaceId);
  if (membership === undefined) notFound();
  const role = membership.role;

  const mayConfigure = canConfigureAgents(role);

  const outcome = await loadAgentEditing(token, workspaceId, projectId, agentId, {
    mayConfigure,
  });
  if (outcome.kind === "not_found") notFound();

  if (outcome.kind === "error") {
    return (
      <section className="stack">
        <h1 className="title">Edit agent</h1>
        <div className="card">
          <p className="form__error" role="alert">
            {outcome.message}
            {outcome.requestId !== undefined && <> (request {outcome.requestId})</>}
          </p>
        </div>
      </section>
    );
  }

  const { agent } = outcome;
  const editability = agentEditability(agent, role);

  if (editability.kind !== "editable") {
    const [headline, detailText] =
      editability.kind === "forbidden"
        ? ["Not available to your role", editability.notice]
        : [editability.notice.headline, editability.notice.detail];
    return (
      <section className="stack">
        <h1 className="title">Edit {agent.display_name}</h1>
        <div className="card">
          <h2 className="card__title">{headline}</h2>
          <p className="muted">{detailText}</p>
          <p className="muted">
            <Link href={detail}>Back to the agent</Link>
          </p>
        </div>
      </section>
    );
  }

  return (
    <section className="stack stack--narrow">
      <div>
        <h1 className="title">Edit {agent.display_name}</h1>
        <p className="muted">
          <code>{agent.agent_key}</code> in {outcome.project.name}. The key cannot be
          changed by anybody once the agent exists. The interaction mode can be, but not
          from here: it decides what may be asked of the agent, and changing it is not
          part of this release.
        </p>
      </div>

      {!mayConfigure && (
        <div className="card">
          <h2 className="card__title">You can edit it, but not configure it</h2>
          <p className="muted">{configurationNotice(role)}</p>
        </div>
      )}

      <AgentEditForm
        workspaceId={workspaceId}
        projectId={projectId}
        agent={agent}
        profiles={outcome.profiles}
        mayConfigure={mayConfigure}
      />
    </section>
  );
}
