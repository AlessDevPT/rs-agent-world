import { afterEach, describe, expect, it, vi } from "vitest";

import {
  DEFAULT_LIMIT,
  MAX_LIMIT,
  archiveAgent,
  createAgent,
  getAgent,
  getProject,
  listAgents,
  listExecutions,
  listRuns,
  listStatusHistory,
  restoreAgent,
  testAgent,
  updateAgent,
} from "@/lib/api/agents";
import { ApiError } from "@/lib/api/client";

interface Call {
  readonly url: string;
  readonly init: RequestInit & { headers: Record<string, string> };
}

function captureFetch(body: unknown = []): { calls: Call[] } {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: Call["init"]) => {
      calls.push({ url, init });
      return { ok: true, status: 200, statusText: "", json: async () => body };
    }),
  );
  return { calls };
}

/** The path and query, without the configured origin. */
function path(url: string): string {
  return url.replace(/^https?:\/\/[^/]+/, "");
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the agents client", () => {
  it("builds the documented paths", async () => {
    const { calls } = captureFetch();

    await getProject("t", "p-1");
    await listAgents("t", "p-1");
    await getAgent("t", "p-1", "a-1");
    await listStatusHistory("t", "p-1", "a-1");
    await listRuns("t", "p-1", "a-1");
    await listExecutions("t", "p-1", "a-1");

    expect(calls.map((call) => `${call.init.method ?? "GET"} ${path(call.url)}`)).toEqual([
      "GET /v1/projects/p-1",
      "GET /v1/projects/p-1/agents",
      "GET /v1/projects/p-1/agents/a-1",
      "GET /v1/projects/p-1/agents/a-1/status-history",
      "GET /v1/projects/p-1/agents/a-1/runs",
      "GET /v1/projects/p-1/agents/a-1/executions",
    ]);
  });

  it("asks for archived agents only when told to", async () => {
    // `include_archived=false` is the API's own default, so sending it would
    // put noise in the request log for no change in the answer.
    const { calls } = captureFetch();

    await listAgents("t", "p-1");
    await listAgents("t", "p-1", { includeArchived: false });
    await listAgents("t", "p-1", { includeArchived: true });

    expect(calls.map((call) => path(call.url))).toEqual([
      "/v1/projects/p-1/agents",
      "/v1/projects/p-1/agents",
      "/v1/projects/p-1/agents?include_archived=true",
    ]);
  });

  it("passes a limit through and omits it otherwise", async () => {
    const { calls } = captureFetch();

    await listStatusHistory("t", "p-1", "a-1", { limit: 10 });
    await listRuns("t", "p-1", "a-1", { limit: MAX_LIMIT });
    await listExecutions("t", "p-1", "a-1", {});

    expect(calls.map((call) => path(call.url))).toEqual([
      "/v1/projects/p-1/agents/a-1/status-history?limit=10",
      "/v1/projects/p-1/agents/a-1/runs?limit=200",
      "/v1/projects/p-1/agents/a-1/executions",
    ]);
  });

  it("sends the token as a bearer credential and never the ingest header", async () => {
    const { calls } = captureFetch();
    await listAgents("the-token", "p-1");

    expect(calls[0]!.init.headers["authorization"]).toBe("Bearer the-token");
    expect(calls[0]!.init.headers["x-agentworld-key"]).toBeUndefined();
  });

  it("never caches a monitoring read", async () => {
    // A stale agent status is worse than no agent status.
    const { calls } = captureFetch();
    await getAgent("t", "p-1", "a-1");
    expect(calls[0]!.init.cache).toBe("no-store");
  });

  it("sends no body and no JSON content type on a read", async () => {
    const { calls } = captureFetch();

    await listRuns("t", "p-1", "a-1");

    expect(calls[0]!.init.body).toBeUndefined();
    expect(calls[0]!.init.headers["content-type"]).toBeUndefined();
  });

  it("posts a create body unchanged, to the project's agents path", async () => {
    // The one write in this module. What to send is decided in
    // `lib/agents/create.ts` and tested there; a client that second-guessed
    // a body would be the bug, so the whole of this is that it does not.
    const { calls } = captureFetch({});

    await createAgent("t", "p-1", {
      display_name: "Market Researcher",
      interaction_mode: "managed",
      agent_key: "research-bot",
      labels: { team: "support" },
    });

    expect(path(calls[0]!.url)).toBe("/v1/projects/p-1/agents");
    expect(calls[0]!.init.method).toBe("POST");
    expect(calls[0]!.init.headers["content-type"]).toBe("application/json");
    expect(JSON.parse(String(calls[0]!.init.body))).toEqual({
      display_name: "Market Researcher",
      interaction_mode: "managed",
      agent_key: "research-bot",
      labels: { team: "support" },
    });
  });

  it("builds the documented paths for the three writes", async () => {
    // Archiving is a `DELETE` on the agent itself and restoring is a `POST`
    // to a sub-path: an archive is not a deletion, and a restore is a verb
    // rather than a field somebody patches back.
    const { calls } = captureFetch({});

    await updateAgent("t", "p-1", "a-1", { role: "Analyst" });
    await archiveAgent("t", "p-1", "a-1");
    await restoreAgent("t", "p-1", "a-1");

    expect(calls.map((call) => `${call.init.method ?? "GET"} ${path(call.url)}`)).toEqual([
      "PATCH /v1/projects/p-1/agents/a-1",
      "DELETE /v1/projects/p-1/agents/a-1",
      "POST /v1/projects/p-1/agents/a-1/restore",
    ]);
  });

  it("patches a body unchanged, including the nulls that clear a field", async () => {
    // What to send is decided in `lib/agents/edit.ts` and tested there. A
    // client that dropped a null would turn "clear the role" into a request
    // that says nothing, which is the one mistake this cannot make.
    const { calls } = captureFetch({});

    await updateAgent("t", "p-1", "a-1", {
      display_name: "Senior Market Researcher",
      role: null,
      labels: { team: "research" },
      model_profile_id: null,
    });

    expect(calls[0]!.init.headers["content-type"]).toBe("application/json");
    expect(JSON.parse(String(calls[0]!.init.body))).toEqual({
      display_name: "Senior Market Researcher",
      role: null,
      labels: { team: "research" },
      model_profile_id: null,
    });
  });

  it("sends no body on either lifecycle call", async () => {
    // Both are verbs with nothing to say: the id in the path is the whole
    // of the request, and a JSON content type on a bodyless call is noise.
    const { calls } = captureFetch({});

    await archiveAgent("t", "p-1", "a-1");
    await restoreAgent("t", "p-1", "a-1");

    for (const call of calls) {
      expect(call.init.body).toBeUndefined();
      expect(call.init.headers["content-type"]).toBeUndefined();
      expect(call.init.headers["authorization"]).toBe("Bearer t");
    }
  });

  it("posts a test to the project-nested path with the sample as its only field", async () => {
    // The route is nested under the project like every other agent route -
    // the flat `/v1/agents/{id}/test` of the design document is not served -
    // and `AgentTestRequest` is `extra="forbid"`, so one field is not a
    // minimum here, it is the whole of what may be sent.
    const { calls } = captureFetch({});

    await testAgent("t", "p-1", "a-1", { input: "Summarise the fintech funding news." });

    expect(path(calls[0]!.url)).toBe("/v1/projects/p-1/agents/a-1/test");
    expect(calls[0]!.init.method).toBe("POST");
    expect(calls[0]!.init.headers["content-type"]).toBe("application/json");
    expect(calls[0]!.init.headers["authorization"]).toBe("Bearer t");
    expect(JSON.parse(String(calls[0]!.init.body))).toEqual({
      input: "Summarise the fintech funding news.",
    });
  });

  it("returns the accepted execution, which has not answered anything yet", async () => {
    // The 202 body: the row exists and the turn has not happened. A client
    // that typed this as a result would be describing a reply that has not
    // been asked for.
    const accepted = {
      id: "e-1",
      workflow_id: "w-9",
      workflow_version_id: "v-9",
      status: "running",
      initiator_kind: "user",
      initiator_label: "someone@example.com",
      input: { input: "Say hello." },
      output: null,
      error: null,
      created_at: "2026-11-19T10:00:00Z",
      started_at: "2026-11-19T10:00:00Z",
      finished_at: null,
    };
    // 202 explicitly, not the 200 `captureFetch` stubs: the status is the
    // half of this endpoint's contract that says the turn has not happened.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({ ok: true, status: 202, statusText: "", json: async () => accepted })),
    );

    const execution = await testAgent("t", "p-1", "a-1", { input: "Say hello." });

    expect(execution).toEqual(accepted);
    expect(execution.output).toBeNull();
    expect(execution.finished_at).toBeNull();
  });

  it("preserves an agent_not_testable problem, findings and all", async () => {
    // The refusal that matters: three different reasons share one status,
    // and only `errors[].code` tells them apart. The pointer is carried as
    // the API wrote it; turning it into a sentence is the action's job.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: false,
        status: 422,
        statusText: "",
        json: async () => ({
          type: "https://agentworld.dev/problems/agent_not_testable",
          title: "Agent not testable",
          status: 422,
          detail: "This agent cannot be tested",
          instance: "req-77",
          errors: [
            {
              pointer: "/agents/9a1f0c6e-0000-4000-8000-000000000001",
              code: "observed_agent",
              detail: "'watcher' only reports its own activity",
            },
          ],
        }),
      })),
    );

    const caught = await testAgent("t", "p-1", "a-1", { input: "Say hello." }).catch(
      (error: unknown) => error,
    );

    expect(caught).toBeInstanceOf(ApiError);
    if (!(caught instanceof ApiError)) return;
    expect(caught.problem.status).toBe(422);
    expect(caught.requestId).toBe("req-77");
    expect(caught.problem.errors?.[0]?.code).toBe("observed_agent");
  });

  it("agrees with the API about its own caps", () => {
    // Mirrored constants, not guesses: `limit` is `ge=1, le=200` with a
    // default of 50 on all three listings, and there is no cursor.
    expect(DEFAULT_LIMIT).toBe(50);
    expect(MAX_LIMIT).toBe(200);
  });
});
