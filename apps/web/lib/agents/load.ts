/**
 * Loading one agent, in an order that can be tested.
 *
 * **Why this is not inlined in the page.** The order of two requests is the
 * authorization rule on the detail route: a project reached through the wrong
 * workspace must become a 404 *before* anything agent-scoped is asked for.
 * That rule is exactly the kind of thing a refactor breaks silently, and
 * vitest here runs `environment: "node"` over `*.test.ts` with no jsdom and
 * no Testing Library - a `.tsx` page cannot be asserted on at all. So the
 * sequence lives in a pure module, where `tests/agent-overview-load.test.ts`
 * can watch the calls go out and prove the agent request is never made.
 *
 * It returns an outcome instead of calling `notFound()` or `redirect()`. Those
 * are Next.js functions that work by throwing, and a module that throws them
 * is a module that cannot be unit-tested without the framework. Mapping an
 * outcome onto them is the page's job, and the page does it in two `if`s.
 *
 * **404 means "not for you" here, and that is deliberate.** Both "no such
 * project" and "a project in a workspace you cannot see" come back from the
 * API as 404 with no way to tell them apart - which is the API's own rule, so
 * that someone guessing ids learns nothing. The wrong-workspace and
 * wrong-project comparisons below join that answer rather than inventing a
 * 403. Anything that is *not* a 404 stays an error: an API that is down must
 * never be reported as an agent that does not exist.
 */

import {
  getAgent,
  getProject,
  listExecutions,
  type Agent,
  type AgentExecution,
} from "@/lib/api/agents";
import { ApiError } from "@/lib/api/client";
import type { Project } from "@/lib/api/tenancy";

export type AgentOverview =
  /** No such agent, or none the viewer may see through this path. */
  | { readonly kind: "not_found" }
  /** Something went wrong that is not an answer about this agent. */
  | {
      readonly kind: "error";
      readonly message: string;
      readonly requestId?: string | undefined;
    }
  | { readonly kind: "ok"; readonly project: Project; readonly agent: Agent };

const NOT_FOUND: AgentOverview = { kind: "not_found" };

/**
 * One agent and the project it belongs to, or why neither can be shown.
 *
 * The sequence is the point:
 *
 * 1. the project, because its workspace is what the path has to match;
 * 2. the workspace comparison - and on a mismatch it stops here, with no
 *    agent request made. The API scopes reads to the caller's memberships
 *    rather than to the workspace in the URL, so it will happily answer 200
 *    for a project of theirs reached under another workspace's name. Letting
 *    that render would show one workspace's data under another's heading;
 * 3. the agent, now that the path is known to be coherent;
 * 4. a comparison of `agent.project_id` against the path. The repository
 *    already filters on `project_id`, so this never fires in practice - it is
 *    defence in depth against that filter being dropped, not the thing
 *    enforcing it.
 */
export async function loadAgentOverview(
  token: string,
  workspaceId: string,
  projectId: string,
  agentId: string,
): Promise<AgentOverview> {
  let project: Project;
  try {
    project = await getProject(token, projectId);
  } catch (caught) {
    return outcomeOf(caught, "Could not load this project");
  }

  // The project exists and the viewer may read it - but not necessarily here.
  if (project.workspace_id !== workspaceId) return NOT_FOUND;

  let agent: Agent;
  try {
    agent = await getAgent(token, projectId, agentId);
  } catch (caught) {
    return outcomeOf(caught, "Could not load this agent");
  }

  if (agent.project_id !== projectId) return NOT_FOUND;

  return { kind: "ok", project, agent };
}

/**
 * What AgentWorld ran for this agent, or why the list cannot be shown.
 *
 * Separate from `loadAgentOverview` rather than folded into it, and the
 * difference is what a failure should cost. The overview decides whether the
 * page exists at all; this decides whether one card on it has content. An
 * execution list that cannot be read must not take the page down with it - the
 * agent's status and heartbeat are still worth seeing - so there is no
 * `not_found` member here: a 404 after the agent has already loaded is a race,
 * and it reads as an error in that card rather than as a missing page.
 */
export type AgentExecutions =
  | {
      readonly kind: "error";
      readonly message: string;
      readonly requestId?: string | undefined;
    }
  | { readonly kind: "ok"; readonly executions: readonly AgentExecution[] };

export async function loadAgentExecutions(
  token: string,
  projectId: string,
  agentId: string,
  limit: number,
): Promise<AgentExecutions> {
  try {
    return { kind: "ok", executions: await listExecutions(token, projectId, agentId, { limit }) };
  } catch (caught) {
    if (caught instanceof ApiError) {
      return {
        kind: "error",
        message: caught.problem.detail,
        requestId: caught.requestId,
      };
    }
    return { kind: "error", message: "Could not load this agent's executions" };
  }
}

/**
 * A failed call as an outcome.
 *
 * Only an `ApiError` carrying a 404 becomes `not_found`. Every other
 * `ApiError` keeps the API's own `detail`, which is written for a reader, and
 * the request id so a report can be traced to a log line. Anything else is a
 * thrown value we know nothing about - a `TypeError` from an unreachable
 * host, or a bug in this code - and gets the caller's generic sentence rather
 * than a message assembled from an exception the user cannot act on.
 */
function outcomeOf(caught: unknown, fallback: string): AgentOverview {
  if (caught instanceof ApiError) {
    if (caught.problem.status === 404) return NOT_FOUND;
    return { kind: "error", message: caught.problem.detail, requestId: caught.requestId };
  }
  return { kind: "error", message: fallback };
}
