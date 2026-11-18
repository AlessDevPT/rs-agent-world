/**
 * The order the "new agent" page loads in, which is its authorization rule.
 *
 * Same shape as `agent-overview-load.test.ts`, and asserting the same kind of
 * thing: that a project reached through the wrong workspace stops the sequence
 * before anything else is asked for. The API scopes reads to the caller's
 * memberships rather than to the workspace in the URL, so it answers 200 for
 * a project of theirs reached under another workspace's name - and rendering
 * that would show one workspace's data under another's heading.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { loadAgentCreation } from "@/lib/agents/load";

interface Call {
  readonly url: string;
}

const PROJECT = {
  id: "p-1",
  workspace_id: "w-1",
  slug: "research",
  name: "Research",
  description: null,
  created_at: null,
};

const PROFILE = {
  id: "mp-1",
  name: "Writer",
  provider_slug: "anthropic",
  model_id: "claude-opus-5",
  parameters: {},
  credential_id: "c-1",
  is_default: true,
};

/** Answers each path with its own body, and records the order they arrive. */
function stubFetch(answers: ReadonlyArray<{ status: number; body: unknown }>): {
  calls: Call[];
} {
  const calls: Call[] = [];
  let index = 0;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      calls.push({ url });
      const answer = answers[index++] ?? { status: 500, body: {} };
      return {
        ok: answer.status < 400,
        status: answer.status,
        statusText: "",
        json: async () => answer.body,
      };
    }),
  );
  return { calls };
}

function path(call: Call): string {
  return call.url.replace(/^https?:\/\/[^/]+/, "");
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("loading the create form", () => {
  it("asks for the project, then the workspace's profiles", async () => {
    const { calls } = stubFetch([
      { status: 200, body: PROJECT },
      { status: 200, body: [PROFILE] },
    ]);

    const outcome = await loadAgentCreation("t", "w-1", "p-1", { mayConfigure: true });

    expect(outcome.kind).toBe("ok");
    expect(outcome.kind === "ok" && outcome.project.name).toBe("Research");
    expect(outcome.kind === "ok" && outcome.profiles).toEqual([PROFILE]);
    expect(calls.map(path)).toEqual([
      "/v1/projects/p-1",
      "/v1/workspaces/w-1/model-profiles",
    ]);
  });

  it("stops at the workspace comparison, with no profile request made", async () => {
    const { calls } = stubFetch([
      { status: 200, body: { ...PROJECT, workspace_id: "w-other" } },
      { status: 200, body: [PROFILE] },
    ]);

    const outcome = await loadAgentCreation("t", "w-1", "p-1", { mayConfigure: true });

    expect(outcome).toEqual({ kind: "not_found" });
    expect(calls.map(path)).toEqual(["/v1/projects/p-1"]);
  });

  it("turns a 404 on the project into not_found", async () => {
    stubFetch([{ status: 404, body: { title: "Not found", status: 404, detail: "gone" } }]);

    expect(await loadAgentCreation("t", "w-1", "p-1", { mayConfigure: true })).toEqual({
      kind: "not_found",
    });
  });

  it("keeps a real failure a failure rather than calling it a missing project", async () => {
    stubFetch([
      {
        status: 503,
        body: { title: "Unavailable", status: 503, detail: "upstream is down", instance: "req_7" },
      },
    ]);

    const outcome = await loadAgentCreation("t", "w-1", "p-1", { mayConfigure: true });

    expect(outcome).toMatchObject({
      kind: "error",
      message: "upstream is down",
      requestId: "req_7",
    });
  });

  it("asks for no profiles at all when the viewer may not attach one", async () => {
    // Listing them is `viewer` on the API, so this is not about permission:
    // it is about not making a request whose answer the page cannot use.
    const { calls } = stubFetch([{ status: 200, body: PROJECT }]);

    const outcome = await loadAgentCreation("t", "w-1", "p-1", { mayConfigure: false });

    expect(outcome.kind === "ok" && outcome.profiles).toBeNull();
    expect(calls.map(path)).toEqual(["/v1/projects/p-1"]);
  });

  it("keeps the page when the profiles cannot be read, and says nothing about having none", async () => {
    // `null` and `[]` are different sentences. An agent without a model is a
    // legitimate thing to create, so losing the form over an optional card
    // would be the wrong trade.
    const { calls } = stubFetch([
      { status: 200, body: PROJECT },
      { status: 500, body: { title: "Error", status: 500, detail: "boom" } },
    ]);

    const outcome = await loadAgentCreation("t", "w-1", "p-1", { mayConfigure: true });

    expect(outcome.kind).toBe("ok");
    expect(outcome.kind === "ok" && outcome.profiles).toBeNull();
    expect(calls).toHaveLength(2);
  });
});
