/**
 * The order the agent detail page loads in, and what it refuses.
 *
 * These are authorization tests, not plumbing tests. The claim each one pins
 * is that a path the viewer may not have becomes "not found" - never a 403,
 * never an error card - and that a mismatched workspace stops the sequence
 * *before* the agent is requested at all. The second half of that is why the
 * assertions below are on the list of calls that went out, not only on the
 * value that came back: a loader that asked for the agent first and then
 * refused would return the same answer and still leak the request.
 *
 * The matching claim on the other side: an API that is down is not an agent
 * that does not exist. Every non-404 failure has to survive as an error.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { loadAgentExecutions, loadAgentOverview } from "@/lib/agents/load";
import type { Agent, AgentExecution } from "@/lib/api/agents";
import type { Project } from "@/lib/api/tenancy";

const WORKSPACE = "w-1";
const PROJECT_PATH = "/v1/projects/p-1";
const AGENT_PATH = "/v1/projects/p-1/agents/a-1";

const PROJECT: Project = {
  id: "p-1",
  workspace_id: WORKSPACE,
  slug: "acceptance",
  name: "Acceptance",
  description: null,
  created_at: "2026-11-01T00:00:00Z",
};

const AGENT: Agent = {
  id: "a-1",
  project_id: "p-1",
  agent_key: "scout",
  display_name: "Scout",
  interaction_mode: "observed",
  role: null,
  description: null,
  instructions: null,
  kind: null,
  labels: {},
  model_profile_id: null,
  model_overrides: {},
  dispatch: {},
  heartbeat_ttl_seconds: 60,
  status: "idle",
  status_reason: null,
  capabilities: [],
  current_task_id: null,
  last_heartbeat_at: "2026-11-16T10:00:00Z",
  archived_at: null,
  liveness: "fresh",
  seconds_since_heartbeat: 12,
};

interface Reply {
  readonly status: number;
  readonly body: unknown;
}

function ok(body: unknown): Reply {
  return { status: 200, body };
}

function problem(status: number, detail: string, instance?: string): Reply {
  return {
    status,
    body: {
      type: "https://docs.agentworld.dev/errors/problem",
      title: "Problem",
      status,
      detail,
      ...(instance === undefined ? {} : { instance }),
    },
  };
}

/** The path and query, without the configured origin. */
function path(url: string): string {
  return url.replace(/^https?:\/\/[^/]+/, "");
}

/**
 * A fetch that answers per path and records what was asked for.
 *
 * Every test stubs the agent route even where the loader is expected never to
 * reach it, so that a loader which asked anyway fails on the recorded calls
 * rather than on a missing fixture - the difference between proving the order
 * and proving the stub was incomplete.
 */
function captureFetch(replies: Readonly<Record<string, Reply>>): { paths: string[] } {
  const paths: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      const asked = path(url);
      paths.push(asked);
      const reply = replies[asked];
      if (reply === undefined) throw new Error(`unstubbed request: ${asked}`);
      return {
        ok: reply.status < 400,
        status: reply.status,
        statusText: "",
        json: async () => reply.body,
      };
    }),
  );
  return { paths };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("loadAgentOverview", () => {
  it("returns the project and the agent, asking for them in that order", async () => {
    const { paths } = captureFetch({ [PROJECT_PATH]: ok(PROJECT), [AGENT_PATH]: ok(AGENT) });

    const result = await loadAgentOverview("t", WORKSPACE, "p-1", "a-1");

    expect(result).toEqual({ kind: "ok", project: PROJECT, agent: AGENT });
    // The project first: its workspace is what the path has to match, and
    // nothing agent-scoped may be requested before that is settled.
    expect(paths).toEqual([PROJECT_PATH, AGENT_PATH]);
  });

  it("refuses a project reached through the wrong workspace, and asks for no agent", async () => {
    // The API scopes reads to the caller's memberships, not to the workspace
    // in the URL, so this project really does come back 200 - the refusal is
    // the frontend's to make, and it has to happen before the agent request.
    const { paths } = captureFetch({ [PROJECT_PATH]: ok(PROJECT), [AGENT_PATH]: ok(AGENT) });

    const result = await loadAgentOverview("t", "another-workspace", "p-1", "a-1");

    expect(result).toEqual({ kind: "not_found" });
    expect(paths).toEqual([PROJECT_PATH]);
  });

  it("turns a project 404 into not found, and asks for no agent", async () => {
    const { paths } = captureFetch({
      [PROJECT_PATH]: problem(404, "Project not found"),
      [AGENT_PATH]: ok(AGENT),
    });

    const result = await loadAgentOverview("t", WORKSPACE, "p-1", "a-1");

    expect(result).toEqual({ kind: "not_found" });
    expect(paths).toEqual([PROJECT_PATH]);
  });

  it("keeps a failing project request a failure, with its detail and request id", async () => {
    const { paths } = captureFetch({
      [PROJECT_PATH]: problem(503, "The database is unavailable", "req_abc123"),
      [AGENT_PATH]: ok(AGENT),
    });

    const result = await loadAgentOverview("t", WORKSPACE, "p-1", "a-1");

    expect(result).toEqual({
      kind: "error",
      message: "The database is unavailable",
      requestId: "req_abc123",
    });
    // An outage is not an answer about this agent, so nothing further is asked.
    expect(paths).toEqual([PROJECT_PATH]);
  });

  it("turns an agent 404 into not found", async () => {
    // The same answer the API gives for an agent in another project, so a
    // guess at an id learns nothing from the difference between them.
    const { paths } = captureFetch({
      [PROJECT_PATH]: ok(PROJECT),
      [AGENT_PATH]: problem(404, "Agent not found"),
    });

    const result = await loadAgentOverview("t", WORKSPACE, "p-1", "a-1");

    expect(result).toEqual({ kind: "not_found" });
    expect(paths).toEqual([PROJECT_PATH, AGENT_PATH]);
  });

  it("keeps a failing agent request a failure rather than a 404", async () => {
    const { paths } = captureFetch({
      [PROJECT_PATH]: ok(PROJECT),
      [AGENT_PATH]: problem(500, "Something broke", "req_def456"),
    });

    const result = await loadAgentOverview("t", WORKSPACE, "p-1", "a-1");

    expect(result).toEqual({
      kind: "error",
      message: "Something broke",
      requestId: "req_def456",
    });
    expect(paths).toEqual([PROJECT_PATH, AGENT_PATH]);
  });

  it("refuses an agent whose project is not the one in the path", async () => {
    // Defence in depth: the repository filters on `project_id` already, so
    // this should be unreachable. It exists for the day that filter is lost.
    const { paths } = captureFetch({
      [PROJECT_PATH]: ok(PROJECT),
      [AGENT_PATH]: ok({ ...AGENT, project_id: "p-other" }),
    });

    const result = await loadAgentOverview("t", WORKSPACE, "p-1", "a-1");

    expect(result).toEqual({ kind: "not_found" });
    expect(paths).toEqual([PROJECT_PATH, AGENT_PATH]);
  });

  it("loads an archived agent like any other", async () => {
    // The detail endpoint passes `include_archived=True`: an archived agent is
    // absent from the list but readable here, and what it did before being
    // archived is usually the question being asked.
    const archived: Agent = { ...AGENT, archived_at: "2026-11-10T09:30:00Z" };
    captureFetch({ [PROJECT_PATH]: ok(PROJECT), [AGENT_PATH]: ok(archived) });

    const result = await loadAgentOverview("t", WORKSPACE, "p-1", "a-1");

    expect(result).toEqual({ kind: "ok", project: PROJECT, agent: archived });
  });

  it("reports an unreachable API as an error, never as a missing agent", async () => {
    // What `fetch` itself throws when there is nothing listening: not an
    // `ApiError`, no problem document, no status to inspect.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("fetch failed");
      }),
    );

    const result = await loadAgentOverview("t", WORKSPACE, "p-1", "a-1");

    expect(result).toEqual({ kind: "error", message: "Could not load this project" });
  });
});


