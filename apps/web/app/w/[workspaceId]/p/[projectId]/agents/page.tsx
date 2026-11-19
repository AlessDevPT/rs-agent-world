import Link from "next/link";
import { notFound } from "next/navigation";

import { getProject, listAgents, type Agent } from "@/lib/api/agents";
import { ApiError } from "@/lib/api/client";
import type { Project } from "@/lib/api/tenancy";
import { canCreateAgents } from "@/lib/agents/create";
import {
  agentsEmptyState,
  archivedToggle,
  includeArchived,
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
 * **Archived agents are excluded unless `?archived=1` asks for them**, which
 * is the API's own default rather than a choice made here: a project's list
 * should be what it is now, and an archived agent is a record of what it was.
 * The link that asks is a plain `Link` rather than a control with state - the
 * filter is in the URL, so it survives a reload and can be sent to somebody.
 * No counts, because the API has no aggregate to count with and a number
 * invented here would read as a fact.
 *
 * An archived row says so once, on the row, and keeps its status and
 * liveness pills: those are still what the agent last reported, and blanking
 * them would be inventing a second claim out of the first.
 */
export default async function AgentsPage({
  params,
  searchParams,
}: {
  readonly params: Promise<{ workspaceId: string; projectId: string }>;
  readonly searchParams: Promise<Record<string, string | string[] | undefined>>;
}) {
  const { workspaceId, projectId } = await params;
  const including = includeArchived((await searchParams)["archived"]);
  const { token, me } = await requireSession(`/w/${workspaceId}/p/${projectId}/agents`);

  // `agent.create` is `member` on the API. The link is hidden from a viewer
  // because a form they cannot post is not worth offering - the endpoint is
  // what refuses them, and the create page explains itself if they arrive
  // there by URL anyway.
  const membership = me.workspaces.find((entry) => entry.workspace.id === workspaceId);
  const mayCreate = canCreateAgents(membership?.role ?? "viewer");

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
      agents = await listAgents(token, projectId, { includeArchived: including });
    } catch (caught) {
      error = caught instanceof ApiError ? caught.problem.detail : "Could not load agents";
    }
  }

  const agentsPath = `/w/${workspaceId}/p/${projectId}/agents`;
  const toggle = archivedToggle(agentsPath, including);
  const empty = agentsEmptyState(including);

  return (
    <section className="stack">
      <div>
        <h1 className="title">{project?.name ?? "Agents"}</h1>
        <p className="muted">
          Agents registered into this project. An agent&apos;s status is what it last
          reported; its heartbeat is a separate signal about whether we are still hearing
          from it.
        </p>
        <p>
          {mayCreate && (
            <>
              <Link className="button button--primary" href={`${agentsPath}/new`}>
                New agent
              </Link>{" "}
            </>
          )}
          <Link className="button button--quiet" href={toggle.href}>
            {toggle.label}
          </Link>
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
            {mayCreate && <p><Link href={`${agentsPath}/new`}>Create the first one</Link></p>}
          </div>
        ) : (
          <ul className="list">
            {agents.map((agent) => {
              const heartbeat = livenessSentence(agent);
              return (
                <li className="list__item" key={agent.id}>
                  <div>
                    {/* The row's own link, styled by `a.list__title` - the
                        same rule a project row uses, so the title keeps the
                        row's colour rather than the browser's link blue. */}
                    <Link className="list__title" href={`${agentsPath}/${agent.id}`}>
                      {agent.display_name}
                    </Link>{" "}
                    <span className="muted">
                      <code>{agent.agent_key}</code>
                    </span>{" "}
                    {agent.archived_at !== null && (
                      <span className={pillClassName("warn")}>archived</span>
                    )}
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
