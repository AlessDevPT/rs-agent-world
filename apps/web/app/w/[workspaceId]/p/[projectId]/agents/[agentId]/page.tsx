import Link from "next/link";
import { notFound } from "next/navigation";
import { Fragment } from "react";

import { agentEditability, canArchiveAgents } from "@/lib/agents/edit";
import { loadAgentExecutions, loadAgentOverview } from "@/lib/agents/load";
import {
  executionStatusTone,
  executionsEmptyState,
  formatSeconds,
  formatTimestamp,
  listingNote,
  livenessLabel,
  livenessSentence,
  livenessTone,
  pillClassName,
  statusLabel,
  statusTone,
} from "@/lib/agents/presentation";
import {
  DEFAULT_LIMIT,
  type AgentExecution,
  type AgentInteractionMode,
} from "@/lib/api/agents";
import { executionStatusLabel, executionTiming } from "@/lib/executions/presentation";
import { requireSession } from "@/lib/session";

import { AgentLifecycle } from "./agent-lifecycle";

export const dynamic = "force-dynamic";

export const metadata = { title: "Agent · AgentWorld" };

/**
 * One agent: what it is, what it last said about itself, and whether we are
 * still hearing from it.
 *
 * **This page authorizes itself.** The project layout above makes the same
 * checks, but a layout and its page execute concurrently - the layout's
 * `notFound()` decides what is sent to the browser, not what runs here - so
 * the ordering lives in `loadAgentOverview`, which fetches the project,
 * refuses a workspace mismatch, and only then asks for the agent. It is a
 * pure module precisely so that order can be unit-tested; see
 * `tests/agent-overview-load.test.ts`.
 *
 * **Status and heartbeat are two claims and get two cards.** `status` is
 * folded from what the agent reported about itself; `liveness` is computed at
 * read time from its last heartbeat against its own TTL. An agent can be
 * `working` and overdue at once, and nothing here infers one from the other.
 * For a `managed` agent there is no outside worker to hear from, so the
 * heartbeat limit and the last-heartbeat time are left out entirely rather
 * than printed as figures that invite a conclusion they do not support.
 *
 * An archived agent is reachable here on purpose: the detail endpoint asks for
 * archived rows while the listing does not, because what an agent did before
 * it was archived is usually the question. It says so, with the date.
 *
 * Deliberately absent: `instructions` is free-form text, and `dispatch`,
 * `model_overrides` and `labels` are free-form JSON objects. The API gives
 * none of the four a schema, so a deployment may have put anything in any of
 * them - including an endpoint or a token - and every one of them is readable
 * by a `viewer`. The edit form shows `instructions` and `labels` because it
 * has to - you cannot change what you cannot see - and it is `member` and
 * above; that is a narrower audience than this page, which is why they are
 * still not here. `current_task_id` is left out because the API has no task
 * endpoint to resolve it against, and a bare uuid on a screen is noise. There
 * is no registration timestamp because `AgentView` does not carry one; none is
 * invented here.
 *
 * **Editing and the lifecycle are two different permissions and are drawn as
 * two.** `agent.update` is `member`, so the edit link follows that; archiving
 * and restoring are `admin`, so the card below does. Both are the API's and
 * are checked again on the request - hiding a control is a convenience, not a
 * boundary. The edit link is not drawn for an archived agent, which the API
 * refuses to change at all.
 */
