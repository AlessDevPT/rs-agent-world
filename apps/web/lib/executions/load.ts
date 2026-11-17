/**
 * Loading one execution and its steps, in an order that can be tested.
 *
 * The same shape as `lib/agents/load.ts`, and for the same reasons: the order
 * of the requests *is* the authorization rule, an outcome is returned rather
 * than `notFound()` thrown so the sequence can be unit-tested with no
 * framework, and 404 means "not for you" because that is what the API answers
 * for a project in a workspace the caller cannot see.
 *
 * Three requests, in this order:
 *
 * 1. the project, because its workspace is what the path has to match;
 * 2. the execution - only once the path is known to be coherent;
 * 3. its steps.
 *
 * The workspace comparison sits between 1 and 2 on purpose. The API scopes
 * reads to the caller's memberships rather than to the workspace in the URL,
 * so a project of theirs reached under another workspace's name really does
 * answer 200; letting that through would show one workspace's execution under
 * another's heading.
 *
 * **The steps are a second call because the API keeps them a second route.**
 * Every route that returns an execution returns the same document, which is
 * what `agent.test`'s "no second result API" depends on, so the steps could
 * not be folded into the first response even if a page would prefer it. A
 * failure of the second call is an error rather than a partial render: half an
 * execution is harder to read than an honest refusal, and a reader cannot tell
 * "no steps" from "the step request failed".
 */

import { getProject } from "@/lib/api/agents";
import { ApiError } from "@/lib/api/client";
import { getExecution, listExecutionSteps } from "@/lib/api/executions";
import type { Execution, ExecutionStep } from "@/lib/api/executions";
import type { Project } from "@/lib/api/tenancy";

export type ExecutionDetail =
  /** No such execution, or none the viewer may see through this path. */
  | { readonly kind: "not_found" }
  /** Something went wrong that is not an answer about this execution. */
  | {
      readonly kind: "error";
      readonly message: string;
      readonly requestId?: string | undefined;
    }
  | {
      readonly kind: "ok";
      readonly project: Project;
      readonly execution: Execution;
      readonly steps: readonly ExecutionStep[];
    };

const NOT_FOUND: ExecutionDetail = { kind: "not_found" };

export async function loadExecutionDetail(
  token: string,
  workspaceId: string,
  projectId: string,
  executionId: string,
): Promise<ExecutionDetail> {
  let project: Project;
  try {
    project = await getProject(token, projectId);
  } catch (caught) {
    return outcomeOf(caught, "Could not load this project");
  }

  // The project exists and the viewer may read it - but not necessarily here.
  if (project.workspace_id !== workspaceId) return NOT_FOUND;

  let execution: Execution;
  try {
    execution = await getExecution(token, projectId, executionId);
  } catch (caught) {
    return outcomeOf(caught, "Could not load this execution");
  }

  let steps: readonly ExecutionStep[];
  try {
    steps = await listExecutionSteps(token, projectId, executionId);
  } catch (caught) {
    return outcomeOf(caught, "Could not load this execution's steps");
  }

  return { kind: "ok", project, execution, steps };
}

/**
 * A failed call as an outcome.
 *
 * Only an `ApiError` carrying a 404 becomes `not_found`. Every other
 * `ApiError` keeps the API's own `detail`, which is written for a reader, and
 * the request id so a report can be traced to a log line. Anything else is a
 * thrown value we know nothing about - a `TypeError` from an unreachable host,
 * or a bug in this code - and gets the caller's generic sentence rather than a
 * message assembled from an exception the user cannot act on.
 */
function outcomeOf(caught: unknown, fallback: string): ExecutionDetail {
  if (caught instanceof ApiError) {
    if (caught.problem.status === 404) return NOT_FOUND;
    return { kind: "error", message: caught.problem.detail, requestId: caught.requestId };
  }
  return { kind: "error", message: fallback };
}
