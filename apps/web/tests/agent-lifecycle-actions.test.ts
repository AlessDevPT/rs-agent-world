/**
 * The three server actions behind editing and the archive lifecycle.
 *
 * Mocked down to the two things an action cannot do in a test - resolve a
 * session and revalidate a route - with the real API client underneath and
 * `fetch` stubbed. So what these assert is the actions' own behaviour: what
 * they send, what they refuse without asking, what they hand back on a
 * refusal, and which routes they discard afterwards.
 *
 * **Why the revalidation calls are asserted on every failing test.**
 * Dropping a page's cached data for a request that changed nothing is work
 * for no reason, and it tells the next reader that something happened. The
 * failing cases below all expect silence from `revalidatePath`; the passing
 * ones expect both the detail route and the list, because the list carries
 * the name, the role and the archived state too.
 *
 * There is no `redirect` here, deliberately. Editing an agent leaves you on
 * its page and archiving one leaves you looking at what you archived - only
 * creation moves you somewhere new, which is why `createAgentAction` is the
 * action that navigates.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));
vi.mock("@/lib/session", () => ({
  requireSession: vi.fn(async () => ({ token: "token-1", me: { workspaces: [] } })),
}));

import { revalidatePath } from "next/cache";

import { archiveAgentAction, restoreAgentAction, updateAgentAction } from "@/app/actions";
import type { UpdateAgentBody } from "@/lib/api/agents";

const AGENTS_PATH = "/w/w-1/p/p-1/agents";
const DETAIL_PATH = `${AGENTS_PATH}/a-1`;

interface Call {
  readonly url: string;
  readonly init: { method?: string; body?: unknown; headers?: Record<string, string> };
}

function stubFetch(status: number, body: unknown): { calls: Call[] } {
  const calls: Call[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string, init: Call["init"]) => {
      calls.push({ url, init });
      return { ok: status < 400, status, statusText: "", json: async () => body };
    }),
  );
  return { calls };
}

/** The path, without the configured origin. */
function path(url: string): string {
  return url.replace(/^https?:\/\/[^/]+/, "");
}

const AGENT = {
  id: "a-1",
  project_id: "p-1",
  agent_key: "market-researcher",
  display_name: "Market Researcher",
  interaction_mode: "managed",
  archived_at: null,
};

