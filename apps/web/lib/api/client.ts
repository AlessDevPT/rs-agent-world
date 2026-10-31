/**
 * Minimal API client.
 *
 * Phase 5 replaces the hand-written types here with a client generated from
 * the API's OpenAPI document (`npm run codegen`). What stays is this file's
 * job: attach auth, parse RFC 9457 problem documents into a typed error, and
 * surface the request id so a user-visible failure can be traced in the logs.
 */

import { publicEnv } from "@/lib/env";

export interface Problem {
  readonly type: string;
  readonly title: string;
  readonly status: number;
  readonly detail: string;
  readonly instance?: string;
  readonly errors?: ReadonlyArray<{
    pointer: string;
    code: string;
    detail?: string;
  }>;
}

export class ApiError extends Error {
  readonly problem: Problem;

  constructor(problem: Problem) {
    super(`${problem.title}: ${problem.detail}`);
    this.name = "ApiError";
    this.problem = problem;
  }

  /** The id to quote when reporting a failure. */
  get requestId(): string | undefined {
    return this.problem.instance;
  }

  get isRetryable(): boolean {
    return this.problem.status === 429 || this.problem.status >= 500;
  }
}

function isProblem(value: unknown): value is Problem {
  if (typeof value !== "object" || value === null) return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v["title"] === "string" &&
    typeof v["status"] === "number" &&
    typeof v["detail"] === "string"
  );
}

export interface RequestOptions extends Omit<RequestInit, "body"> {
  readonly token?: string;
  readonly body?: unknown;
}

export async function apiFetch<T>(
  path: string,
  options: RequestOptions = {},
): Promise<T> {
  const { token, body, headers, ...rest } = options;

  const response = await fetch(`${publicEnv.apiUrl}${path}`, {
    // Monitoring data is never cacheable: a stale agent status is worse than
    // no agent status. Next.js would otherwise fold a server-component fetch
    // into the build output and serve a snapshot forever.
    cache: "no-store",
    ...rest,
    headers: {
      ...(body === undefined ? {} : { "content-type": "application/json" }),
      ...(token === undefined ? {} : { authorization: `Bearer ${token}` }),
      ...headers,
    },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });

  if (!response.ok) {
    let parsed: unknown = undefined;
    try {
      parsed = await response.json();
    } catch {
      // A proxy or gateway can fail before reaching the API, in which case the
      // body is HTML rather than a problem document.
    }
    throw new ApiError(
      isProblem(parsed)
        ? parsed
        : {
            type: "about:blank",
            title: "Request failed",
            status: response.status,
            detail: response.statusText || "The API returned a non-JSON error.",
          },
    );
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

export interface Meta {
  readonly api_version: string;
  readonly protocol_version: string;
  readonly adapters: readonly string[];
  readonly server_time: string;
}

export function getMeta(): Promise<Meta> {
  return apiFetch<Meta>("/v1/meta");
}
