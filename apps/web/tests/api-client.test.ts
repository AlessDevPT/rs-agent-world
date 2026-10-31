import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, apiFetch } from "@/lib/api/client";

function mockFetch(status: number, body: unknown, ok = status < 400): void {
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => ({
      ok,
      status,
      statusText: "",
      json: async () => body,
    })),
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("apiFetch", () => {
  it("returns the parsed body on success", async () => {
    mockFetch(200, { protocol_version: "1.0" });
    await expect(apiFetch<{ protocol_version: string }>("/v1/meta")).resolves.toEqual({
      protocol_version: "1.0",
    });
  });

  it("turns a problem document into a typed error carrying the request id", async () => {
    mockFetch(404, {
      type: "https://docs.agentworld.dev/errors/not_found",
      title: "Not found",
      status: 404,
      detail: "no such project",
      instance: "req_abc123",
    });

    const error = await apiFetch("/v1/projects/nope").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    const apiError = error as ApiError;
    expect(apiError.problem.status).toBe(404);
    expect(apiError.requestId).toBe("req_abc123");
    expect(apiError.isRetryable).toBe(false);
  });

  it("marks 429 and 5xx as retryable", async () => {
    for (const status of [429, 500, 503]) {
      mockFetch(status, { title: "x", status, detail: "y" });
      const error = (await apiFetch("/x").catch((e: unknown) => e)) as ApiError;
      expect(error.isRetryable, `status ${status}`).toBe(true);
    }
  });

  it("survives a non-JSON error body from a proxy", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => ({
        ok: false,
        status: 502,
        statusText: "Bad Gateway",
        json: async () => {
          throw new Error("not json");
        },
      })),
    );

    const error = (await apiFetch("/x").catch((e: unknown) => e)) as ApiError;
    expect(error).toBeInstanceOf(ApiError);
    expect(error.problem.status).toBe(502);
    expect(error.isRetryable).toBe(true);
  });
});
