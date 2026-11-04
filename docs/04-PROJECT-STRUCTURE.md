# AgentWorld — Project Structure

> Revision 1 (2026-10-31). A pnpm + uv monorepo. The API and the web app share no runtime
> code; they share only the generated client and the protocol schemas.

## 1. Top level

```
rs-agent-world/
├── apps/
│   ├── api/                  Python 3.12 + FastAPI backend
│   └── web/                  Next.js + React + TypeScript frontend
├── packages/
│   ├── protocol/             JSON Schemas for the canonical event envelope (source of truth)
│   ├── sdk-python/           agentworld  (pip)
│   └── sdk-typescript/       @agentworld/sdk  (npm)
├── infra/
│   ├── docker/               Dockerfiles for api, worker, web
│   ├── compose/              docker-compose.yml for local Postgres + Redis
│   └── migrations-ci/        schema drift check
├── docs/
│   ├── 01-ARCHITECTURE.md
│   ├── 02-DATABASE.md
│   ├── 03-API.md
│   ├── 04-PROJECT-STRUCTURE.md
│   ├── 05-ROADMAP.md
│   ├── adr/                  one file per decision, superseding allowed
│   └── protocol/             generated human docs for the event types
├── scripts/                  dev bootstrap, seed, codegen, release
├── .github/workflows/
├── pnpm-workspace.yaml
├── Makefile                  the single entry point for every dev command
└── README.md
```

Two rules that the layout exists to enforce:

- `apps/web` may import from `packages/sdk-typescript` and `lib/api` (generated). It may
  never import from `apps/api`.
- `packages/protocol` is generated **from** `apps/api/src/agentworld/domain/protocol/`
  and consumed by both SDKs and the web app. One definition, three consumers.

## 2. Backend — `apps/api`

```
apps/api/
├── pyproject.toml            uv, ruff, mypy strict, pytest config
├── alembic.ini
├── migrations/versions/
├── src/agentworld/
│   ├── main.py               app factory, router mounting, lifespan
│   ├── settings.py           pydantic-settings; every env var declared once
│   │
│   ├── domain/               ← no I/O, no framework, no provider SDK
│   │   ├── entities.py       Agent, Task, Run, Project, Workspace, ApiKey
│   │   ├── enums.py          AgentStatus, TaskStatus, Severity, MemberRole
│   │   ├── protocol/
│   │   │   ├── envelope.py   CanonicalEvent — the wire contract
│   │   │   ├── types.py      the event-type registry and their payload models
│   │   │   └── version.py    PROTOCOL_VERSION
│   │   ├── reducers/
│   │   │   ├── registry.py   @reducer("task.progress") decorator
│   │   │   ├── agent.py      agent.* -> Agent state
│   │   │   ├── task.py       task.* -> Task state
│   │   │   └── run.py        run.*, tool.* -> Run state
│   │   ├── policy.py         authorize(principal, action, resource)
│   │   └── errors.py         domain exceptions, mapped to problem details in api/
│   │
│   ├── services/
│   │   ├── ingest.py         validate → dedupe → persist → fold → publish
│   │   ├── agents.py
│   │   ├── tasks.py          includes the SKIP LOCKED claim
│   │   ├── commands.py
│   │   ├── projects.py
│   │   ├── workspaces.py
│   │   ├── api_keys.py       mint, hash, verify, revoke
│   │   ├── world.py          layout and placement (presentation)
│   │   └── webhooks.py       outbound dispatch planning
│   │
│   ├── repositories/
│   │   ├── base.py           TenantScope; no method exists without it
│   │   ├── models.py         SQLAlchemy 2.0 declarative models
│   │   ├── agents.py  tasks.py  runs.py  events.py  logs.py  metrics.py
│   │   ├── world.py
│   │   └── unit_of_work.py
│   │
│   ├── adapters/
│   │   ├── base.py           Adapter protocol, Capability, InboundRequest
│   │   ├── registry.py       entry-point discovery ("agentworld.adapters")
│   │   ├── generic_rest.py
│   │   ├── generic_webhook.py
│   │   ├── langgraph.py
│   │   ├── openai_assistants.py
│   │   └── anthropic_sdk.py
│   │
│   ├── api/
│   │   ├── deps.py           get_user_principal / get_key_principal — mutually exclusive
│   │   ├── errors.py         exception handlers → RFC 9457
│   │   ├── schemas/          request/response pydantic models, per resource
│   │   ├── v1/
│   │   │   ├── me.py  workspaces.py  projects.py  agents.py  tasks.py
│   │   │   ├── api_keys.py  events.py  logs.py  metrics.py
│   │   │   ├── integrations.py  webhooks.py  world.py  meta.py
│   │   │   ├── ingest.py     the ingest plane
│   │   │   ├── self.py       agent self-service
│   │   │   └── hooks.py      adapter inbound
│   │   └── ws/
│   │       ├── gateway.py    connection registry, subscribe/resume, backpressure
│   │       └── frames.py     frame models
│   │
│   ├── infra/
│   │   ├── db.py             async engine, session, RLS session variable
│   │   ├── event_bus.py      EventBus protocol + InProcess + Redis impls
│   │   ├── redis.py
│   │   ├── auth/
│   │   │   ├── supabase.py   JWKS fetch + cache + RS256 verify
│   │   │   └── provider.py   AuthProvider interface (swappable)
│   │   ├── secrets/
│   │   │   ├── keyring.py    the KEK map, parsed and refused at boot
│   │   │   └── envelope.py   AES-256-GCM, per-row data key, AAD-bound
│   │   ├── hashing.py        Argon2id for API-key secrets (not credentials)
│   │   ├── ratelimit.py
│   │   ├── logging.py        structlog, request-id binding
│   │   └── telemetry.py      OpenTelemetry setup
│   │
│   └── workers/
│       ├── webhook_dispatcher.py
│       ├── heartbeat_sweeper.py   agents past TTL → offline + status event
│       ├── partition_maintainer.py
│       ├── rollups.py
│       └── retention.py
│
└── tests/
    ├── unit/
    │   ├── reducers/         pure, table-driven, no DB
    │   ├── policy/
    │   └── adapters/
    ├── integration/          real Postgres + Redis via testcontainers
    │   ├── test_ingest.py    batch, dedupe, partial failure, out-of-order
    │   ├── test_replay.py    truncate state, replay events, assert identical
    │   ├── test_ws.py        snapshot, deltas, resume, resync
    │   └── test_claim.py     concurrent claims never double-assign
    ├── conformance/
    │   ├── test_protocol_compat.py   the six promises in 03-API.md §5
    │   ├── test_headless.py          full lifecycle with no frontend involved
    │   └── test_tenant_isolation.py  every endpoint, foreign id, expect 404
    └── factories.py
```

