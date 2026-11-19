/**
 * The order the edit page loads in, which is its authorization rule.
 *
 * The same rule `agent-overview-load.test.ts` and `agent-create-load.test.ts`
 * assert, and asserted again here because the edit page is a third path to
 * the same agent: a project reached through the wrong workspace must stop the
 * sequence before the agent is asked for. The API scopes reads to the
 * caller's memberships rather than to the workspace in the URL, so it answers
 * 200 for a project of theirs reached under another workspace's name.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { loadAgentEditing } from "@/lib/agents/load";

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

const AGENT = {
  id: "a-1",
  project_id: "p-1",
  agent_key: "market-researcher",
  display_name: "Market Researcher",
  interaction_mode: "managed",
  labels: {},
  heartbeat_ttl_seconds: 60,
  archived_at: null,
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

const ADMIN = { mayConfigure: true };
const MEMBER = { mayConfigure: false };

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("loading the edit form", () => {
  it("asks for the project, then the agent, then the profiles", async () => {
    const { calls } = stubFetch([
      { status: 200, body: PROJECT },
      { status: 200, body: AGENT },
      { status: 200, body: [PROFILE] },
    ]);

    const outcome = await loadAgentEditing("t", "w-1", "p-1", "a-1", ADMIN);

    expect(calls.map(path)).toEqual([
      "/v1/projects/p-1",
      "/v1/projects/p-1/agents/a-1",
      "/v1/workspaces/w-1/model-profiles",
    ]);
    expect(outcome).toMatchObject({ kind: "ok", agent: { id: "a-1" } });
  });

  it("stops at the project when the workspace does not match the path", async () => {
    // The agent is never requested: rendering it would show one workspace's
    // data under another's heading.
    const { calls } = stubFetch([{ status: 200, body: { ...PROJECT, workspace_id: "w-2" } }]);

    const outcome = await loadAgentEditing("t", "w-1", "p-1", "a-1", ADMIN);

    expect(outcome).toEqual({ kind: "not_found" });
    expect(calls.map(path)).toEqual(["/v1/projects/p-1"]);
  });

  it("stops at the agent when it belongs to another project", async () => {
    const { calls } = stubFetch([
      { status: 200, body: PROJECT },
      { status: 200, body: { ...AGENT, project_id: "p-9" } },
    ]);

    const outcome = await loadAgentEditing("t", "w-1", "p-1", "a-1", ADMIN);

    expect(outcome).toEqual({ kind: "not_found" });
    expect(calls).toHaveLength(2);
  });

  it("does not ask for profiles a member could not send anyway", async () => {
    const { calls } = stubFetch([
      { status: 200, body: PROJECT },
      { status: 200, body: AGENT },
    ]);

    const outcome = await loadAgentEditing("t", "w-1", "p-1", "a-1", MEMBER);

    expect(calls).toHaveLength(2);
    expect(outcome).toMatchObject({ kind: "ok", profiles: null });
  });

  it("keeps the form when the profile listing fails", async () => {
    // Editing a name must not be blocked by a card about models. Null, not
    // `[]`: "could not ask" and "this workspace has none" are two sentences.
    const { calls } = stubFetch([
      { status: 200, body: PROJECT },
      { status: 200, body: AGENT },
      { status: 500, body: {} },
    ]);

    const outcome = await loadAgentEditing("t", "w-1", "p-1", "a-1", ADMIN);

    expect(calls).toHaveLength(3);
    expect(outcome).toMatchObject({ kind: "ok", profiles: null });
  });

  it("reports a missing agent as not found, and a broken API as an error", async () => {
    const missing = stubFetch([
      { status: 200, body: PROJECT },
      { status: 404, body: { title: "Not found", status: 404, detail: "Agent not found" } },
    ]);
    expect(await loadAgentEditing("t", "w-1", "p-1", "a-1", ADMIN)).toEqual({
      kind: "not_found",
    });
    expect(missing.calls).toHaveLength(2);

    vi.unstubAllGlobals();
    stubFetch([
      { status: 200, body: PROJECT },
      { status: 503, body: { title: "Unavailable", status: 503, detail: "Upstream is down" } },
    ]);
    expect(await loadAgentEditing("t", "w-1", "p-1", "a-1", ADMIN)).toMatchObject({
      kind: "error",
      message: "Upstream is down",
    });
  });

  it("opens an archived agent, and leaves the refusal to the page", async () => {
    // The detail endpoint answers for archived rows, so the loader does not
    // second-guess it: the page is what declines to draw a form.
    stubFetch([
      { status: 200, body: PROJECT },
      { status: 200, body: { ...AGENT, archived_at: "2026-01-01T00:00:00Z" } },
      { status: 200, body: [PROFILE] },
    ]);

    const outcome = await loadAgentEditing("t", "w-1", "p-1", "a-1", ADMIN);

    expect(outcome).toMatchObject({
      kind: "ok",
      agent: { archived_at: "2026-01-01T00:00:00Z" },
    });
  });
});
