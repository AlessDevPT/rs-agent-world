import { notFound } from "next/navigation";
import { Fragment } from "react";

import { loadAgentOverview } from "@/lib/agents/load";
import {
  formatSeconds,
  formatTimestamp,
  livenessLabel,
  livenessSentence,
  livenessTone,
  pillClassName,
  statusLabel,
  statusTone,
} from "@/lib/agents/presentation";
import { requireSession } from "@/lib/session";

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
 * by a `viewer`. `current_task_id` is left out because the API has no task
 * endpoint to resolve it against, and a bare uuid on a screen is noise. There
 * is no registration timestamp because `AgentView` does not carry one; none is
 * invented here.
 */
export default async function AgentDetailPage({
  params,
}: {
  readonly params: Promise<{ workspaceId: string; projectId: string; agentId: string }>;
}) {
  const { workspaceId, projectId, agentId } = await params;
  const { token } = await requireSession(
    `/w/${workspaceId}/p/${projectId}/agents/${agentId}`,
  );

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
            Archived {formatTimestamp(agent.archived_at)}. It is hidden from this
            project&apos;s agent list.
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
    </section>
  );
}