/**
 * The agent page's execution list, which is one card rather than the page.
 *
 * The claim worth pinning is the one `loadAgentOverview` does not make: this
 * loader has **no `not_found`**. A 404 here arrives after the agent has
 * already loaded - the agent exists, the viewer may read it - so it is a race
 * or a deletion mid-render, and turning it into `notFound()` would take down a
 * page whose status and heartbeat are still perfectly readable. It stays an
 * error inside the card.
 */
describe("loadAgentExecutions", () => {
  const EXECUTIONS_PATH = "/v1/projects/p-1/agents/a-1/executions?limit=50";

  const EXECUTION: AgentExecution = {
    id: "e-1",
    workflow_id: "wf-1",
    workflow_version_id: "v-1",
    status: "completed",
    initiator_kind: "user",
    initiator_label: "ana@example.com",
    input: {},
    output: { confirm: { text: "ACCEPTED" } },
    error: null,
    created_at: "2026-11-17T09:00:00Z",
    started_at: "2026-11-17T09:00:01Z",
    finished_at: "2026-11-17T09:00:04Z",
    is_test_harness: true,
  };

  it("returns what the agent took part in, asking for the limit it was given", async () => {
    const { paths } = captureFetch({ [EXECUTIONS_PATH]: ok([EXECUTION]) });

    const result = await loadAgentExecutions("t", "p-1", "a-1", 50);

    expect(result).toEqual({ kind: "ok", executions: [EXECUTION] });
    expect(paths).toEqual([EXECUTIONS_PATH]);
  });

  it("treats an empty list as an answer rather than as nothing", async () => {
    // An agent with no executions is the ordinary case for a `dispatchable`
    // or `observed` one, and the card has a sentence for it.
    captureFetch({ [EXECUTIONS_PATH]: ok([]) });

    expect(await loadAgentExecutions("t", "p-1", "a-1", 50)).toEqual({
      kind: "ok",
      executions: [],
    });
  });

  it("keeps the API's own words and its request id when it refuses", async () => {
    captureFetch({
      [EXECUTIONS_PATH]: problem(503, "The database is not accepting connections", "req_abc"),
    });

    expect(await loadAgentExecutions("t", "p-1", "a-1", 50)).toEqual({
      kind: "error",
      message: "The database is not accepting connections",
      requestId: "req_abc",
    });
  });

  it("does not turn a 404 into a missing page", async () => {
    // The whole point of this loader being separate: the outcome has no
    // `not_found` member, so a 404 cannot reach `notFound()` from here.
    captureFetch({ [EXECUTIONS_PATH]: problem(404, "Agent not found") });

    const result = await loadAgentExecutions("t", "p-1", "a-1", 50);

    expect(result).toEqual({ kind: "error", message: "Agent not found" });
    expect(result.kind).not.toBe("not_found");
  });

  it("falls back to its own sentence for a thrown value it knows nothing about", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("fetch failed");
      }),
    );

    expect(await loadAgentExecutions("t", "p-1", "a-1", 50)).toEqual({
      kind: "error",
      message: "Could not load this agent's executions",
    });
  });
});