export default async function AgentDetailPage({
  params,
}: {
  readonly params: Promise<{ workspaceId: string; projectId: string; agentId: string }>;
}) {
  const { workspaceId, projectId, agentId } = await params;
  const { token, me } = await requireSession(
    `/w/${workspaceId}/p/${projectId}/agents/${agentId}`,
  );

  const membership = me.workspaces.find((entry) => entry.workspace.id === workspaceId);
  const role = membership?.role ?? "viewer";
  const mayArchive = canArchiveAgents(role);

  const result = await loadAgentOverview(token, workspaceId, projectId, agentId);

  // `notFound()` throws, and there is no `try` anywhere in this page for it to
  // be swallowed by: the loader hands back an outcome rather than throwing, so
  // a genuine failure arrives as `error` and stays an error card.
  if (result.kind === "not_found") notFound();

  if (result.kind === "error") {
    return (
      <section className="stack">
        <h1 className="title">Agent</h1>
        <div className="card">
          <p className="form__error" role="alert">
            {result.message}
          </p>
          {result.requestId !== undefined && (
            <p className="muted">
              Request <code>{result.requestId}</code> if you are reporting this.
            </p>
          )}
        </div>
      </section>
    );
  }

  const { project, agent } = result;
  // The same rule the edit page itself applies, from the same function, so
  // a link is never offered to a page that would answer with a notice.
  const mayEdit = agentEditability(agent, role).kind === "editable";
  // After the overview, not beside it: this list is one card's content, and
  // asking for it before the path is known to be coherent would request an
  // agent's executions for a path the loader is about to refuse.
  const executions = await loadAgentExecutions(token, projectId, agentId, DEFAULT_LIMIT);
  const heartbeat = livenessSentence(agent);
  const archived = agent.archived_at !== null;
  // A heartbeat TTL and a last-heartbeat time are facts about an agent that is
  // expected to call in. A managed agent is not, so they would only invite the
  // reader to conclude something is late.
  const expectsHeartbeat = agent.interaction_mode !== "managed";

  return (
    <section className="stack">
      <div>
        <h1 className="title">
          {agent.display_name}{" "}
          {archived && <span className={pillClassName("warn")}>archived</span>}
        </h1>
        <p className="muted">
          <code>{agent.agent_key}</code> in {project.name}
        </p>
        {archived && (
          <p className="muted">
            Archived {formatTimestamp(agent.archived_at)}. It is left out of this
            project&apos;s agent list unless the list is asked for archived agents.
          </p>
        )}
        {mayEdit && (
          <p>
            <Link
              className="button button--quiet"
              href={`/w/${workspaceId}/p/${projectId}/agents/${agentId}/edit`}
            >
              Edit this agent
            </Link>
          </p>
        )}
      </div>

      <div className="card">
        <h2 className="card__title">Status</h2>
        <p>
          <span className={pillClassName(statusTone(agent.status))}>
            {statusLabel(agent.status)}
          </span>
        </p>
        {/* Only when the agent gave one. A sentence invented to fill the gap
            would be describing an agent that said nothing. */}
        {agent.status_reason !== null && <p className="muted">{agent.status_reason}</p>}
      </div>

      <div className="card">
        <h2 className="card__title">Heartbeat</h2>
        {/* The pill alone, not the pill and `heartbeat.headline`: the two say
            the same thing in nearly the same words ("heartbeat current" next
            to "Heartbeat is current"), and read as a stutter. The headline is
            there for a screen that has no pill. */}
        <p>
          <span className={pillClassName(livenessTone(agent.liveness))}>
            {livenessLabel(agent.liveness)}
          </span>
        </p>
        <p className="muted">{heartbeat.detail}</p>
      </div>

      <div className="card">
        <h2 className="card__title">Executions</h2>
        {executions.kind === "error" ? (
          <>
            <p className="form__error" role="alert">
              {executions.message}
            </p>
            {executions.requestId !== undefined && (
              <p className="muted">
                Request <code>{executions.requestId}</code> if you are reporting this.
              </p>
            )}
          </>
        ) : (
          <ExecutionList
            executions={executions.executions}
            workspaceId={workspaceId}
            projectId={projectId}
            mode={agent.interaction_mode}
          />
        )}
      </div>

      <div className="card">
        <h2 className="card__title">Details</h2>
        <dl className="kv">
          <dt>Interaction mode</dt>
          <dd>
            <span className="pill pill--role">{agent.interaction_mode}</span>
          </dd>

          {agent.kind !== null && (
            <>
              <dt>Kind</dt>
              <dd>{agent.kind}</dd>
            </>
          )}

          {agent.role !== null && (
            <>
              <dt>Role</dt>
              <dd>{agent.role}</dd>
            </>
          )}

          {agent.description !== null && (
            <>
              <dt>Description</dt>
              <dd>{agent.description}</dd>
            </>
          )}

          <dt>Capabilities</dt>
          <dd>
            {agent.capabilities.length === 0 ? (
              <span className="muted">None reported</span>
            ) : (
              agent.capabilities.map((capability) => (
                // The space belongs between the pills, not inside one: a
                // trailing space within the border widens the pill instead.
                <Fragment key={capability}>
                  <span className="pill">{capability}</span>{" "}
                </Fragment>
              ))
            )}
          </dd>

          {/* Whether, not which: there is no endpoint that reads one profile
              by id, so naming it would mean listing the workspace's profiles
              to resolve it - and a raw uuid says nothing to a reader. */}
          <dt>Model profile</dt>
          <dd>
            {agent.model_profile_id === null ? (
              <span className="muted">Not configured</span>
            ) : (
              "Configured"
            )}
          </dd>

          {expectsHeartbeat && (
            <>
              <dt>Heartbeat limit</dt>
              <dd>{formatSeconds(agent.heartbeat_ttl_seconds)}</dd>

              <dt>Last heartbeat</dt>
              <dd>{formatTimestamp(agent.last_heartbeat_at)}</dd>
            </>
          )}
        </dl>
      </div>

      {mayArchive && (
        <AgentLifecycle
          workspaceId={workspaceId}
          projectId={projectId}
          agentId={agent.id}
          agentKey={agent.agent_key}
          archived={archived}
        />
      )}
    </section>
  );
}

/**
 * The agent's executions, newest first, each a link to what its steps did.
 *
 * A row says only what it can: the status, when it ran, and - for a managed
 * agent, whose executions are mostly its own tests - whether this one was the
 * `agent.test` harness rather than a workflow somebody drew. The id is not
 * printed; it is the link, and a uuid on screen is noise.
 */
function ExecutionList({
  executions,
  workspaceId,
  projectId,
  mode,
}: {
  readonly executions: readonly AgentExecution[];
  readonly workspaceId: string;
  readonly projectId: string;
  readonly mode: AgentInteractionMode;
}) {
  if (executions.length === 0) {
    const empty = executionsEmptyState(mode);
    return (
      <>
        <p>{empty.headline}</p>
        <p className="muted">{empty.detail}</p>
      </>
    );
  }

  const note = listingNote(executions.length, DEFAULT_LIMIT);
  return (
    <>
      <ul className="list">
        {executions.map((execution) => (
          <li className="list__item" key={execution.id}>
            <div>
              <Link
                className="list__title"
                href={`/w/${workspaceId}/p/${projectId}/executions/${execution.id}`}
              >
                {execution.is_test_harness ? "Test of this agent" : "Workflow run"}
              </Link>
              <p className="muted">{executionTiming(execution)}</p>
            </div>
            <div>
              <span className={pillClassName(executionStatusTone(execution.status))}>
                {executionStatusLabel(execution.status)}
              </span>
            </div>
          </li>
        ))}
      </ul>
      {note !== null && <p className="muted">{note}</p>}
    </>
  );
}
