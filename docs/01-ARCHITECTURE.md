# AgentWorld — Architecture

> Status: design, pre-implementation. Revision 1 (2026-10-31).

## 1. What this is

AgentWorld is a standalone platform for connecting, monitoring and managing AI agents
that live in *other people's applications*. It is not a framework you build agents with,
and it is not tied to any model provider.

The monitored application:

- may be written in any language,
- may use any AI provider (or none),
- may have a UI or be entirely headless,
- never imports AgentWorld code as a hard dependency — it only speaks a documented wire
  protocol (REST + webhooks), with optional thin SDKs for convenience.

### Design goals

| # | Goal | How it is enforced |
|---|------|--------------------|
| G1 | Provider independence | No provider SDK in the core. Provider specifics live only in `adapters/`. |
| G2 | Multi-tenant | Every row is reachable from exactly one `workspace_id`; tenant scope is a required repository argument. |
| G3 | Multi-agent, multi-project | `workspace -> project -> agent` hierarchy, no global agent namespace. |
| G4 | Real time | Append-only event log plus pub/sub fan-out to WebSocket subscribers. |
| G5 | Extensible integrations | Adapter registry with a stable `Adapter` interface and declared capabilities. |
| G6 | Secure auth | Two separate auth planes (human JWT vs. machine API key), never interchangeable. |
| G7 | Independent frontend/backend | The frontend is a pure API consumer. No SSR coupling to the DB, no shared runtime. |
| G8 | Visuals are a projection | The island renders *derived* state. Deleting the whole visual layer must not change backend behaviour. |

### Explicit non-goals (v1)

- Running or scheduling agents for the user. AgentWorld observes and commands; it does
  not host inference.
- Prompt/LLM cost analytics beyond generic metric ingestion.
- On-prem single-binary distribution.
- Agent-to-agent message routing. Events are observational, not a message bus for the
  agents themselves.

## 2. System context

```
+---------------------------- Customer side -----------------------------+
|                                                                        |
|  Python agent     Node agent      Headless worker     Any app          |
|  (LangGraph)      (custom)        (cron/queue)        (webhook only)   |
|      |                |                 |                 |           |
|      | SDK            | SDK             | raw REST        | webhook   |
+------|----------------|-----------------|-----------------|-----------+
       |                |                 |                 |
       v                v                 v                 v
+------------------------------------------------------------------------+
|                       AgentWorld API  (FastAPI)                        |
|                                                                        |
|   Ingest plane (API key)            Control plane (Supabase JWT)       |
|   /v1/ingest/*                      /v1/workspaces, /v1/projects, ...  |
|        |                                      |                        |
|        v                                      v                        |
|   +-----------+    +------------+    +-----------------+               |
|   | Adapter   |--->| Event      |--->| Domain services |               |
|   | registry  |    | normaliser |    | + reducers      |               |
|   +-----------+    +------------+    +--------+--------+               |
|                                               |                        |
|                 +-----------------------------+------------+           |
|                 v                     v                    v           |
|           PostgreSQL              Event bus          Outbound          |
|           (truth)                 (Redis pub/sub)    webhooks          |
|                                        |                               |
|                                        v                               |
|                              WebSocket gateway                         |
+----------------------------------------+-------------------------------+
                                         | wss
                                         v
+------------------------------------------------------------------------+
|                   AgentWorld Web  (Next.js + React + TS)               |
|                                                                        |
|   REST + WS client  ->  domain store  ->  +-----------------+          |
|                                           | World projector |          |
|   Dashboards / tables / forms  -----------+--------+--------+          |
|                                                    v                   |
|                                          Phaser isometric island       |
+------------------------------------------------------------------------+
```

## 3. The core: the Agent Protocol

Everything else in the system is replaceable. This is not.

The protocol is a **canonical event envelope** plus a small set of **event types**.
Agents push events; AgentWorld folds those events into current state.

```jsonc
{
  "dedup_key": "run-3f9a:step-12",     // idempotency, unique per project
  "type": "task.progress",             // canonical type, see section 4
  "occurred_at": "2026-10-31T09:14:02.511Z",
  "agent_key": "etl-worker-eu-1",      // stable, caller-chosen agent identity
  "task_ref": "ticket-8812",           // caller's own task id, optional
  "run_ref": "run-3f9a",               // optional grouping, optional
  "severity": "info",                  // debug|info|warning|error|critical
  "labels": { "region": "eu-west-1" }, // low-cardinality, indexed
  "payload": { "percent": 45, "message": "parsed 9/20 files" }
}
```