function problem(status: number, detail: string, extra: object = {}): object {
  return {
    type: "https://docs.agentworld.dev/errors/conflict",
    title: "Conflict",
    status,
    detail,
    instance: "req_abc",
    ...extra,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe("editing an agent", () => {
  it("patches the agent's own path and sends the body unchanged", async () => {
    const { calls } = stubFetch(200, AGENT);

    await updateAgentAction("w-1", "p-1", "a-1", {
      display_name: "Senior Market Researcher",
      role: null,
    });

    expect(path(calls[0]!.url)).toBe("/v1/projects/p-1/agents/a-1");
    expect(calls[0]!.init.method).toBe("PATCH");
    expect(calls[0]!.init.headers?.["authorization"]).toBe("Bearer token-1");
    expect(JSON.parse(String(calls[0]!.init.body))).toEqual({
      display_name: "Senior Market Researcher",
      role: null,
    });
  });

  it("sends only the fields it was given, filling nothing in", async () => {
    const { calls } = stubFetch(200, AGENT);

    await updateAgentAction("w-1", "p-1", "a-1", { role: "Analyst" });

    expect(Object.keys(JSON.parse(String(calls[0]!.init.body)))).toEqual(["role"]);
  });

  it("refuses an empty patch without asking the API", async () => {
    // `AgentService.update` accepts a body that writes no column and records
    // an `agent.update` audit row for it.
    const { calls } = stubFetch(200, AGENT);

    const result = await updateAgentAction("w-1", "p-1", "a-1", {});

    expect(result).toEqual({ ok: false, message: "Nothing was changed" });
    expect(calls).toHaveLength(0);
    expect(revalidatePath).not.toHaveBeenCalled();
  });

  it("refuses a null for a NOT NULL column without asking the API", async () => {
    // The same refusal the API makes, one step earlier and with the same
    // field locator, so a form puts it on the same input either way.
    const { calls } = stubFetch(200, AGENT);
    const posted = { display_name: null } as unknown as UpdateAgentBody;

    const result = await updateAgentAction("w-1", "p-1", "a-1", posted);

    expect(result).toMatchObject({ ok: false, field: "display_name" });
    expect(result.ok === false && result.message).toContain("cannot be null");
    expect(calls).toHaveLength(0);
    expect(revalidatePath).not.toHaveBeenCalled();
  });

  it("refuses a field this milestone does not edit, without asking the API", async () => {
    // All three are accepted by the endpoint. `model_overrides` and
    // `dispatch` are stored and read by nothing, so a caller who believed
    // they had set one would never find out otherwise; `interaction_mode`
    // changes what an agent is, and no screen explains that yet.
    for (const field of ["interaction_mode", "model_overrides", "dispatch"]) {
      vi.clearAllMocks();
      vi.unstubAllGlobals();
      const { calls } = stubFetch(200, AGENT);
      const posted = { [field]: "whatever" } as unknown as UpdateAgentBody;

      const result = await updateAgentAction("w-1", "p-1", "a-1", posted);

      expect(result).toMatchObject({ ok: false, field });
      expect(calls).toHaveLength(0);
      expect(revalidatePath).not.toHaveBeenCalled();
    }
  });

  it("forwards the two configuration fields the editor offers", async () => {
    // Both are in the API's `CONFIGURATION_FIELDS`, so the endpoint demands
    // `admin` for them. That is the API's gate and not this action's: the
    // form declines to draw them for a member, and a body that arrives here
    // carrying them is sent and refused on its merits.
    const { calls } = stubFetch(200, AGENT);

    const result = await updateAgentAction("w-1", "p-1", "a-1", {
      model_profile_id: "mp-2",
      heartbeat_ttl_seconds: 900,
    });

    expect(result.ok).toBe(true);
    expect(JSON.parse(String(calls[0]!.init.body))).toEqual({
      model_profile_id: "mp-2",
      heartbeat_ttl_seconds: 900,
    });
  });

  it("refuses the whole body rather than dropping the unsupported half", async () => {
    const { calls } = stubFetch(200, AGENT);
    const posted = {
      role: "Analyst",
      model_overrides: { temperature: 0.2 },
    } as unknown as UpdateAgentBody;

    const result = await updateAgentAction("w-1", "p-1", "a-1", posted);

    expect(result).toMatchObject({ ok: false, field: "model_overrides" });
    expect(calls).toHaveLength(0);
  });

  it("treats a key set to undefined as nothing changed", async () => {
    // `JSON.stringify` would drop it, so the request would be an empty
    // PATCH - accepted by the API, writing no column, and still audited.
    const { calls } = stubFetch(200, AGENT);

    const posted = { role: undefined } as unknown as UpdateAgentBody;

    const result = await updateAgentAction("w-1", "p-1", "a-1", posted);

    expect(result).toEqual({ ok: false, message: "Nothing was changed" });
    expect(calls).toHaveLength(0);
  });

  it("still clears a nullable field", async () => {
    const { calls } = stubFetch(200, { ...AGENT, role: null, model_profile_id: null });

    const result = await updateAgentAction("w-1", "p-1", "a-1", {
      role: null,
      model_profile_id: null,
    });

    expect(result.ok).toBe(true);
    expect(JSON.parse(String(calls[0]!.init.body))).toEqual({
      role: null,
      model_profile_id: null,
    });
  });

  it("refreshes the agent's page and the project's list, in that order", async () => {
    stubFetch(200, AGENT);

    await updateAgentAction("w-1", "p-1", "a-1", { role: "Analyst" });

    expect(vi.mocked(revalidatePath).mock.calls.map(([route]) => route)).toEqual([
      DETAIL_PATH,
      AGENTS_PATH,
    ]);
  });

  it("keeps a member's 403 as a refusal, with its status", async () => {
    // Naming a model profile is `agent.configure`, which is `admin`. The
    // page may hide the select; this is the boundary that holds.
    stubFetch(403, problem(403, "This action needs the admin role"));

    const result = await updateAgentAction("w-1", "p-1", "a-1", { model_profile_id: "mp-1" });

    expect(result).toMatchObject({
      ok: false,
      status: 403,
      message: "This action needs the admin role",
      requestId: "req_abc",
    });
    expect(revalidatePath).not.toHaveBeenCalled();
  });

  it("keeps the 409 for an archived agent", async () => {
    stubFetch(409, problem(409, "This agent is archived, so it cannot be edited"));

    const result = await updateAgentAction("w-1", "p-1", "a-1", { role: "Analyst" });

    expect(result).toMatchObject({ ok: false, status: 409 });
    expect(result.ok === false && result.message).toContain("archived");
    expect(revalidatePath).not.toHaveBeenCalled();
  });

  it("keeps the locator on a 409 for a profile deleted mid-request", async () => {
    // `agents_model_profile_id_fkey` through `CONFLICT_FOREIGN_KEYS`: the
    // one foreign-key conflict this endpoint maps, and it carries `field`.
    stubFetch(
      409,
      problem(409, "That model profile was deleted while this request was in flight.", {
        field: "model_profile_id",
      }),
    );

    const result = await updateAgentAction("w-1", "p-1", "a-1", { model_profile_id: "mp-gone" });

    expect(result).toMatchObject({ ok: false, status: 409, field: "model_profile_id" });
    expect(revalidatePath).not.toHaveBeenCalled();
  });

  it("keeps the locator on a 422 for a profile from another workspace", async () => {
    stubFetch(
      422,
      problem(422, "This workspace has no such model profile", {
        field: "model_profile_id",
        status: 422,
      }),
    );

    const result = await updateAgentAction("w-1", "p-1", "a-1", { model_profile_id: "mp-other" });

    expect(result).toMatchObject({ ok: false, status: 422, field: "model_profile_id" });
    expect(revalidatePath).not.toHaveBeenCalled();
  });
});

describe("archiving an agent", () => {
  it("deletes the agent's own path and parses nothing", async () => {
    // 204: `apiFetch` returns before touching the body.
    const { calls } = stubFetch(204, undefined);

    const result = await archiveAgentAction("w-1", "p-1", "a-1");

    expect(result).toEqual({ ok: true });
    expect(path(calls[0]!.url)).toBe("/v1/projects/p-1/agents/a-1");
    expect(calls[0]!.init.method).toBe("DELETE");
    expect(calls[0]!.init.body).toBeUndefined();
  });

  it("refreshes the agent's page and the project's list after it lands", async () => {
    stubFetch(204, undefined);

    await archiveAgentAction("w-1", "p-1", "a-1");

    expect(vi.mocked(revalidatePath).mock.calls.map(([route]) => route)).toEqual([
      DETAIL_PATH,
      AGENTS_PATH,
    ]);
  });

  it("reports an already-archived 409 rather than claiming success", async () => {
    stubFetch(409, problem(409, "This agent is already archived"));

    const result = await archiveAgentAction("w-1", "p-1", "a-1");

    expect(result).toMatchObject({ ok: false, status: 409 });
    expect(result.ok === false && result.message).toBe("This agent is already archived");
    expect(revalidatePath).not.toHaveBeenCalled();
  });

  it("reports a member's 403 and refreshes nothing", async () => {
    stubFetch(403, problem(403, "This action needs the admin role"));

    const result = await archiveAgentAction("w-1", "p-1", "a-1");

    expect(result).toMatchObject({ ok: false, status: 403 });
    expect(revalidatePath).not.toHaveBeenCalled();
  });

  it("scopes the request and the refreshed routes to the project it was given", async () => {
    const { calls } = stubFetch(204, undefined);

    await archiveAgentAction("w-9", "p-9", "a-9");

    expect(path(calls[0]!.url)).toBe("/v1/projects/p-9/agents/a-9");
    expect(vi.mocked(revalidatePath).mock.calls.map(([route]) => route)).toEqual([
      "/w/w-9/p/p-9/agents/a-9",
      "/w/w-9/p/p-9/agents",
    ]);
  });
});

describe("restoring an agent", () => {
  it("posts to the restore path with no body and hands the agent back", async () => {
    const { calls } = stubFetch(200, { ...AGENT, archived_at: null });

    const result = await restoreAgentAction("w-1", "p-1", "a-1");

    expect(path(calls[0]!.url)).toBe("/v1/projects/p-1/agents/a-1/restore");
    expect(calls[0]!.init.method).toBe("POST");
    expect(calls[0]!.init.body).toBeUndefined();
    expect(result).toMatchObject({ ok: true, agent: { id: "a-1" } });
  });

  it("refreshes the agent's page and the project's list after it lands", async () => {
    stubFetch(200, AGENT);

    await restoreAgentAction("w-1", "p-1", "a-1");

    expect(vi.mocked(revalidatePath).mock.calls.map(([route]) => route)).toEqual([
      DETAIL_PATH,
      AGENTS_PATH,
    ]);
  });

  it("keeps the 404 as the API wrote it, and refreshes nothing", async () => {
    // One 404 for three causes - a live agent, another project's agent, an
    // unknown id - so it is returned rather than rephrased.
    stubFetch(404, {
      type: "https://docs.agentworld.dev/errors/not_found",
      title: "Not found",
      status: 404,
      detail: "Agent not found",
    });

    const result = await restoreAgentAction("w-1", "p-1", "a-1");

    expect(result).toMatchObject({ ok: false, status: 404, message: "Agent not found" });
    expect(revalidatePath).not.toHaveBeenCalled();
  });

  it("reports a member's 403 and refreshes nothing", async () => {
    stubFetch(403, problem(403, "This action needs the admin role"));

    const result = await restoreAgentAction("w-1", "p-1", "a-1");

    expect(result).toMatchObject({ ok: false, status: 403 });
    expect(revalidatePath).not.toHaveBeenCalled();
  });
});
