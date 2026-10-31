# AgentWorld — API Specification

> Revision 1 (2026-10-31). Base path `/v1`. All bodies JSON, UTF-8. All timestamps
> RFC 3339 with explicit offset. FastAPI generates the machine-readable OpenAPI document
> at `/openapi.json`; this file is the human contract and the spec the implementation is
> written against.

## 1. Planes

| Plane | Prefix | Credential | Audience |
|-------|--------|-----------|----------|
| Control | `/v1/workspaces`, `/v1/projects`, `/v1/agents`, `/v1/tasks`, `/v1/api-keys`, `/v1/events`, `/v1/logs`, `/v1/world`, `/v1/integrations`, `/v1/webhooks` | `Authorization: Bearer <supabase-jwt>` | The web app, a human operator, CI |
| Ingest | `/v1/ingest/*`, `/v1/self/*` | `X-AgentWorld-Key: aw_live_...` | Monitored agents |
| Adapter inbound | `/v1/hooks/{integration_id}` | Adapter-defined (usually HMAC header) | Third-party systems |
| Realtime | `/v1/ws/projects/{project_id}` | Short-lived WS ticket | The web app |

A control-plane token is rejected on `/v1/ingest/*` and vice versa — not downgraded,
rejected with `401`. Mixing the planes is the single most likely source of a privilege
bug, so the dependency that resolves a principal can only ever produce one of the two
types.

## 2. Conventions

**Errors** — RFC 9457 problem details, always this shape:

```json
{
  "type": "https://docs.agentworld.dev/errors/validation_failed",
  "title": "Validation failed",
  "status": 422,
  "detail": "events[2].occurred_at is in the future by more than 300s",
  "instance": "req_01JBQ8T4K9",
  "errors": [{ "pointer": "/events/2/occurred_at", "code": "future_timestamp" }]
}
```

| Status | Meaning in AgentWorld |
|--------|----------------------|
| 400 | Malformed request |
| 401 | Missing/invalid credential, or wrong plane |
| 403 | Authenticated but the role forbids the action |
| 404 | Not found **or** not visible to this tenant (deliberately indistinguishable) |
| 409 | Conflict (slug taken, task already terminal, optimistic-concurrency mismatch) |
| 422 | Semantic validation failure |
| 429 | Rate limited; `Retry-After` and `X-RateLimit-*` headers set |
| 503 | Dependency unavailable; safe to retry with backoff |

**Pagination** — cursor-based everywhere. `?limit=50&cursor=<opaque>`:

```json
{ "data": [], "page": { "next_cursor": "ev_01JBQ...", "has_more": true } }
```

Offset pagination is not offered: the event stream moves while you page it.

**Idempotency** — mutating control-plane calls accept `Idempotency-Key`. Ingest uses
`dedup_key` inside each event instead, so a batch retry is exact per event rather than
per request.

**Versioning** — the path carries the major version. Additive changes (new fields, new
event types, new enum members) ship without a version bump, and clients must tolerate
them; that tolerance is part of the protocol contract, stated in section 5.

**Request tracing** — every response carries `X-Request-Id`, echoed into logs, the
`audit_log` row and the problem-details `instance`.

## 3. Control plane

### 3.1 Identity

| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/v1/me` | Current user, workspace memberships and roles. Upserts the `users` mirror row on first call. |
| `PATCH` | `/v1/me` | `display_name`, `avatar_url`. |

Registration and login happen in Supabase Auth, client-side. AgentWorld never sees a
password. The first authenticated `GET /v1/me` is what materialises the user and, if they
have no workspace, returns `"needs_onboarding": true`.

### 3.2 Workspaces

| Method | Path | Role | Notes |
|--------|------|------|-------|
| `GET` | `/v1/workspaces` | any member | |
| `POST` | `/v1/workspaces` | authenticated | Creator becomes `owner`. |
| `GET` | `/v1/workspaces/{id}` | viewer+ | |
| `PATCH` | `/v1/workspaces/{id}` | admin+ | |
| `DELETE` | `/v1/workspaces/{id}` | owner | Soft delete; a 7-day grace period before purge. |
| `GET` | `/v1/workspaces/{id}/members` | viewer+ | |
| `POST` | `/v1/workspaces/{id}/members` | admin+ | Invite by email. |
| `PATCH` | `/v1/workspaces/{id}/members/{user_id}` | admin+ | Change role. Owner cannot be demoted by an admin. |
| `DELETE` | `/v1/workspaces/{id}/members/{user_id}` | admin+, or self | |

Role capabilities:

| Action | viewer | member | admin | owner |
|--------|--------|--------|-------|-------|
| Read everything in the workspace | yes | yes | yes | yes |
| Create/update projects, agents, tasks | — | yes | yes | yes |
| Cancel tasks, silence agents | — | yes | yes | yes |
| Manage API keys, integrations, webhooks | — | — | yes | yes |
| Manage members | — | — | yes | yes |
| Delete workspace, transfer ownership | — | — | — | yes |

### 3.3 Projects

| Method | Path | Role |
|--------|------|------|
| `GET` | `/v1/workspaces/{ws}/projects` | viewer+ |
| `POST` | `/v1/workspaces/{ws}/projects` | member+ |
| `GET` | `/v1/projects/{id}` | viewer+ |
| `PATCH` | `/v1/projects/{id}` | member+ |
| `DELETE` | `/v1/projects/{id}` | admin+ |
| `GET` | `/v1/projects/{id}/overview` | viewer+ |

`GET /v1/projects/{id}/overview` is the dashboard's single call:

```json
{
  "project": { "id": "...", "name": "Support Automation", "slug": "support" },
  "agent_counts": { "working": 3, "idle": 2, "blocked": 1, "error": 0, "offline": 1 },
  "task_counts": { "pending": 12, "running": 3, "completed_24h": 184, "failed_24h": 6 },
  "throughput_24h": [{ "hour": "2026-10-31T08:00:00Z", "completed": 9, "failed": 1 }],
  "recent_events": [],
  "cursor": 448912
}
```

### 3.4 Agents

| Method | Path | Role | Notes |
|--------|------|------|-------|
| `GET` | `/v1/projects/{id}/agents` | viewer+ | Filters: `status`, `kind`, `label.<k>=<v>`, `q`. |
| `POST` | `/v1/projects/{id}/agents` | member+ | Pre-register an agent before it first connects. Idempotent on `agent_key`. |
| `GET` | `/v1/agents/{id}` | viewer+ | |
| `PATCH` | `/v1/agents/{id}` | member+ | `display_name`, `kind`, `labels`, `heartbeat_ttl_seconds`. **Not** `status`. |
| `POST` | `/v1/agents/{id}/maintenance` | member+ | `{"enabled": true, "reason": "deploy"}` — mutes alerts and the island's error glow. |
| `DELETE` | `/v1/agents/{id}` | admin+ | Archive. History is retained. |
| `GET` | `/v1/agents/{id}/status-history` | viewer+ | |
| `GET` | `/v1/agents/{id}/runs` | viewer+ | |
| `POST` | `/v1/agents/{id}/commands` | member+ | See below. |

Agent representation:

```json
{
  "id": "a7f1...",
  "project_id": "p01...",
  "agent_key": "etl-worker-eu-1",
  "display_name": "ETL Worker (EU)",
  "kind": "etl",
  "status": "working",
  "status_reason": null,
  "runtime": { "language": "python", "framework": "langgraph", "provider": "anthropic" },
  "capabilities": ["task.claim", "command.cancel", "command.pause"],
  "labels": { "region": "eu-west-1" },
  "current_task": { "id": "t9...", "title": "Reconcile invoices", "progress_percent": 45 },
  "last_heartbeat_at": "2026-10-31T09:14:02Z",
  "heartbeat_ttl_seconds": 60,
  "registered_at": "2026-09-02T11:00:00Z"
}
```

**Commands** (`POST /v1/agents/{id}/commands`) are how management works without a
persistent connection into the customer's network. A command is recorded, surfaced to the
agent on its next `GET /v1/self/commands` poll (or over its SDK's long-poll), and
acknowledged with an event:

```json
{ "type": "pause", "reason": "cost control", "params": {} }
```

`type` is one of `pause`, `resume`, `cancel_task`, `reload_config`, `drain`, or an
adapter-declared custom type. An agent advertises what it honours in `capabilities`; the
API rejects a command the agent has not advertised with `422` rather than letting the
operator believe something happened. This is the honest version of "management": the
platform cannot force a third-party process to obey, so it never pretends the command
succeeded until the agent says so.

### 3.5 Tasks

| Method | Path | Role | Notes |
|--------|------|------|-------|
| `GET` | `/v1/projects/{id}/tasks` | viewer+ | Filters: `status`, `assigned_agent_id`, `priority`, `label.*`, `created_after`. |
| `POST` | `/v1/projects/{id}/tasks` | member+ | Create work for agents to claim. |
| `GET` | `/v1/tasks/{id}` | viewer+ | Includes runs and the task's event timeline. |
| `PATCH` | `/v1/tasks/{id}` | member+ | `title`, `description`, `priority`, `deadline_at`, `labels`, `assigned_agent_id`. Status is never set here. |
| `POST` | `/v1/tasks/{id}/cancel` | member+ | Sets `cancelled`, queues a `cancel_task` command for the assignee. |
| `POST` | `/v1/tasks/{id}/retry` | member+ | Clones into a new `pending` task linked via `labels.retry_of`. |
| `GET` | `/v1/tasks/{id}/events` | viewer+ | |

Create:

```json
{
  "task_ref": "ticket-8812",
  "title": "Reconcile October invoices",
  "description": "Pull from S3, match against ledger, flag deltas over 1%.",
  "priority": "high",
  "assigned_agent_id": null,
  "input": { "bucket": "finance-exports", "month": "2026-10" },
  "max_attempts": 3,
  "deadline_at": "2026-11-01T00:00:00Z",
  "labels": { "team": "finance" }
}
```

Task status is only ever changed by a reducer (from an ingested event) or by an explicit
operator action (`cancel`). There is no third path, which is why a task's history always
explains its current state.

### 3.6 Events, logs, metrics (read)

| Method | Path | Notes |
|--------|------|-------|
| `GET` | `/v1/projects/{id}/events` | `type`, `severity_min`, `agent_id`, `task_id`, `since`, `until`, `cursor`. Descending by default; `order=asc` for replay. |
| `GET` | `/v1/projects/{id}/logs` | Adds `q` full-text search. |
| `GET` | `/v1/projects/{id}/metrics` | `name`, `agent_id`, `bucket=hour|day`, `since`, `until`. Served from the rollup. |
| `GET` | `/v1/projects/{id}/events/export` | NDJSON stream, `since`/`until`, admin+. |

### 3.7 API keys

| Method | Path | Role |
|--------|------|------|
| `GET` | `/v1/workspaces/{ws}/api-keys` | admin+ |
| `POST` | `/v1/workspaces/{ws}/api-keys` | admin+ |
| `POST` | `/v1/api-keys/{id}/revoke` | admin+ |
| `DELETE` | `/v1/api-keys/{id}` | admin+ |

```jsonc
// POST request
{ "name": "EU workers", "project_id": "p01...", "scope": "ingest",
  "agent_key_allowlist": ["etl-worker-eu-*"], "rate_limit_rps": 100,
  "expires_at": "2027-10-31T00:00:00Z" }

