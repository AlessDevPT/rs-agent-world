/**
 * Typed calls for the agent-management endpoints.
 *
 * Hand-written for now and deliberately thin, exactly like `tenancy.ts`:
 * Phase 5 replaces both with a client generated from `/openapi.json`, so
 * nothing here should grow logic worth keeping. The shapes mirror
 * `apps/api/src/agentworld/api/v1/agents.py` and the functions do nothing but
 * name a path.
 *
 * **Two listings on an agent, and they are not the same list.** `listRuns` is
 * what an external agent reported about work it did wherever it lives, folded
 * from its own events. `listExecutions` is AgentWorld's record of work it drove
 * itself. Different tables, different producers, and the API keeps them apart
 * rather than merging them into a common "activity" record neither one is - so
 * this file does too, and an empty list on either side is an answer about
 * where to look rather than a missing record.
 *
 * Every read here is `viewer`, and every one is tenant-scoped: an id belonging
 * to another workspace is a 404, not a 403.
 */

import { apiFetch } from "./client";
import type { Project } from "./tenancy";

export type AgentInteractionMode = "managed" | "dispatchable" | "observed";

export type AgentStatus =
  | "provisioning"
  | "offline"
  | "idle"
  | "working"
  | "blocked"
  | "error"
  | "maintenance"
  | "paused";

/**
 * Whether an agent's heartbeat is current. **Nothing more than that.**
 *
 * Four outcomes rather than a boolean, because three of them are not "stale"
 * and collapsing them would publish a claim the data does not support. In
 * particular `stale` does not mean dead, disconnected or stuck: heartbeats and
 * work are reported by different events, so an agent can be mid-run, reporting
 * steps every second, and still be `stale` here.
 */
export type AgentLiveness = "not_applicable" | "never_reported" | "fresh" | "stale";

export type RunStatus = "running" | "succeeded" | "failed" | "cancelled";

export type ExecutionStatus =
  | "pending"
  | "running"
  | "waiting_approval"
  | "waiting_input"
  | "paused"
  | "completed"
  | "failed"
  | "cancelled";

export type InitiatorKind = "user" | "system";

/** A JSON object the API passes through without interpreting it. */
export type JsonObject = Readonly<Record<string, unknown>>;

export interface Agent {
  readonly id: string;
  readonly project_id: string;
  readonly agent_key: string;
  readonly display_name: string;
  readonly interaction_mode: AgentInteractionMode;
  readonly role: string | null;
  readonly description: string | null;
  readonly instructions: string | null;
  readonly kind: string | null;
  readonly labels: JsonObject;
  readonly model_profile_id: string | null;
  readonly model_overrides: JsonObject;
  readonly dispatch: JsonObject;
  readonly heartbeat_ttl_seconds: number;
  readonly status: AgentStatus;
  readonly status_reason: string | null;
  readonly capabilities: readonly string[];
  readonly current_task_id: string | null;
  readonly last_heartbeat_at: string | null;
  readonly archived_at: string | null;
  /** Derived at read time, not folded: there is no column behind either. */
  readonly liveness: AgentLiveness;
  /**
   * Whole seconds of silence, or null when the agent has never heartbeated.
   * Measured by the server against the same instant as `liveness`, so the two
   * cannot disagree - which is why the UI renders this number rather than
   * recomputing one from `last_heartbeat_at` against the browser's clock.
   */
  readonly seconds_since_heartbeat: number | null;
}

export interface StatusChange {
  /**
   * The status being left. Rows recorded before stage D1.1 repeat `to_status`
   * instead, and were left as they are rather than guessed at.
   */
  readonly from_status: AgentStatus | null;
  readonly to_status: AgentStatus;
  readonly changed_at: string;
  readonly reason: string | null;
  readonly event_id: string | null;
}

/** One attempt an external agent reported making at something. Not an execution. */
export interface Run {
  readonly id: string;
  readonly run_ref: string | null;
  readonly status: RunStatus;
  readonly task_id: string | null;
  readonly execution_step_id: string | null;
  readonly step_count: number;
  /**
   * Tool calls this run *invoked*. Refused ones are not counted, so an agent
   * whose every call was blocked reports zero here.
   */
  readonly tool_call_count: number;
  readonly error: JsonObject | null;
  readonly started_at: string;
  readonly finished_at: string | null;
  /** Computed by the database from the two timestamps. Null while in flight. */
  readonly duration_ms: number | null;
}

/** AgentWorld's own record of work it drove. Not a run. */
export interface AgentExecution {
  readonly id: string;
  readonly workflow_id: string;
  readonly workflow_version_id: string;
  readonly status: ExecutionStatus;
  readonly initiator_kind: InitiatorKind;
  readonly initiator_label: string;
  readonly input: JsonObject;
  readonly output: JsonObject | null;
  readonly error: JsonObject | null;
  readonly created_at: string | null;
  readonly started_at: string | null;
  readonly finished_at: string | null;
  /**
   * True when this ran the agent's `agent.test` harness rather than a workflow
   * somebody drew. Included rather than filtered out: for most agents the
   * harness runs are the only executions there are.
   */
  readonly is_test_harness: boolean;
}

