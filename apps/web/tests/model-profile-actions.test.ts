/**
 * The server actions behind the model-profile forms.
 *
 * Mocked down to the two things an action cannot do in a test - resolve a
 * session and revalidate a route - with the real API client underneath and
 * `fetch` stubbed, so what these assert is the action's own behaviour: what it
 * returns on a refusal, what it carries out of a problem document, and when it
 * does *not* tell Next to re-render.
 *
 * **Why the cache call matters.** `revalidatePath` after a failed write would
 * discard the page's data for no reason and, worse, imply the write landed.
 * Every test below that expects a failure also expects silence from it.
 */

import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("next/cache", () => ({ revalidatePath: vi.fn() }));
vi.mock("next/navigation", () => ({ redirect: vi.fn() }));
vi.mock("@/lib/session", () => ({
  requireSession: vi.fn(async () => ({ token: "token-1", me: { workspaces: [] } })),
}));

import { revalidatePath } from "next/cache";

import {
  createModelProfileAction,
  deleteModelProfileAction,
  updateModelProfileAction,
} from "@/app/actions";

const PROFILES_PATH = "/w/w-1/settings/model-profiles";

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

const PROFILE = {
  id: "p-1",
  name: "Writer",
  provider_slug: "anthropic",
  model_id: "claude-opus-5",
  parameters: {},
  credential_id: null,
  is_default: false,
};

function refusal(status: number, detail: string, extra: Record<string, unknown> = {}) {
  return {
    type: "https://docs.agentworld.dev/errors/validation_failed",
    title: "Validation failed",
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

describe("creating a profile", () => {
  it("returns the stored profile and refreshes the page", async () => {
    stubFetch(201, PROFILE);

    const result = await createModelProfileAction("w-1", {
      name: "Writer",
      provider_slug: "anthropic",
      model_id: "claude-opus-5",
    });

    expect(result).toEqual({ ok: true, profile: PROFILE });
    expect(revalidatePath).toHaveBeenCalledWith(PROFILES_PATH);
  });

  it("carries the API's field and request id out of a 422, and revalidates nothing", async () => {
    stubFetch(
      422,
      refusal(422, "This server cannot serve that model provider. It serves: anthropic", {
        field: "provider_slug",
      }),
    );

    const result = await createModelProfileAction("w-1", {
      name: "Typo",
      provider_slug: "nope",
      model_id: "m",
    });

    expect(result).toMatchObject({
      ok: false,
      field: "provider_slug",
      requestId: "req_abc",
    });
    expect(result.ok === false && result.message).toContain("It serves: anthropic");
    expect(revalidatePath).not.toHaveBeenCalled();
  });

  it("says which fields a schema refusal was about", async () => {
    // FastAPI's own handler puts the information in `errors` and leaves
    // `detail` as a count, which on its own tells a person nothing.
    stubFetch(422, {
      type: "https://docs.agentworld.dev/errors/validation_failed",
      title: "Validation failed",
      status: 422,
      detail: "1 field(s) failed validation",
      errors: [
        { pointer: "/name", code: "string_too_long", detail: "String should have at most 200 characters" },
      ],
    });

    const result = await createModelProfileAction("w-1", {
      name: "x".repeat(201),
      provider_slug: "anthropic",
      model_id: "m",
    });

    expect(result.ok === false && result.message).toBe(
      "1 field(s) failed validation: /name String should have at most 200 characters",
    );
  });
});

describe("changing a profile", () => {
  it("sends a body with only what moved and refreshes the page", async () => {
    const { calls } = stubFetch(200, { ...PROFILE, name: "Renamed" });

    const result = await updateModelProfileAction("w-1", "p-1", { name: "Renamed" });

    expect(result.ok).toBe(true);
    expect(calls[0]!.init.method).toBe("PATCH");
    expect(JSON.parse(String(calls[0]!.init.body))).toEqual({ name: "Renamed" });
    expect(revalidatePath).toHaveBeenCalledWith(PROFILES_PATH);
  });

  it("refuses an empty body without asking the API", async () => {
    // `ModelProfileService.update` accepts one and audits it, so a PATCH that
    // writes no column would leave a record of an edit that never happened.
    const { calls } = stubFetch(200, PROFILE);

    const result = await updateModelProfileAction("w-1", "p-1", {});

    expect(result).toEqual({ ok: false, message: "Nothing was changed" });
    expect(calls).toHaveLength(0);
    expect(revalidatePath).not.toHaveBeenCalled();
  });

  it("passes an explicit null through to the API", async () => {
    const { calls } = stubFetch(200, PROFILE);

    await updateModelProfileAction("w-1", "p-1", { credential_id: null });

    expect(JSON.parse(String(calls[0]!.init.body))).toEqual({ credential_id: null });
  });

  it("reports a pairing refusal against the credential field", async () => {
    stubFetch(
      422,
      refusal(422, "That credential is for 'aurora' and this profile names 'anthropic'.", {
        field: "credential_id",
      }),
    );

    const result = await updateModelProfileAction("w-1", "p-1", { provider_slug: "anthropic" });

    expect(result).toMatchObject({ ok: false, field: "credential_id" });
    expect(revalidatePath).not.toHaveBeenCalled();
  });
});

describe("deleting a profile", () => {
  it("refreshes the page once the API has accepted it", async () => {
    stubFetch(204, undefined);

    expect(await deleteModelProfileAction("w-1", "p-1")).toEqual({ ok: true });
    expect(revalidatePath).toHaveBeenCalledWith(PROFILES_PATH);
  });

  it("returns the 409 rather than reporting a deletion that did not happen", async () => {
    stubFetch(409, {
      type: "https://docs.agentworld.dev/errors/conflict",
      title: "Conflict",
      status: 409,
      detail: "This model profile is still used by at least one agent",
    });

    const result = await deleteModelProfileAction("w-1", "p-1");

    expect(result).toMatchObject({
      ok: false,
      message: "This model profile is still used by at least one agent",
    });
    expect(revalidatePath).not.toHaveBeenCalled();
  });

  it("reports a role refusal as the API wrote it", async () => {
    // The page hides the buttons from a non-admin; this is the boundary that
    // actually holds, and it is the API's.
    stubFetch(403, {
      type: "https://docs.agentworld.dev/errors/forbidden",
      title: "Forbidden",
      status: 403,
      detail: "This action needs the admin role",
    });

    const result = await deleteModelProfileAction("w-1", "p-1");

    expect(result).toMatchObject({ ok: false, message: "This action needs the admin role" });
    expect(revalidatePath).not.toHaveBeenCalled();
  });
});