Rules that keep the protocol honest:

1. **Agents are identified by `agent_key`, not by a server-issued UUID.** A worker that
   restarts, redeploys or scales out keeps its identity without persisting anything.
2. **Events are facts about the past.** `occurred_at` comes from the client,
   `received_at` from the server. Clock skew is recorded, never silently corrected.
3. **Ingestion is idempotent.** `(project_id, dedup_key)` is unique. Retries are free.
4. **Unknown event types and unknown payload fields are accepted and stored**, not
   rejected. Forward compatibility beats strictness at the edge.
5. **State is derived, never directly writable by agents.** An agent cannot `PATCH` its
   own status row; it emits `agent.status_changed` and the server folds it. One
   auditable path into state.
6. **Nothing in the protocol names a provider, a model or a UI concept.** No `openai_*`
   fields, no `building`, no `sprite`.

### Fold (reducer) semantics

```
event -> validate envelope -> resolve agent/task/run (upsert by ref)
      -> append to events (immutable)
      -> apply reducer -> agents / tasks / runs current-state rows
      -> publish to event bus -> WebSocket subscribers
                              -> outbound webhook dispatcher
```

Reducers are pure functions `(current_state, event) -> new_state`, registered per event
type. They are the only code allowed to mutate current-state tables from ingested data,
which makes the pipeline replayable: truncate current state, replay `events`, get the
same result. That replay equivalence is a first-class test (roadmap Phase 2).

## 4. Canonical event taxonomy

| Namespace | Types | Folds into |
|-----------|-------|-----------|
| `agent` | `registered`, `heartbeat`, `status_changed`, `capability_changed`, `deregistered` | `agents` |
| `task` | `created`, `assigned`, `started`, `progress`, `blocked`, `unblocked`, `completed`, `failed`, `cancelled` | `tasks` |
| `run` | `started`, `step_started`, `step_finished`, `finished`, `failed` | `runs` |
| `tool` | `invoked`, `returned`, `failed` | `runs` counters, `events` |
| `log` | `emitted` | `logs` |
| `metric` | `recorded` | `metric_samples` |
| `custom` | `custom.<anything>` | `events` only — stored and broadcast, never folded |

`custom.*` is the escape hatch that stops users from abusing semantic types. It shows up
in the activity feed and streams over WebSocket, but it never touches current state.

## 5. Component breakdown

### 5.1 Backend (`apps/api`, Python 3.12 + FastAPI)

Layered; dependencies point inward only.

```
api/           FastAPI routers, request/response schemas, auth dependencies
                 | calls
services/      use cases: AgentService, TaskService, IngestService, ...
                 |
domain/        entities, enums, reducers, protocol schemas - zero I/O, zero framework
                 ^ implemented by
repositories/  SQLAlchemy 2.0 async data access
adapters/      provider and framework integrations (see 5.3)
infra/         db session, event bus, JWKS client, settings, logging
```

`domain/` imports nothing from `api/`, `repositories/` or any third-party SDK except
pydantic. CI enforces this with an import-linter contract, because G1 and G8 decay
silently otherwise.

Processes:

| Process | Responsibility |
|---------|----------------|
| `api` (uvicorn) | REST + WebSocket. Stateless, horizontally scalable. |
| `worker` (arq) | Outbound webhook delivery with retries, metric rollups, stale-agent sweeps. |
| `scheduler` | Periodic: mark agents offline past heartbeat TTL, partition maintenance, retention. |

### 5.2 Real-time path

- **Source of truth:** the PostgreSQL `events` table (append-only, monthly partitions).
- **Fan-out:** an `EventBus` interface with two implementations —
  `InProcessEventBus` (dev, single worker) and `RedisEventBus` (production, pub/sub on
  channel `project:{project_id}`). The interface exists so the WebSocket gateway never
  knows which one it is using.
- **WebSocket contract:** on connect the client receives a **snapshot** of project state
  (agents, open tasks, world placements, a `cursor`), then **deltas**. On reconnect the
  client sends its `cursor` and receives a replay from `events`, so a dropped socket
  never leaves the island showing stale agents. Details in `03-API.md` section 6.
