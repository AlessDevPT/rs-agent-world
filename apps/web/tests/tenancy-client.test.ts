import { afterEach, describe, expect, it, vi } from "vitest";

import {
  createApiKey,
  createProject,
  createWorkspace,
  getMe,
  listApiKeys,
  listProjects,
  revokeApiKey,
  roleAtLeast,
  type MemberRole,
} from "@/lib/api/tenancy";
import { readSupabaseConfig } from "@/lib/supabase/config";

interface Call {
  readonly url: string;
  readonly init: RequestInit & { headers: Record<string, string> };
}

function captureFetch(body: unknown = {}): { calls: Call[] } {
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

afterEach(() => {
  vi.unstubAllGlobals();
  delete process.env.NEXT_PUBLIC_SUPABASE_URL;
  delete process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;
});

describe("the tenancy client", () => {
  it("sends the token as a bearer credential on the control plane", async () => {
    const { calls } = captureFetch();
    await getMe("the-token");

    expect(calls).toHaveLength(1);
    expect(calls[0]!.url).toContain("/v1/me");
    expect(calls[0]!.init.headers["authorization"]).toBe("Bearer the-token");
    // Never the ingest header: a user token on the ingest plane is a 401, and
    // sending both at once is refused outright.
    expect(calls[0]!.init.headers["x-agentworld-key"]).toBeUndefined();
  });

  it("never caches a monitoring read", async () => {
    // A stale agent status is worse than no agent status, and Next would
    // otherwise fold a server-component fetch into the build output.
    const { calls } = captureFetch();
    await listProjects("t", "ws-1");
    expect(calls[0]!.init.cache).toBe("no-store");
  });

  it("builds the documented paths and methods", async () => {
    const { calls } = captureFetch();

    await createWorkspace("t", { name: "Acme" });
    await listProjects("t", "ws-1");
    await createProject("t", "ws-1", { name: "P" });
    await listApiKeys("t", "ws-1");
    await listApiKeys("t", "ws-1", { includeRevoked: true });
    await createApiKey("t", "ws-1", { name: "k", scope: "ingest", project_id: "p-1" });
    await revokeApiKey("t", "key-1");

    expect(calls.map((call) => `${call.init.method ?? "GET"} ${path(call.url)}`)).toEqual([
      "POST /v1/workspaces",
      "GET /v1/workspaces/ws-1/projects",
      "POST /v1/workspaces/ws-1/projects",
      "GET /v1/workspaces/ws-1/api-keys",
      "GET /v1/workspaces/ws-1/api-keys?include_revoked=true",
      "POST /v1/workspaces/ws-1/api-keys",
      "POST /v1/api-keys/key-1/revoke",
    ]);
  });

  it("sends a JSON content type only when there is a body", async () => {
    const { calls } = captureFetch();
    await listProjects("t", "ws-1");
    await createProject("t", "ws-1", { name: "P" });

    expect(calls[0]!.init.headers["content-type"]).toBeUndefined();
    expect(calls[1]!.init.headers["content-type"]).toBe("application/json");
    expect(JSON.parse(String(calls[1]!.init.body))).toEqual({ name: "P" });
  });
});

describe("roleAtLeast", () => {
  const order: MemberRole[] = ["viewer", "member", "admin", "owner"];

  it("ranks roles weakest to strongest", () => {
    for (const [index, role] of order.entries()) {
      for (const [otherIndex, other] of order.entries()) {
        expect(roleAtLeast(role, other)).toBe(index >= otherIndex);
      }
    }
  });

  it("mirrors the API's own table, so the UI hides what would be refused", () => {
    // These four are the decisions the Phase 1 UI makes. If the API's rules
    // change, this is the test that should fail first.
    expect(roleAtLeast("viewer", "member")).toBe(false); // cannot create projects
    expect(roleAtLeast("member", "member")).toBe(true);
    expect(roleAtLeast("member", "admin")).toBe(false); // cannot manage keys
    expect(roleAtLeast("admin", "admin")).toBe(true);
  });
});

describe("the Supabase configuration", () => {
  it("is absent in a fresh checkout", () => {
    // Which has to be a renderable state, not a crash: the browser client
    // throws on an empty URL, so every auth page would 500 and a new
    // contributor's first sight of the app would be a stack trace.
    expect(readSupabaseConfig()).toBeNull();
  });

  it("needs both values, not one", () => {
    process.env.NEXT_PUBLIC_SUPABASE_URL = "https://project.supabase.co";
    expect(readSupabaseConfig()).toBeNull();

    process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY = "anon-key";
    expect(readSupabaseConfig()).toEqual({
      url: "https://project.supabase.co",
      anonKey: "anon-key",
    });
  });
});

function path(url: string): string {
  return url.replace(/^https?:\/\/[^/]+/, "");
}