// 201 response — `key` appears exactly once, in this response, ever
{ "id": "k_7f...", "prefix": "aw_live_8f2c1a",
  "key": "aw_live_8f2c1a.s3cr3t-half-shown-once",
  "name": "EU workers", "scope": "ingest", "created_at": "..." }
```

### 3.8 Integrations and webhooks

| Method | Path | Role | Notes |
|--------|------|------|-------|
| `GET` | `/v1/adapters` | viewer+ | Registry listing: slug, name, capabilities, JSON Schema for config. Drives the UI form. |
| `GET` | `/v1/projects/{id}/integrations` | admin+ | |
| `POST` | `/v1/projects/{id}/integrations` | admin+ | Config validated against the adapter's schema. |
| `PATCH` | `/v1/integrations/{id}` | admin+ | |
| `DELETE` | `/v1/integrations/{id}` | admin+ | |
| `POST` | `/v1/integrations/{id}/test` | admin+ | Replays a sample payload through `normalise` and returns the canonical events **without** persisting them. |
| `GET/POST/DELETE` | `/v1/projects/{id}/webhooks` | admin+ | Outbound subscriptions. |
| `GET` | `/v1/webhooks/{id}/deliveries` | admin+ | |

Outbound webhook delivery carries `X-AgentWorld-Signature: t=<unix>,v1=<hex>` where `v1`
is `HMAC-SHA256(secret, "<t>.<raw-body>")`. Retries: 8 attempts, exponential backoff from
10s to 6h, then `dead`. Receivers must treat deliveries as at-least-once and dedupe on
`event.id`.

### 3.9 World (presentation)

Separate namespace, admin of looks only. Nothing here affects agent behaviour.

| Method | Path | Role |
|--------|------|------|
| `GET` | `/v1/projects/{id}/world` | viewer+ |
| `PUT` | `/v1/projects/{id}/world/layout` | member+ |
| `PUT` | `/v1/projects/{id}/world/placements/{agent_id}` | member+ |
| `POST` | `/v1/projects/{id}/world/autolayout` | member+ |

```json
{
  "layout": { "theme": "island-temperate", "grid": { "width": 16, "height": 16 },
              "tiles": [{ "q": 3, "r": 5, "terrain": "grass", "decor": "tree" }],
              "landmarks": [{ "kind": "warehouse", "q": 8, "r": 8 }] },
  "placements": [
    { "agent_id": "a7f1...", "building_kind": "workshop", "character_kind": "artisan",
      "tile": { "q": 4, "r": 6 }, "facing": "south-east", "auto_assigned": false }
  ]
}
```

## 4. Ingest plane

Authenticated with `X-AgentWorld-Key`. The key pins the project, so no path or body
parameter names it — a key cannot be pointed at another tenant even by a buggy client.

### 4.1 `POST /v1/ingest/events`

The protocol's single most important endpoint.

```json
{
  "events": [
    { "dedup_key": "run-3f9a:started", "type": "run.started",
      "occurred_at": "2026-10-31T09:13:40.102Z", "agent_key": "etl-worker-eu-1",
      "task_ref": "ticket-8812", "run_ref": "run-3f9a", "payload": {} },
    { "dedup_key": "run-3f9a:step-12", "type": "task.progress",
      "occurred_at": "2026-10-31T09:14:02.511Z", "agent_key": "etl-worker-eu-1",
      "task_ref": "ticket-8812", "run_ref": "run-3f9a", "severity": "info",
      "labels": { "region": "eu-west-1" },
      "payload": { "percent": 45, "message": "parsed 9/20 files" } }
  ]
}
```

Response `202`:

```json
{
  "accepted": 2, "duplicates": 0, "rejected": 0,
  "results": [
    { "dedup_key": "run-3f9a:started", "status": "accepted", "event_id": "e1...", "seq": 448911 },
    { "dedup_key": "run-3f9a:step-12", "status": "accepted", "event_id": "e2...", "seq": 448912 }
  ]
}
```

Semantics:

- Batches up to 500 events or 1 MB. Larger gets `413`.
- **Partial success is normal.** One bad event does not fail the batch; it comes back
  `{"status": "rejected", "error": {...}}` while the rest are accepted. An agent must not
  lose 499 good events to one malformed one.
- `dedup_key` already seen in this project returns `{"status": "duplicate"}` with the
  original `event_id` and `seq`. Safe, cheap retries.
- Unknown `type` is accepted and stored; it folds into nothing (section 5).
- Referenced `agent_key` / `task_ref` / `run_ref` are **upserted** — an agent that has
  never called register still appears, with `status = idle` and an audit note that it
  self-registered. Requiring registration first would make the first-run experience a
  two-step dance for no safety gain, since the API key is already the authorisation.
- `occurred_at` more than 300s in the future is rejected (`future_timestamp`); in the past
  it is accepted at any age, because backfill is a legitimate use.
- At-most-once ordering is not assumed. Reducers are written to tolerate out-of-order
  arrival by comparing `occurred_at` against the state row's last-applied timestamp and
  ignoring stale transitions.

### 4.2 Other ingest endpoints

| Method | Path | Notes |
|--------|------|-------|
| `POST` | `/v1/ingest/heartbeat` | `{"agent_key": "...", "status": "idle", "runtime": {...}}`. Cheap, no event row written unless the status changed. |
| `POST` | `/v1/ingest/logs` | Batch log lines. Below the project's severity floor they are dropped and counted, not stored. |
| `POST` | `/v1/ingest/metrics` | Batch metric samples. |
| `GET` | `/v1/self` | The calling agent's own record. Agents resolve their UUID without a control-plane token. |
| `GET` | `/v1/self/commands` | Pending commands. `?wait=25` long-polls up to 25s. |
| `POST` | `/v1/self/commands/{id}/ack` | `{"status": "accepted"\|"rejected"\|"done", "detail": "..."}`. |
| `POST` | `/v1/self/tasks/claim` | `{"max": 1, "labels": {...}}` — atomic `SELECT ... FOR UPDATE SKIP LOCKED` claim of pending tasks. Returns the claimed tasks and marks them `running`. |

`POST /v1/self/tasks/claim` is the one place an agent changes state without an event —
and it still writes a `task.assigned` event internally, so the audit trail stays complete.

### 4.3 Adapter inbound

`POST /v1/hooks/{integration_id}` — raw body passed to the adapter's `verify` then
`normalise`, and the resulting canonical events go through the identical ingest pipeline.
No adapter has a privileged path into the database; an adapter bug can produce wrong
events but never wrong tenancy.

## 5. Protocol compatibility rules

These are promises, not guidelines, and the compatibility test suite asserts each one:

1. A client may send **unknown event types**; the server stores and broadcasts them.
2. A client may send **unknown payload fields**; they are preserved verbatim.
3. A client may **omit any optional field**; defaults never change meaning across minor
   versions.
4. The server may **add fields** to any response; clients must ignore unknown fields.
5. The server may **add enum members** (a new `agent_status`, a new `task_status`);
   clients must render an unknown member as its raw string rather than crashing.
6. Nothing in the protocol will ever require a UI concept. A headless integration
   that never opens the web app must stay fully functional — and a conformance test runs
   the whole agent lifecycle with the frontend absent.

## 6. Realtime (WebSocket)

`GET /v1/ws/projects/{project_id}?ticket=<ticket>`

Tickets, not JWTs, because browsers cannot set headers on a WebSocket and a token in a
query string ends up in access logs. The client calls
`POST /v1/projects/{id}/ws-ticket` with its JWT, gets a 60-second single-use ticket, and
connects with it.

### Client to server

```jsonc
{ "type": "subscribe",   "channels": ["agents", "tasks", "events", "world"] }
{ "type": "unsubscribe", "channels": ["events"] }
{ "type": "resume",      "cursor": 448912 }
{ "type": "ping" }
```

### Server to client

```jsonc
// 1. immediately on connect
{ "type": "snapshot", "cursor": 448912, "data": {
    "agents": [], "tasks": [], "runs": [], "world": { "layout": {}, "placements": [] } } }

