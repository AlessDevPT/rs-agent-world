/**
 * The server action behind the "new agent" form.
 *
 * Mocked down to the three things an action cannot do in a test - resolve a
 * session, revalidate a route and redirect - with the real API client
 * underneath and `fetch` stubbed. So what these assert is the action's own
 * behaviour: what it sends, where it navigates, what it returns on a refusal,
 * and that a failed create tells Next nothing.
 *
 * **Why the cache and redirect calls matter.** `revalidatePath` after a
 * failed write would discard the page's data for no reason and imply the
 * write landed; a `redirect` after one would send somebody to an agent that
 * does not exist. Every failing test below expects silence from both.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));
vi.mock("next/navigation", () => ({ redirect: vi.fn() }));
vi.mock("@/lib/session", () => ({
  requireSession: vi.fn(async () => ({ token: "token-1", me: { workspaces: [] } })),
}));

import { revalidatePath } from "next/cache";
import { redirect } from "next/navigation";

import { createAgentAction } from "@/app/actions";

const AGENTS_PATH = "/w/w-1/p/p-1/agents";

interface Call {
  readonly url: string;
  readonly init: { method?: string; body?: unknown };
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

const AGENT = {
  id: "a-1",
  project_id: "p-1",
  agent_key: "market-researcher",
  display_name: "Market Researcher",
  interaction_mode: "managed",
};

const BODY = {
  display_name: "Market Researcher",
  interaction_mode: "managed",
} as const;

beforeEach(() => {
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe("creating an agent", () => {
  it("posts to the project's agents path and sends the body unchanged", async () => {
    const { calls } = stubFetch(201, AGENT);

    await createAgentAction("w-1", "p-1", {
      display_name: "Market Researcher",
      interaction_mode: "managed",
      agent_key: "research-bot",
      labels: { team: "support" },
    });

    expect(calls[0]!.url).toContain("/v1/projects/p-1/agents");
    expect(calls[0]!.init.method).toBe("POST");
    expect(JSON.parse(String(calls[0]!.init.body))).toEqual({
      display_name: "Market Researcher",
      interaction_mode: "managed",
      agent_key: "research-bot",
      labels: { team: "support" },
    });
  });

  it("refreshes the list and goes to the new agent's own page", async () => {
    stubFetch(201, AGENT);

    await createAgentAction("w-1", "p-1", BODY);

    expect(revalidatePath).toHaveBeenCalledWith(AGENTS_PATH);
    expect(redirect).toHaveBeenCalledWith(`${AGENTS_PATH}/a-1`);
  });

  it("blames the key box for a duplicate, and navigates nowhere", async () => {
    // The API's 409 carries `agent_key` in its context, which is not a
    // locator and never reaches the body - so the action places it by status.
    stubFetch(409, {
      type: "https://docs.agentworld.dev/errors/conflict",
      title: "Conflict",
      status: 409,
      detail: "This project already has an agent with the key 'market-researcher'",
      instance: "req_abc",
    });

    const result = await createAgentAction("w-1", "p-1", BODY);

    expect(result).toMatchObject({
      ok: false,
      field: "agent_key",
      requestId: "req_abc",
      message: "This project already has an agent with the key 'market-researcher'",
    });
    expect(revalidatePath).not.toHaveBeenCalled();
    expect(redirect).not.toHaveBeenCalled();
  });

  it("keeps the API's own locator on a model-mode refusal", async () => {
    stubFetch(422, {
      type: "https://docs.agentworld.dev/errors/validation_failed",
      title: "Validation failed",
      status: 422,
      detail: "Only a managed agent has a model",
      field: "model_profile_id",
    });

    const result = await createAgentAction("w-1", "p-1", BODY);

    expect(result).toMatchObject({ ok: false, field: "model_profile_id" });
    expect(redirect).not.toHaveBeenCalled();
  });

  it("leaves a slug refusal at form level rather than guessing", async () => {
    // `validated` raises with `slug=`, so this 422 arrives with nothing
    // pointing at an input.
    stubFetch(422, {
      type: "https://docs.agentworld.dev/errors/validation_failed",
      title: "Validation failed",
      status: 422,
      detail: "The slug 'new' is reserved",
    });

    const result = await createAgentAction("w-1", "p-1", BODY);

    expect(result).toMatchObject({ ok: false, message: "The slug 'new' is reserved" });
    expect(result.ok === false && result.field).toBeUndefined();
  });

  it("reports a role refusal as the API wrote it", async () => {
    // The page hides the model picker from a member; this is the boundary
    // that actually holds, and it is the API's.
    stubFetch(403, {
      type: "https://docs.agentworld.dev/errors/forbidden",
      title: "Forbidden",
      status: 403,
      detail: "This action needs the admin role",
    });

    const result = await createAgentAction("w-1", "p-1", {
      ...BODY,
      model_profile_id: "mp-1",
    });

    expect(result).toMatchObject({ ok: false, message: "This action needs the admin role" });
    expect(result.ok === false && result.field).toBeUndefined();
    expect(revalidatePath).not.toHaveBeenCalled();
    expect(redirect).not.toHaveBeenCalled();
  });

  it("says which fields a schema refusal was about", async () => {
    stubFetch(422, {
      type: "https://docs.agentworld.dev/errors/validation_failed",
      title: "Validation failed",
      status: 422,
      detail: "1 field(s) failed validation",
      errors: [
        {
          pointer: "/interaction_mode",
          code: "enum",
          detail: "Input should be 'managed', 'dispatchable' or 'observed'",
        },
      ],
    });

    const result = await createAgentAction("w-1", "p-1", BODY);

    expect(result.ok === false && result.message).toBe(
      "1 field(s) failed validation: /interaction_mode Input should be 'managed', " +
        "'dispatchable' or 'observed'",
    );
  });

  it("puts a deleted model profile on the select, not on the key box", async () => {
    // A profile deleted between page load and submit is the API's one
    // foreign-key conflict: `CONFLICT_FOREIGN_KEYS` maps
    // `agents_model_profile_id_fkey` to a 409 carrying `field`. So this is
    // the case where a 409's locator has to win over the status rule that
    // otherwise blames the key.
    stubFetch(409, {
      type: "https://docs.agentworld.dev/errors/conflict",
      title: "Conflict",
      status: 409,
      detail:
        "That model profile was deleted while this request was in flight. " +
        "Pick another one, or save the agent without a model.",
      field: "model_profile_id",
    });

    const result = await createAgentAction("w-1", "p-1", {
      ...BODY,
      model_profile_id: "mp-gone",
    });

    expect(result).toMatchObject({ ok: false, field: "model_profile_id" });
    expect(result.ok === false && result.message).toContain("was deleted");
    expect(redirect).not.toHaveBeenCalled();
  });

  it("reports a 500 without claiming the agent exists", async () => {
    // Nothing about this endpoint is expected to 500 any more, but a form
    // that only handled the failures it predicted would show nothing at all
    // for the ones it did not.
    stubFetch(500, {
      type: "https://docs.agentworld.dev/errors/internal_error",
      title: "Internal server error",
      status: 500,
      detail: "Something went wrong on our side.",
    });

    const result = await createAgentAction("w-1", "p-1", {
      ...BODY,
      model_profile_id: "mp-gone",
    });

    expect(result.ok).toBe(false);
    expect(redirect).not.toHaveBeenCalled();
  });
});