### Enforced boundaries

`importlinter` contracts in `pyproject.toml`, run in CI:

```
domain      -> may import: (nothing internal)
services    -> domain, repositories
repositories-> domain
adapters    -> domain
api         -> domain, services, adapters, infra
infra       -> domain
```

Plus one forbidden-module contract: `agentworld.domain` must not import `sqlalchemy`,
`fastapi`, `httpx`, `openai`, `anthropic` or `redis`. That single check is what keeps goal
G1 true a year from now.

## 3. Frontend — `apps/web`

```
apps/web/
├── package.json
├── next.config.ts
├── app/
│   ├── layout.tsx
│   ├── (auth)/login/  (auth)/callback/        Supabase Auth client flow
│   ├── (app)/
│   │   ├── layout.tsx                         shell: workspace switcher, nav
│   │   ├── w/[workspace]/page.tsx             project list
│   │   └── w/[workspace]/p/[project]/
│   │       ├── page.tsx                       dashboard (overview + island)
│   │       ├── island/page.tsx                full-screen island
│   │       ├── agents/page.tsx  agents/[id]/page.tsx
│   │       ├── tasks/page.tsx   tasks/[id]/page.tsx
│   │       ├── activity/page.tsx              live event feed
│   │       ├── logs/page.tsx
│   │       └── settings/
│   │           ├── keys/  integrations/  webhooks/  world/
│   └── api/auth/callback/route.ts
│
├── lib/
│   ├── api/
│   │   ├── generated/        openapi-typescript output — never hand-edited
│   │   └── client.ts         fetch wrapper: auth header, problem-details parsing
│   ├── realtime/
│   │   ├── socket.ts         connect, ticket refresh, backoff, resume cursor
│   │   └── store.ts          Zustand live domain state fed by deltas
│   ├── query/                TanStack Query keys and hooks
│   └── auth/                 Supabase browser + server clients
│
├── features/
│   ├── agents/   components, status badges, agent detail panels
│   ├── tasks/    table, filters, create dialog, timeline
│   ├── activity/ virtualised event feed
│   ├── keys/     create dialog with the show-once secret
│   └── overview/ counters, throughput chart
│
├── world/                    ← the visual engine, fully quarantined
│   ├── types.ts              WorldState, WorldEntity, AnimationIntent
│   ├── projector/
│   │   ├── project.ts        project(domain, layout) -> WorldState   (pure)
│   │   ├── rules.ts          status → visual state; event → animation intent
│   │   └── project.test.ts   the island's real test suite
│   ├── layout/
│   │   └── autolayout.ts     deterministic tile assignment for new agents
│   ├── phaser/
│   │   ├── game.ts           Phaser.Game bootstrap, isometric projection
│   │   ├── scenes/IslandScene.ts  BootScene.ts
│   │   ├── entities/AgentCharacter.ts  Building.ts  ResourceToken.ts
│   │   ├── fx/               smoke, glows, pop-ins
│   │   └── assets/           sprite atlases, tilesets, manifest.json
│   └── bridge/
│       ├── IslandCanvas.tsx  client component; mounts Phaser once
│       └── useWorldSync.ts   store subscription → scene.applyWorldState()
│
├── components/ui/            design system primitives
└── tests/
    ├── unit/                 projector, rules, store reducers
    └── e2e/                  Playwright: login → create project → ingest → island reacts
```

### Enforced boundaries

An `eslint-plugin-boundaries` config with two rules:

- Nothing outside `world/` may import `world/phaser/**` or `phaser` itself.
- `world/projector/**` may not import React, Phaser, or anything from `lib/api`. It takes
  plain data in and returns plain data out.

The Phaser bundle is loaded with `next/dynamic({ ssr: false })` from exactly one place,
`bridge/IslandCanvas.tsx`, so the rest of the app never pays for it.

## 4. Protocol package — `packages/protocol`

```
packages/protocol/
├── schemas/
│   ├── envelope.schema.json
│   ├── events/agent.registered.schema.json   (one per canonical type)
│   └── index.json
├── generate.py               reads domain/protocol/, writes schemas/
└── README.md
```

`make protocol` regenerates the schemas and fails CI on drift. The TypeScript SDK types
and the docs in `docs/protocol/` are both built from here, which means the protocol can
never be documented differently from how it is validated.

## 5. SDKs

```
packages/sdk-python/
├── src/agentworld/
│   ├── client.py      AgentWorld — transport, retry, Retry-After handling
│   ├── agent.py       AgentHandle — heartbeat thread, claim, run context manager
│   ├── buffer.py      batching, flush timer, optional disk spill
│   ├── types.py       generated from packages/protocol
│   └── errors.py      never raised into caller control flow by default
└── tests/             against a recorded server and a live testcontainer

packages/sdk-typescript/
├── src/{client,agent,buffer,types}.ts
└── tests/
```

## 6. Developer commands

One `Makefile`, so nobody has to remember two toolchains:

| Command | Does |
|---------|------|
| `make bootstrap` | uv sync, pnpm install, copy `.env.example`, start compose, migrate, seed |
| `make dev` | compose up + API (reload) + web (turbo) concurrently |
| `make test` | unit + integration + conformance (api), vitest (web) |
| `make test-fast` | unit only, no containers — the pre-commit loop |
| `make lint` | ruff, mypy --strict, import-linter, eslint, tsc --noEmit |
| `make migrate` / `make migration m="..."` | Alembic upgrade / autogenerate |
| `make codegen` | OpenAPI → TypeScript client, protocol → JSON Schema |
| `make seed-demo` | a workspace, a project, 6 agents, a task generator for island work |
| `make e2e` | Playwright against a fully seeded stack |

## 7. CI pipeline

| Job | Gate |
|-----|------|
| `lint-api` | ruff, mypy strict, **import-linter** |
| `test-api` | pytest with Postgres + Redis services; coverage floor 85% on `domain/` and `services/` |
| `conformance` | protocol-compat, headless lifecycle, tenant isolation |
| `lint-web` | eslint (incl. boundaries), `tsc --noEmit` |
| `test-web` | vitest; the projector suite must stay green independent of Phaser |
| `codegen-drift` | regenerate client and schemas, fail if the tree changes |
| `migrations` | fresh DB, apply all migrations, then run `test-api` |
| `e2e` | Playwright on PRs touching `apps/web` |
| `build-images` | api, worker, web Docker builds on `main` |

## 8. Configuration

Two files, one per app, because Next.js loads `.env` from its own project directory
rather than from the repository root. A `NEXT_PUBLIC_*` value placed in the root `.env`
is read by nothing - the API has no use for it and Next never looks there - so the web
app boots on its defaults and renders the setup page as though it were unconfigured.

### `.env` (apps/api)

From `.env.example`, every variable documented in `settings.py`:

```
DATABASE_URL=postgresql+asyncpg://agentworld:dev@localhost:5432/agentworld
REDIS_URL=redis://localhost:6379/0
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_JWT_AUDIENCE=authenticated
SUPABASE_JWKS_URL=https://xxxx.supabase.co/auth/v1/.well-known/jwks.json
API_KEY_PEPPER=change-me                # Argon2id pepper, rotatable
CREDENTIAL_KEKS={"k2026a":"<base64>"}   # credential KEKs; several valid at once
CREDENTIAL_ACTIVE_KEK_ID=k2026a         # the one that wraps new data keys
EVENT_BUS=redis                         # redis | inprocess
WS_REPLAY_WINDOW_EVENTS=10000
DEFAULT_HEARTBEAT_TTL_SECONDS=60
CORS_ORIGINS=http://localhost:3000
OTEL_EXPORTER_OTLP_ENDPOINT=
```

No secret has a working default. `settings.py` fails fast at boot on a missing required
variable rather than at the first request that needs it.

### `apps/web/.env.local` (apps/web)

From `apps/web/.env.example`:

```
NEXT_PUBLIC_API_URL=http://localhost:8000
NEXT_PUBLIC_WS_URL=ws://localhost:8000
NEXT_PUBLIC_SUPABASE_URL=
NEXT_PUBLIC_SUPABASE_ANON_KEY=
```

`NEXT_PUBLIC_*` is inlined into the browser bundle at build time, so only public values
belong in this file - the Supabase *anon* key is designed for it. Server-only secrets
(`API_KEY_PEPPER`, `CREDENTIAL_KEKS`, `MIGRATION_DATABASE_URL`, a service-role key) stay
in the root `.env`, and giving one a `NEXT_PUBLIC_` name is what would publish it.
`apps/web/tests/public-env.test.ts` holds this split in place.