// 2. then deltas, each carrying the monotonic per-project cursor
{ "type": "agent.updated", "cursor": 448913, "data": { "id": "a7f1...", "status": "working",
    "current_task": { "id": "t9...", "progress_percent": 45 } } }
{ "type": "task.updated",  "cursor": 448914, "data": {} }
{ "type": "event",         "cursor": 448915, "data": {} }   // raw event, incl. custom.*
{ "type": "world.updated", "cursor": 448916, "data": {} }

// 3. control frames
{ "type": "pong", "server_time": "2026-10-31T09:14:10Z" }
{ "type": "resync_required", "reason": "cursor_too_old" }
{ "type": "error", "code": "ticket_expired" }
```

Guarantees:

- `cursor` is strictly increasing per project. A client that reconnects with `resume` gets
  every delta it missed, or `resync_required` if the gap exceeds the replay window
  (1 hour / 10,000 events), which it answers by re-fetching the snapshot.
- Deltas are **state snapshots of the changed entity**, not patches. Ordering bugs cannot
  produce a half-applied entity in the UI.
- Heartbeat: server pings every 30s, closes after two misses.
- One socket per project per tab. Channel subscriptions let the island subscribe to
  `agents` and `world` while an open log viewer adds `events`.

## 7. SDK surface (thin by design)

The SDKs are convenience wrappers — buffering, retry with jitter, heartbeat thread,
context managers — over the REST protocol. Everything they do is possible with `curl`,
and the Python SDK's test suite asserts that by driving the same flows through raw HTTP.

```python
from agentworld import AgentWorld

