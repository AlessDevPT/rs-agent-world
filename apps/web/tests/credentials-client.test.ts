/**
 * The credential client, and the one property worth stating about it.
 *
 * **Material travels one way.** There is no `getCredential` here to test,
 * because the API has no route that returns one and no permission at any role
 * that would - so the tests below assert the paths and bodies of the five
 * calls that exist, and the last one asserts the absence: nothing this module
 * exports reads a secret back.
 */

import { afterEach, describe, expect, it, vi } from "vitest";

import * as credentials from "@/lib/api/credentials";
import {
  createCredential,
  deleteCredential,
  listCredentials,
  revokeCredential,
  rotateCredential,
} from "@/lib/api/credentials";

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

describe("the credential client", () => {
  it("lists a workspace's credentials under the workspace", async () => {
    const { calls } = captureFetch(200, []);
    await listCredentials("t", "w-1");

    expect(path(calls[0]!)).toBe("/v1/workspaces/w-1/credentials");
    expect(calls[0]!.init.headers["authorization"]).toBe("Bearer t");
  });

  it("asks for revoked rows only when told to", async () => {
    // The API defaults to excluding them, so spelling out `false` would put
    // noise in the request log for no change in the answer.
    const { calls } = captureFetch(200, []);
    await listCredentials("t", "w-1");
    await listCredentials("t", "w-1", { includeRevoked: false });
    await listCredentials("t", "w-1", { includeRevoked: true });

    expect(calls.map(path)).toEqual([
      "/v1/workspaces/w-1/credentials",
      "/v1/workspaces/w-1/credentials",
      "/v1/workspaces/w-1/credentials?include_revoked=true",
    ]);
  });

  it("posts material to the workspace's collection, and nothing else", async () => {
    const { calls } = captureFetch(201);
    await createCredential("t", "w-1", {
      name: "Anthropic, production",
      kind: "api_key",
      provider_slug: "anthropic",
      secret: "sk-not-a-real-key",
      project_id: null,
      scopes: null,
      expires_at: null,
    });

    expect(calls[0]!.init.method).toBe("POST");
    expect(path(calls[0]!)).toBe("/v1/workspaces/w-1/credentials");
    expect(sent(calls[0]!)).toEqual({
      name: "Anthropic, production",
      kind: "api_key",
      provider_slug: "anthropic",
      secret: "sk-not-a-real-key",
      project_id: null,
      scopes: null,
      expires_at: null,
    });
    // `CredentialCreate` is `extra="forbid"`, so a field this client invented
    // would be a 422 rather than a silently ignored key.
    expect(Object.keys(sent(calls[0]!) as object)).toHaveLength(7);
  });

  it("rotates and revokes through the item routes, nested under the workspace", async () => {
    // Nested rather than id-addressed: resolving a credential id to a
    // workspace before authorizing the caller would be a cross-tenant query.
    const { calls } = captureFetch(200);
    await rotateCredential("t", "w-1", "c-1", { secret: "sk-replacement", expires_at: null });
    await revokeCredential("t", "w-1", "c-1");

    expect(path(calls[0]!)).toBe("/v1/workspaces/w-1/credentials/c-1/rotate");
    expect(calls[0]!.init.method).toBe("POST");
    expect(sent(calls[0]!)).toEqual({ secret: "sk-replacement", expires_at: null });

    expect(path(calls[1]!)).toBe("/v1/workspaces/w-1/credentials/c-1/revoke");
    expect(calls[1]!.init.method).toBe("POST");
    expect(calls[1]!.init.body).toBeUndefined();
  });

  it("destroys material with a DELETE that has no body to parse", async () => {
    const { calls } = captureFetch(204);

    await expect(deleteCredential("t", "w-1", "c-1")).resolves.toBeUndefined();
    expect(calls[0]!.init.method).toBe("DELETE");
    expect(path(calls[0]!)).toBe("/v1/workspaces/w-1/credentials/c-1");
  });

  it("exports no way to read material back", () => {
    // The shape of the module is part of the guarantee: a function that read
    // a secret would have nothing to call, and its existence here would
    // invite someone to add the route.
    const exported = Object.keys(credentials);
    expect(exported.sort()).toEqual([
      "createCredential",
      "deleteCredential",
      "listCredentials",
      "revokeCredential",
      "rotateCredential",
    ]);
  });
});
