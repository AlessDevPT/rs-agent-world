/**
 * Typed calls for the tenancy endpoints.
 *
 * Hand-written for now and deliberately thin. Phase 5 replaces this file with
 * a client generated from `/openapi.json`, so nothing here should grow logic
 * worth keeping - the shapes mirror `apps/api/src/agentworld/api/schemas/`
 * and the functions do nothing but name a path and a method.
 */

import { apiFetch } from "./client";

export type MemberRole = "viewer" | "member" | "admin" | "owner";
export type ApiKeyScope = "ingest" | "control" | "full";

export interface User {
  readonly id: string;
  readonly email: string;
  readonly display_name: string | null;
  readonly avatar_url: string | null;
}

export interface Workspace {
  readonly id: string;
  readonly slug: string;
  readonly name: string;
  readonly created_by: string;
  readonly created_at: string | null;
}

export interface WorkspaceMembership {
  readonly workspace: Workspace;
  readonly role: MemberRole;
}

export interface Me {
  readonly user: User;
  readonly workspaces: readonly WorkspaceMembership[];
  readonly needs_onboarding: boolean;
}

export interface Project {
  readonly id: string;
  readonly workspace_id: string;
  readonly slug: string;
  readonly name: string;
  readonly description: string | null;
  readonly created_at: string | null;
}

export interface ApiKey {
  readonly id: string;
  readonly workspace_id: string;
  readonly project_id: string | null;
  readonly name: string;
  readonly prefix: string;
  readonly scope: ApiKeyScope;
  readonly agent_key_allowlist: readonly string[] | null;
  readonly rate_limit_rps: number;
  readonly created_at: string | null;
  readonly last_used_at: string | null;
  readonly expires_at: string | null;
  readonly revoked_at: string | null;
}

/**
 * The mint response. `key` is present here and in no other shape in this
 * file, because the server cannot produce it again.
 */
export interface MintedApiKey extends ApiKey {
  readonly key: string;
}

export interface Member {
  readonly user: User;
  readonly role: MemberRole;
  readonly joined_at: string | null;
}

// -------------------------------------------------------------------- calls ---

export function getMe(token: string): Promise<Me> {
  return apiFetch<Me>("/v1/me", { token });
}

export function listWorkspaces(token: string): Promise<WorkspaceMembership[]> {
  return apiFetch<WorkspaceMembership[]>("/v1/workspaces", { token });
}

export function createWorkspace(
  token: string,
  body: { name: string; slug?: string },
): Promise<Workspace> {
  return apiFetch<Workspace>("/v1/workspaces", { token, method: "POST", body });
}

export function listProjects(token: string, workspaceId: string): Promise<Project[]> {
  return apiFetch<Project[]>(`/v1/workspaces/${workspaceId}/projects`, { token });
}

export function createProject(
  token: string,
  workspaceId: string,
  body: { name: string; slug?: string; description?: string },
): Promise<Project> {
  return apiFetch<Project>(`/v1/workspaces/${workspaceId}/projects`, {
    token,
    method: "POST",
    body,
  });
}

export function listMembers(token: string, workspaceId: string): Promise<Member[]> {
  return apiFetch<Member[]>(`/v1/workspaces/${workspaceId}/members`, { token });
}

export function listApiKeys(
  token: string,
  workspaceId: string,
  options: { includeRevoked?: boolean } = {},
): Promise<ApiKey[]> {
  const query = options.includeRevoked ? "?include_revoked=true" : "";
  return apiFetch<ApiKey[]>(`/v1/workspaces/${workspaceId}/api-keys${query}`, { token });
}

export interface CreateApiKeyBody {
  readonly name: string;
  readonly scope: ApiKeyScope;
  readonly project_id?: string | null;
  readonly agent_key_allowlist?: readonly string[] | null;
  readonly rate_limit_rps?: number;
}

export function createApiKey(
  token: string,
  workspaceId: string,
  body: CreateApiKeyBody,
): Promise<MintedApiKey> {
  return apiFetch<MintedApiKey>(`/v1/workspaces/${workspaceId}/api-keys`, {
    token,
    method: "POST",
    body,
  });
}

export function revokeApiKey(token: string, apiKeyId: string): Promise<ApiKey> {
  return apiFetch<ApiKey>(`/v1/api-keys/${apiKeyId}/revoke`, { token, method: "POST" });
}

/** The role needed for each thing the UI offers, mirroring docs/03-API.md 3.2. */
export const ROLE_RANK: Record<MemberRole, number> = {
  viewer: 0,
  member: 1,
  admin: 2,
  owner: 3,
};

export function roleAtLeast(role: MemberRole, required: MemberRole): boolean {
  return ROLE_RANK[role] >= ROLE_RANK[required];
}