/** The cap the API puts on every listing here. There is no cursor to page with. */
export const MAX_LIMIT = 200;

/** What the API uses when `limit` is omitted. */
export const DEFAULT_LIMIT = 50;

interface Listing {
  readonly limit?: number;
}

/**
 * A query string, or the empty string.
 *
 * `false` and `undefined` are both dropped rather than sent: the API's
 * defaults are `include_archived=false` and `limit=50`, so spelling them out
 * would put noise in the request log for no change in the answer.
 */
function query(params: Readonly<Record<string, string | number | boolean | undefined>>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === false) continue;
    search.set(key, String(value));
  }
  const rendered = search.toString();
  return rendered === "" ? "" : `?${rendered}`;
}

// -------------------------------------------------------------------- calls ---

/**
 * One project by id.
 *
 * Workspace-scoped like everything else, so a project in another workspace is
 * a 404 - which is what the page turns into `notFound()`.
 */
export function getProject(token: string, projectId: string): Promise<Project> {
  return apiFetch<Project>(`/v1/projects/${projectId}`, { token });
}

/**
 * The agents in a project.
 *
 * Archived agents are excluded unless asked for. They stay readable
 * individually - what an agent did before it was archived is the question -
 * so the detail page needs no flag.
 */
export function listAgents(
  token: string,
  projectId: string,
  options: { readonly includeArchived?: boolean } = {},
): Promise<Agent[]> {
  const search = query({ include_archived: options.includeArchived });
  return apiFetch<Agent[]>(`/v1/projects/${projectId}/agents${search}`, { token });
}

export function getAgent(token: string, projectId: string, agentId: string): Promise<Agent> {
  return apiFetch<Agent>(`/v1/projects/${projectId}/agents/${agentId}`, { token });
}

/**
 * What `POST /v1/projects/{id}/agents` accepts.
 *
 * `display_name` and `interaction_mode` are the only required fields: the
 * schema gives `interaction_mode` no default, so a body without it is a 422
 * rather than a managed agent. Everything else is optional, and *omitted*
 * rather than nulled - `agent_key` falls back to a slug of the display name
 * only when it is absent, and an empty string would be refused instead.
 *
 * `model_profile_id` additionally needs `agent.configure`, which is `admin`:
 * a body carrying one from a member is a 403 for the whole request. The
 * fields `heartbeat_ttl_seconds`, and anything about status, heartbeat or
 * capabilities, are deliberately not here - the first is not on `AgentCreate`
 * at all, and the rest are folded from events that only the agent produces.
 *
 * `model_overrides` and `dispatch` are accepted by the API and left out of
 * this type: nothing in the dispatch path reads either one, so sending them
 * would store configuration that has no effect.
 */
export interface CreateAgentBody {
  readonly display_name: string;
  readonly interaction_mode: AgentInteractionMode;
  readonly agent_key?: string;
  readonly role?: string;
  readonly description?: string;
  readonly instructions?: string;
  readonly kind?: string;
  readonly labels?: Readonly<Record<string, unknown>>;
  readonly model_profile_id?: string;
}

/** Register an agent into a project. `member`, or `admin` with a model. */
export function createAgent(
  token: string,
  projectId: string,
  body: CreateAgentBody,
): Promise<Agent> {
  return apiFetch<Agent>(`/v1/projects/${projectId}/agents`, {
    token,
    method: "POST",
    body,
  });
}

/**
 * When this agent's status changed, newest first.
 *
 * An agent AgentWorld runs itself has none: a managed agent reports nothing,
 * and its work appears under `listExecutions` instead.
 */
export function listStatusHistory(
  token: string,
  projectId: string,
  agentId: string,
  options: Listing = {},
): Promise<StatusChange[]> {
  const search = query({ limit: options.limit });
  return apiFetch<StatusChange[]>(
    `/v1/projects/${projectId}/agents/${agentId}/status-history${search}`,
    { token },
  );
}

/** What this agent reported doing, newest first. External agents only. */
export function listRuns(
  token: string,
  projectId: string,
  agentId: string,
  options: Listing = {},
): Promise<Run[]> {
  const search = query({ limit: options.limit });
  return apiFetch<Run[]>(`/v1/projects/${projectId}/agents/${agentId}/runs${search}`, { token });
}

/** Executions this agent took part in, newest first. Test harness runs included. */
export function listExecutions(
  token: string,
  projectId: string,
  agentId: string,
  options: Listing = {},
): Promise<AgentExecution[]> {
  const search = query({ limit: options.limit });
  return apiFetch<AgentExecution[]>(
    `/v1/projects/${projectId}/agents/${agentId}/executions${search}`,
    { token },
  );
}