- **Backpressure:** per-connection bounded queue. On overflow the server drops deltas and
  sends `{"type": "resync_required"}` rather than growing memory.

### 5.3 Adapter system

An adapter translates somebody else's shape into canonical events. It never reaches into
the database.

```python
class Adapter(Protocol):
    slug: str                              # "langgraph", "openai-assistants", ...
    display_name: str
    capabilities: frozenset[Capability]    # INGEST_WEBHOOK | POLL | COMMAND | TRACE_IMPORT
    config_schema: type[BaseModel]         # rendered as a form in the UI

    def verify(self, req: InboundRequest, cfg: BaseModel) -> None: ...
    def normalise(self, req: InboundRequest, cfg: BaseModel) -> list[CanonicalEvent]: ...
```

Adapters are registered via Python entry points (`agentworld.adapters`), so a third party
ships one as a separate pip package and AgentWorld discovers it at boot. Each integration
instance is a row in `integrations` with its own config and secret.

v1 ships: `generic-rest` (the native protocol), `generic-webhook` (HMAC-verified JSON with
a JSONPath mapping config), `langgraph`, `openai-assistants`, `anthropic-sdk`.

### 5.4 Auth: two planes, never mixed

| | Control plane | Ingest plane |
|---|---|---|
| Principal | Human user | Agent / machine |
| Credential | Supabase Auth JWT (RS256/ES256, verified against cached JWKS) | API key `aw_<env>_<random>.<secret>` |
| Carried in | `Authorization: Bearer <jwt>` | `X-AgentWorld-Key: <key>` |
| Scope | Workspace membership and role | Pinned to one project, optional agent-key allowlist |
| Can do | Everything the role allows, including key management | Ingest events, read own agent/task rows, claim tasks |
| Cannot do | — | Enumerate other agents, read other projects, mint keys |

Keys are stored as Argon2id hashes of a peppered secret. Only `prefix`
(`aw_live_8f2c1a3d5e70`) is queryable, and the prefix is what the UI shows. A leaked key is
revocable individually, not per project. Rate limits are per key (token bucket in Redis).

JWT verification is entirely local: the JWKS document is cached, so an outage of the
identity provider does not take the API down with it, and a token check costs one signature
verification rather than a network round trip. The accepted algorithm list is fixed to the
asymmetric ones — taking the algorithm from the token's own header is the JWT confusion
attack, where a forger signs `alg: HS256` with the published *public* key and a lenient
verifier accepts it as a valid HMAC. An unknown `kid` does prompt one refetch, so rotation
works without a restart, but at most one per cooldown window: unauthenticated callers
choose that `kid`, and without the cap a stream of forged tokens becomes a
denial-of-service amplifier aimed at the identity provider.

Authorization is a single `authorize(principal, action, resource)` call in the service
layer rather than scattered route checks, and every mutating control-plane call writes an
`audit_log` row — in the same transaction as the change it describes, so a rolled-back
mutation leaves no claim that it happened and a successful one cannot fail to be recorded.

`authorize` also decides 404 against 403: a principal with no membership in the resource's
workspace gets `NotFoundError`, because a 403 would confirm to a stranger that the
workspace exists and that they guessed its id. Once membership is established the
distinction is safe to make, and a 403 naming the required role is far more useful.

The two principal types are separate classes rather than one with a `kind` field, so a
control-plane function cannot accept an agent's key: the type checker refuses it before any
test does. `authorize` additionally fails closed for any key principal, so the guarantee
does not rest on the routing table staying correct.

### 5.5 Frontend (`apps/web`, Next.js App Router + TypeScript)

```
app/                routes; server components for shell/auth, client for live data
features/           agents/, tasks/, projects/, keys/, activity/
lib/api/            TypeScript client generated from the API's OpenAPI document
lib/realtime/       WebSocket client, cursor handling, reconnect and resync
world/
  projector/        DomainState -> WorldState (pure, unit-tested, no Phaser import)
  phaser/           Scene, sprites, isometric tilemap, animation controllers
  bridge/           React <-> Phaser mount; one subscription, no React re-render per frame
```