aw = AgentWorld(api_key=os.environ["AGENTWORLD_KEY"])
agent = aw.agent("etl-worker-eu-1", kind="etl",
                 runtime={"language": "python", "framework": "langgraph"})
agent.start_heartbeat()                      # background thread, honours server TTL

for task in agent.claim_tasks(max=1):
    with agent.run(task) as run:             # run.started / run.finished|failed
        for i, f in enumerate(files):
            run.step("parse", kind="io")
            task.progress(percent=i * 5, message=f"parsed {i}/{len(files)}")
        task.complete(result={"flagged": 3}) # task.completed
```

```ts
import { AgentWorld } from "@agentworld/sdk";

const aw = new AgentWorld({ apiKey: process.env.AGENTWORLD_KEY! });
const agent = await aw.agent("support-triage", { kind: "classifier" });
await agent.heartbeat();
await agent.emit("custom.cache_warmed", { entries: 1204 });
```

Both SDKs buffer events and flush on a 200ms timer or 100 events, drop to disk on
prolonged failure where a path is configured, and never throw into the caller's control
flow: a monitoring library that can crash the monitored agent is worse than no monitoring.

## 8. Rate limits

| Endpoint group | Default | Enforcement |
|----------------|---------|-------------|
| `/v1/ingest/events` | `api_keys.rate_limit_rps`, default 50 req/s per key, burst 200 | Redis token bucket |
| `/v1/ingest/heartbeat` | 2 req/s per agent_key | Redis token bucket |
| Control plane reads | 100 req/s per user | Redis token bucket |
| Control plane writes | 20 req/s per user | Redis token bucket |
| WebSocket connections | 20 concurrent per user, 200 per project | Connection registry |

`429` responses carry `Retry-After`, and the SDKs honour it.

## 9. Health and metadata

| Path | Purpose |
|------|---------|
| `GET /healthz` | Liveness. No dependency checks. |
| `GET /readyz` | Readiness: DB and Redis reachable. |
| `GET /metrics` | Prometheus. |
| `GET /v1/meta` | API version, protocol version, registered adapters, server time. |
| `GET /openapi.json` | OpenAPI 3.1, the source for the generated TypeScript client. |
