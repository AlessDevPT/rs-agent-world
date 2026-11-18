import { afterEach, describe, expect, it, vi } from "vitest";

import {
  DEFAULT_LIMIT,
  MAX_LIMIT,
  createAgent,
  getAgent,
  getProject,
  listAgents,
  listExecutions,
  listRuns,
  listStatusHistory,
} from "@/lib/api/agents";

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

  it("agrees with the API about its own caps", () => {
    // Mirrored constants, not guesses: `limit` is `ge=1, le=200` with a
    // default of 50 on all three listings, and there is no cursor.
    expect(DEFAULT_LIMIT).toBe(50);
    expect(MAX_LIMIT).toBe(200);
  });
});
