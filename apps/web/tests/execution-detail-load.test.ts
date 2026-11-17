/**
 * The order the execution detail page loads in, and what it refuses.
 *
 * Authorization tests, like `agent-overview-load.test.ts`, and the same claim
 * in a different place: a path the viewer may not have becomes "not found" -
 * never a 403, never an error card - and a mismatched workspace stops the
 * sequence *before* the execution is requested at all. The assertions are on
 * the list of calls that went out as well as on the value returned, because a
 * loader that asked first and refused afterwards would give the same answer
 * and still have leaked the request.
 *
 * The matching claim on the other side: an API that is down is not an
 * execution that does not exist, and a step list that fails to load is not an
 * execution with no steps.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import type { Execution, ExecutionStep } from "@/lib/api/executions";
import { loadExecutionDetail } from "@/lib/executions/load";
import type { Project } from "@/lib/api/tenancy";

const WORKSPACE = "w-1";
const PROJECT_PATH = "/v1/projects/p-1";
const EXECUTION_PATH = "/v1/projects/p-1/executions/e-1";
const STEPS_PATH = "/v1/projects/p-1/executions/e-1/steps";

const PROJECT: Project = {
  id: "p-1",
  workspace_id: WORKSPACE,
  slug: "acceptance",
  name: "Acceptance",
  description: null,
  created_at: "2026-11-01T00:00:00Z",
};

const EXECUTION: Execution = {
  id: "e-1",
  workflow_id: "wf-1",
  workflow_version_id: "v-1",
  status: "completed",
  initiator_kind: "user",
  initiator_label: "ana@example.com",
  input: {},
  output: { research: { text: "done" } },
  error: null,
  created_at: "2026-11-17T09:00:00Z",
  started_at: "2026-11-17T09:00:01Z",
  finished_at: "2026-11-17T09:00:04Z",
};

const STEPS: ExecutionStep[] = [
  {
    step_key: "research",
    kind: "agent_task",
    status: "succeeded",
    attempt: 0,
    agent_key: "researcher",
    started_at: "2026-11-17T09:00:01Z",
    finished_at: "2026-11-17T09:00:03Z",
    output: { text: "done" },
    error: null,
  },
];

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
 * Every test stubs all three routes even where the loader is expected never to
 * reach one, so a loader that asked anyway fails on the recorded calls rather
 * than on a missing fixture - the difference between proving the order and
 * proving the stub was incomplete.
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

function everything(): Readonly<Record<string, Reply>> {
  return {
    [PROJECT_PATH]: ok(PROJECT),
    [EXECUTION_PATH]: ok(EXECUTION),
    [STEPS_PATH]: ok(STEPS),
  };
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("loadExecutionDetail", () => {
  it("returns the project, the execution and its steps, asked for in that order", async () => {
    const { paths } = captureFetch(everything());

    const result = await loadExecutionDetail("t", WORKSPACE, "p-1", "e-1");

    expect(result).toEqual({
      kind: "ok",
      project: PROJECT,
      execution: EXECUTION,
      steps: STEPS,
    });
    // The project first: its workspace is what the path has to match, and
    // nothing execution-scoped may be requested before that is settled. The
    // steps last, because they are a route of their own.
    expect(paths).toEqual([PROJECT_PATH, EXECUTION_PATH, STEPS_PATH]);
  });

  it("refuses a project reached through the wrong workspace, and asks for nothing else", async () => {
    // The API scopes reads to the caller's memberships, not to the workspace
    // in the URL, so this project really does come back 200 - the refusal is
    // the frontend's to make, and it has to happen first.
    const { paths } = captureFetch(everything());

    const result = await loadExecutionDetail("t", "another-workspace", "p-1", "e-1");

    expect(result).toEqual({ kind: "not_found" });
    expect(paths).toEqual([PROJECT_PATH]);
  });

  it("turns a project 404 into not found, and asks for no execution", async () => {
    const { paths } = captureFetch({
      ...everything(),
      [PROJECT_PATH]: problem(404, "Project not found"),
    });

    const result = await loadExecutionDetail("t", WORKSPACE, "p-1", "e-1");

    expect(result).toEqual({ kind: "not_found" });
    expect(paths).toEqual([PROJECT_PATH]);
  });

  it("turns an execution 404 into not found, and asks for no steps", async () => {
    const { paths } = captureFetch({
      ...everything(),
      [EXECUTION_PATH]: problem(404, "Execution not found"),
    });

    const result = await loadExecutionDetail("t", WORKSPACE, "p-1", "e-1");

    expect(result).toEqual({ kind: "not_found" });
    expect(paths).toEqual([PROJECT_PATH, EXECUTION_PATH]);
  });

  it("keeps a failure that is not an answer about this execution as an error", async () => {
    const { paths } = captureFetch({
      ...everything(),
      [EXECUTION_PATH]: problem(503, "The database is not accepting connections", "req_abc"),
    });

    const result = await loadExecutionDetail("t", WORKSPACE, "p-1", "e-1");

    expect(result).toEqual({
      kind: "error",
      message: "The database is not accepting connections",
      requestId: "req_abc",
    });
    expect(paths).toEqual([PROJECT_PATH, EXECUTION_PATH]);
  });

  it("does not render half an execution when the steps fail to load", async () => {
    // The alternative - an execution with an empty step list - would read as
    // "this ran nothing", which is a different and false statement.
    const { paths } = captureFetch({
      ...everything(),
      [STEPS_PATH]: problem(500, "Something went wrong", "req_def"),
    });

    const result = await loadExecutionDetail("t", WORKSPACE, "p-1", "e-1");

    expect(result).toEqual({
      kind: "error",
      message: "Something went wrong",
      requestId: "req_def",
    });
    expect(paths).toEqual([PROJECT_PATH, EXECUTION_PATH, STEPS_PATH]);
  });

  it("falls back to its own sentence for a thrown value it knows nothing about", async () => {
    // What `fetch` throws when there is nothing listening: no problem
    // document, no status, nothing written for a reader.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new TypeError("fetch failed");
      }),
    );

    const result = await loadExecutionDetail("t", WORKSPACE, "p-1", "e-1");

    expect(result).toEqual({ kind: "error", message: "Could not load this project" });
  });
});