TanStack Query for REST, a small Zustand store as the live domain state that the
WebSocket feeds. Phaser subscribes to that store **outside** React's render cycle.

## 6. The visual layer contract (why the island cannot leak)

The island is a **projection**, defined by one pure function:

```ts
project(domain: DomainState, layout: LayoutConfig): WorldState
```

- `DomainState` — agents, tasks, runs, recent events. Exactly what the API returns.
- `LayoutConfig` — presentation-only data: tile coordinates, building kind, character
  sprite. Stored in `world_placements`, a table no reducer and no adapter ever touches.
- `WorldState` — tiles, entities, animation intents (`walk_to`, `work`, `idle`, `alert`).

Mapping rules, all of them in the projector:

| Domain fact | Island expression |
|-------------|-------------------|
| `agent.status = working` | character at its building, work animation, chimney smoke |
| `agent.status = idle` | character idles outside, building dim |
| `agent.status = blocked` | question-mark bubble, amber building glow |
| `agent.status = error` | red glow, character slumped |
| `agent.status = offline` | character absent, building shuttered |
| `task.started` | character walks from the plaza to its building |
| `task.completed` | resource token pops and flows to the project warehouse |
| `task.failed` | smoke puff, red flash |
| `run.step_finished` | one progress notch on the building's banner |

Two consequences worth stating plainly:

1. **Every animation traces to a real event.** The projector has no timers that invent
   activity. Idle means idle; a still island is a truthful island.
2. **Deleting `world/` breaks no test outside `world/`.** CI enforces it with a dependency
   rule: nothing outside `world/` may import from `world/phaser/`.

## 7. Deployment

| Concern | Choice |
|---------|--------|
| API | Docker image, any container host (Fly / Render / ECS). Stateless. |
| Web | Vercel or container. Talks to the API over HTTPS/WSS only. |
| DB | Managed PostgreSQL 16 (Supabase Postgres or any). `pgcrypto`, partitioned `events`. |
| Cache / bus | Managed Redis 7. |
| Migrations | Alembic, forward-only, run as a release step. |
| Config | Env vars via pydantic-settings; no secrets baked into images. |
| Observability | structlog JSON logs, OpenTelemetry traces, `/healthz` and `/readyz`, Prometheus `/metrics`. |

Local dev: `docker compose up` gives Postgres and Redis; API and web run natively with hot
reload. Nothing in the dev path needs a cloud account except Supabase Auth, which is
stubbed by a local JWT signer in tests.

## 8. Decisions on the record

| ID | Decision | Why | Rejected alternative |
|----|----------|-----|----------------------|
| AD-1 | Event-sourced ingest folded into current-state tables | Replayable, auditable, cheap real-time | Direct state writes from agents (unauditable, racy) |
| AD-2 | `agent_key` as client-chosen identity | Stateless, restart-safe agents | Server-issued UUIDs the agent must persist |
| AD-3 | Separate API-key plane from user JWTs | A leaked agent key must not be a user session | One token type with scopes |
| AD-4 | Redis pub/sub for fan-out, Postgres as truth | Simple; truth is never duplicated | Postgres `LISTEN/NOTIFY` (payload limits, per-connection cost) |
| AD-5 | Layout stored apart from domain state | Keeps G8 structurally true | A `building_type` column on `agents` |
| AD-6 | Adapters as entry-point plugins | Third parties extend without forking | Hardcoded `if provider == ...` branches |
| AD-7 | Accept unknown event types | Clients upgrade before servers do | Strict schema rejection at ingest |
| AD-8 | Phaser subscribes to the store, not to React props | 60fps without React churn | Re-rendering the canvas via props |

## 9. Risk register

| Risk | Mitigation |
|------|-----------|
| Event volume from chatty agents | Per-key rate limits, per-project log severity floor, monthly partitions plus retention policy |
| The island becomes the de-facto API shape | Projector is pure and tested; CI import rule; `world_placements` isolation |
| Adapter authors get DB access by accident | Adapters receive `InboundRequest` and return events; no session is in scope |
| Supabase lock-in on auth | Only JWKS verification and the user id are consumed; an `AuthProvider` interface allows swapping |
| Multi-tenant leak | Every repository method takes an explicit tenant scope; a test suite asserts cross-tenant 404s on every endpoint |
