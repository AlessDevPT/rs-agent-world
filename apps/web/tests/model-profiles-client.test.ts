/**
 * The model-profile client: four paths, four methods, and the bodies verbatim.
 *
 * Written the same way `credentials-client.test.ts` is, and asserting the
 * same kind of thing: that the functions name the API's paths and send what
 * they were handed without editing it. The decisions about *what* to send are
 * `lib/model-profiles/presentation.ts`' and are tested there - a client that
 * second-guessed a body would be the bug.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api/client";
import {
  createModelProfile,
  deleteModelProfile,
  listModelProfiles,
  updateModelProfile,
} from "@/lib/api/model-profiles";

interface Call {
  readonly url: string;
  readonly init: RequestInit & { headers: Record<string, string> };
}

function captureFetch(status = 200, body: unknown = {}): { calls: Call[] } {
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

function path(call: Call): string {
  return call.url.replace(/^https?:\/\/[^/]+/, "");
}

function sent(call: Call): unknown {
  return typeof call.init.body === "string" ? JSON.parse(call.init.body) : undefined;
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("the model profile client", () => {
  it("lists a workspace's profiles under the workspace", async () => {
    const { calls } = captureFetch(200, []);
    await listModelProfiles("t", "w-1");

    expect(path(calls[0]!)).toBe("/v1/workspaces/w-1/model-profiles");
    expect(calls[0]!.init.method).toBeUndefined();
    expect(calls[0]!.init.headers["authorization"]).toBe("Bearer t");
  });

  it("posts a create body unchanged", async () => {
    const { calls } = captureFetch(201, {});
    await createModelProfile("t", "w-1", {
      name: "Writer",
      provider_slug: "anthropic",
      model_id: "claude-opus-5",
      parameters: { max_output_tokens: 2048 },
      credential_id: null,
      is_default: true,
    });

    expect(path(calls[0]!)).toBe("/v1/workspaces/w-1/model-profiles");
    expect(calls[0]!.init.method).toBe("POST");
    expect(sent(calls[0]!)).toEqual({
      name: "Writer",
      provider_slug: "anthropic",
      model_id: "claude-opus-5",
      parameters: { max_output_tokens: 2048 },
      credential_id: null,
      is_default: true,
    });
  });

  it("patches the item path with only the fields it was given", async () => {
    const { calls } = captureFetch(200, {});
    await updateModelProfile("t", "w-1", "p-1", { name: "Renamed" });

    expect(path(calls[0]!)).toBe("/v1/workspaces/w-1/model-profiles/p-1");
    expect(calls[0]!.init.method).toBe("PATCH");
    expect(sent(calls[0]!)).toEqual({ name: "Renamed" });
  });

  it("sends an explicit null through JSON rather than dropping it", async () => {
    // `JSON.stringify` drops `undefined` and keeps `null`, which is exactly
    // the API's absent-versus-null distinction - so the one thing worth
    // asserting about this call is that a cleared credential survives it.
    const { calls } = captureFetch(200, {});
    await updateModelProfile("t", "w-1", "p-1", { credential_id: null });

    expect(sent(calls[0]!)).toEqual({ credential_id: null });
  });

  it("deletes the item path and parses nothing", async () => {
    const { calls } = captureFetch(204, undefined);
    await expect(deleteModelProfile("t", "w-1", "p-1")).resolves.toBeUndefined();

    expect(path(calls[0]!)).toBe("/v1/workspaces/w-1/model-profiles/p-1");
    expect(calls[0]!.init.method).toBe("DELETE");
  });

  it("turns a 422 into an ApiError carrying the field the API blamed", async () => {
    // The locator is what lets a form put the message against the input that
    // caused it; `field` and `index` are the only context keys the API sends.
    captureFetch(422, {
      type: "https://docs.agentworld.dev/errors/validation_failed",
      title: "Validation failed",
      status: 422,
      detail: "This server cannot serve that model provider. It serves: anthropic",
      instance: "req_1",
      field: "provider_slug",
    });

    await expect(
      createModelProfile("t", "w-1", {
        name: "Typo",
        provider_slug: "nope",
        model_id: "m",
      }),
    ).rejects.toMatchObject({
      name: "ApiError",
      problem: { field: "provider_slug", status: 422 },
    });
  });

  it("turns a 409 on delete into an ApiError, not a resolved promise", async () => {
    captureFetch(409, {
      type: "https://docs.agentworld.dev/errors/conflict",
      title: "Conflict",
      status: 409,
      detail: "This model profile is still used by at least one agent",
    });

    const caught = await deleteModelProfile("t", "w-1", "p-1").catch((error: unknown) => error);

    expect(caught).toBeInstanceOf(ApiError);
    expect((caught as ApiError).problem.status).toBe(409);
  });
});
