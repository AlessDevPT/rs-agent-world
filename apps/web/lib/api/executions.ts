/**
 * Typed calls for reading one execution and what its steps did.
 *
 * Hand-written and deliberately thin, exactly like `agents.ts` and
 * `tenancy.ts`; the shapes mirror
 * `apps/api/src/agentworld/api/v1/executions.py` and the functions do nothing
 * but name a path.
 *
 * **Two reads, because the API has two routes, and that is not an accident.**
 * `GET .../executions/{id}` is the execution's own record, and every route
 * that returns an execution returns exactly that document - the project
 * listing, the workflow's listing, the agent's listing and `agent.test`'s 202
 * included. The steps are a route of their own precisely so that stays true,
 * so a page that wants both makes both calls rather than expecting one
 * response to carry everything.
 *
 * Both are `viewer` and both are tenant-scoped: an id belonging to another
 * workspace is a 404, not a 403, and an execution the caller may not see is a
 * 404 rather than an empty step list.
 */

import type { AgentExecution, JsonObject } from "./agents";
import { apiFetch } from "./client";

/**
 * An execution, as every route that returns one returns it.
 *
 * Defined from `AgentExecution` rather than beside it, so the eleven shared
 * fields have one definition and cannot drift: `is_test_harness` is the single
 * field the agent's own listing adds, and it is resolved there because that is
 * the only list where the distinction changes how a row reads.
 */
export type Execution = Omit<AgentExecution, "is_test_harness">;

/** What a step of a workflow does. */
export type StepKind =
  | "agent_task"
  | "tool_call"
  | "approval"
  | "condition"
  | "transform"
  | "wait";

/**
 * Where one step has got to.
 *
 * **There is no `needs_review` member, and that is the API's answer rather
 * than an omission here.** A step whose dispatch expired mid-flight keeps the
 * status it had when the call went out - `dispatched` - because nobody knows
 * whether the work landed and the worker writes no event for an outcome it
 * cannot describe. The uncertainty belongs to the dispatch, and the review
 * queue is where it is answered.
 */
export type StepStatus =
  | "pending"
  | "ready"
  | "dispatched"
  | "running"
  | "waiting_approval"
  | "succeeded"
  | "failed"
  | "skipped"
  | "cancelled";

/** Why a step failed, in the two fields the API will serve. */
export interface StepError {
  readonly code: string;
  readonly message: string;
}

/**
 * One step of one execution.
 *
 * `output` is narrowed by the API per step kind, and `null` therefore means
 * either "produced nothing" or "its kind has no rule for showing one" - a
 * `transform`'s output is never served, because its values are copies and the
 * API cannot tell a copy of the author's own input from a copy of something
 * upstream that was not served. Nothing here tries to tell the two apart;
 * `lib/executions/presentation.ts` says so in words instead.
 */
export interface ExecutionStep {
  readonly step_key: string;
  readonly kind: StepKind;
  readonly status: StepStatus;
  /** 0 for the first attempt. A retry adds a row rather than replacing one. */
  readonly attempt: number;
  /** The agent the frozen graph assigned, for an `agent_task`. */
  readonly agent_key: string | null;
  readonly started_at: string | null;
  readonly finished_at: string | null;
  readonly output: JsonObject | null;
  readonly error: StepError | null;
}

/** One execution's own record. */
export function getExecution(
  token: string,
  projectId: string,
  executionId: string,
): Promise<Execution> {
  return apiFetch<Execution>(`/v1/projects/${projectId}/executions/${executionId}`, { token });
}

/**
 * What its steps did, in the published version's own order.
 *
 * Not completion order, and not the frozen tables' order either: the author's
 * order survives only in the version's graph, and the API applies it. So this
 * list may be rendered as it arrives, and a page that sorts it is undoing
 * work.
 */
export function listExecutionSteps(
  token: string,
  projectId: string,
  executionId: string,
): Promise<ExecutionStep[]> {
  return apiFetch<ExecutionStep[]>(
    `/v1/projects/${projectId}/executions/${executionId}/steps`,
    { token },
  );
}
