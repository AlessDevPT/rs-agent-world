import { notFound } from "next/navigation";

import { getProject, listAgents, type Agent } from "@/lib/api/agents";
import { ApiError } from "@/lib/api/client";
import type { Project } from "@/lib/api/tenancy";
import {
  agentsEmptyState,
  livenessLabel,
  livenessSentence,
  livenessTone,
  pillClassName,
  statusLabel,
  statusTone,
} from "@/lib/agents/presentation";
import { requireSession } from "@/lib/session";

export const dynamic = "force-dynamic";

export const metadata = { title: "Agents · AgentWorld" };

/**
 * The agents registered into one project.
 *
 * **Status and liveness are two different claims and are rendered as two.**
 * `status` is folded from what the agent reported; `liveness` is computed at
 * read time from its heartbeat against its own TTL. An agent can be `working`
 * and overdue at the same time, and this page says both rather than picking
 * the scarier one - which is why every row carries the sentence from
 * `presentation.ts` instead of a colour and a word. Nothing here infers health
 * or progress from a heartbeat.
 *
 * Archived agents are not listed: the API excludes them unless asked, and the
 * link that asks arrives in stage 1e. No counts, because the API has no
 * aggregate to count with and a number invented here would read as a fact.
 */
export default async function AgentsPage({
  params,
}: {
  readonly params: Promise<{ workspaceId: string; projectId: string }>;
}) {
  const { workspaceId, projectId } = await params;
  const { token } = await requireSession(`/w/${workspaceId}/p/${projectId}/agents`);

  // **This page authorizes itself, rather than trusting the layout above.**
  // The layout makes the same checks, but a layout and its page execute
  // concurrently: its `notFound()` decides what is sent to the browser, not
  // what runs here. Measured rather than assumed - a cross-workspace URL that
  // rendered the 404 page still put this page's two requests in the API log.
  //
  // So the project is fetched first and its workspace checked before anything
  // project-scoped is asked for. That costs a round trip the previous
  // `Promise.all` saved, and buys the guarantee that a mismatched path never
  // reaches the agent listing at all.
  let project: Project | null = null;
  let error: string | null = null;
  try {
    project = await getProject(token, projectId);
  } catch (caught) {
    // A 404 is the API saying this project is not there for this caller -
    // which is an answer, not a failure, and the same answer the layout
    // gives. `notFound()` throws, and there is no outer `try` to swallow it.
    // Anything else really is a failure and stays one: an API that is down
    // must not be reported as a project that does not exist.
    if (caught instanceof ApiError && caught.problem.status === 404) notFound();
    error = caught instanceof ApiError ? caught.problem.detail : "Could not load this project";
  }

  // Outside the `try` on purpose: `notFound()` signals by throwing, so a catch
  // around it would swallow the 404 and render an error card instead. A
  // genuine API failure stays an error; only a project that really belongs to
  // another workspace becomes a 404.
  if (project !== null && project.workspace_id !== workspaceId) notFound();

  let agents: Agent[] = [];
  if (project !== null) {
    try {
      agents = await listAgents(token, projectId);
    } catch (caught) {
      error = caught instanceof ApiError ? caught.problem.detail : "Could not load agents";
    }
  }

  const empty = agentsEmptyState(false);

  return (
    <section className="stack">
      <div>
        <h1 className="title">{project?.name ?? "Agents"}</h1>
        <p className="muted">
          Agents registered into this project. An agent&apos;s status is what it last
          reported; its heartbeat is a separate signal about whether we are still hearing
          from it.
        </p>
      </div>

      {error !== null && (
        <div className="card">
          <p className="form__error" role="alert">
            {error}
          </p>
        </div>
      )}

      {error === null &&
        (agents.length === 0 ? (
          <div className="card">
            <h2 className="card__title">{empty.headline}</h2>
            <p className="muted">{empty.detail}</p>
          </div>
        ) : (
          <ul className="list">
            {agents.map((agent) => {
              const heartbeat = livenessSentence(agent);
              return (
                <li className="list__item" key={agent.id}>
                  <div>
                    <span className="list__title">{agent.display_name}</span>{" "}
                    <span className="muted">
                      <code>{agent.agent_key}</code>
                    </span>
                    <p className="muted">{heartbeat.detail}</p>
                  </div>

                  <div>
                    <span className={pillClassName(statusTone(agent.status))}>
                      {statusLabel(agent.status)}
                    </span>{" "}
                    <span className={pillClassName(livenessTone(agent.liveness))}>
                      {livenessLabel(agent.liveness)}
                    </span>{" "}
                    <span className="pill pill--role">{agent.interaction_mode}</span>
                  </div>
                </li>
              );
            })}
          </ul>
        ))}
    </section>
  );
}
