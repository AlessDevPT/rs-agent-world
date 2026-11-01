# AgentWorld

A standalone, provider-independent platform for connecting, monitoring and managing AI
agents — wherever they run, in whatever language, with or without a UI of their own.

Monitored applications never import AgentWorld. They speak a small documented wire
protocol (REST, webhooks, optional thin SDKs), and AgentWorld folds the resulting events
into live state: agent status, tasks, runs, logs and metrics — plus an animated isometric
island, inspired by strategy board games, where each agent has a character, a building, a
current activity and an assigned task. Every animation on that island traces to a real
backend event; nothing is simulated.

## Status

**Phase 1 complete (backend)** — multi-tenant, authenticated, and isolated at two
independent layers. Phase 2 (the agent protocol and event ingest) is next: the core of
the product, and the first phase where an agent appears.

| Phase | State |
|-------|-------|
| 0 — Foundations | done |
| 1 — Tenancy and auth | done |
| 2 — Protocol and ingest | next |
| 3–8 | see [the roadmap](docs/05-ROADMAP.md) |

What works today, end to end over HTTP: a user authenticates with a Supabase JWT, is
mirrored locally by their first `GET /v1/me`, creates a workspace and projects, invites
members with roles, and mints API keys whose plaintext the server cannot recover. Agents
can present a key to `GET /v1/self/key` and be told what they are allowed to do. Every
error is an RFC 9457 problem document carrying a request id, and every mutation writes an
audit row in the same transaction as the change.

No agents, no events, no island yet — and no web UI for any of the above, which is the
remaining half of Phase 1.

## Quick start

```bash
cp .env.example .env
npm run api:install     # creates apps/api/.venv and installs the API
npm install             # installs the web workspace
npm run infra:up        # Postgres + Redis
npm run api:migrate     # apply migrations, as the schema owner
```

Then in two terminals:

```bash
npm run api:dev         # http://localhost:8000  (/docs for the OpenAPI UI)
```

```bash
npm run dev             # http://localhost:3000
```

`make` is the documented entry point and mirrors all of the above, but it is not
installed by default on Windows, so every task is also an `npm run` script.

```bash
npm run verify          # everything CI runs: lint, types, contracts, tests
```

## Design documents

The plan the code is written against.

| Document | Contents |
|----------|----------|
| [01-ARCHITECTURE.md](docs/01-ARCHITECTURE.md) | Goals and non-goals, system context, the agent protocol, components, adapters, auth planes, the visual-layer contract, decisions, risks |
| [02-DATABASE.md](docs/02-DATABASE.md) | Full PostgreSQL schema with runnable SQL, enums, partitioning, RLS, retention, migration order |
| [03-API.md](docs/03-API.md) | REST endpoints for both planes, error and pagination conventions, ingest semantics, compatibility promises, WebSocket frames, SDK surface, rate limits |
| [04-PROJECT-STRUCTURE.md](docs/04-PROJECT-STRUCTURE.md) | Monorepo layout, enforced module boundaries, dev commands, CI jobs, configuration |
| [05-ROADMAP.md](docs/05-ROADMAP.md) | Nine phases with build items, tests, exit criteria, deferrals and milestone gates |

## Stack

| Layer | Choice |
|-------|--------|
| Frontend | Next.js (App Router) + React + TypeScript |
| Visual engine | Phaser (isometric), quarantined behind a pure projector |
| Backend | Python 3.12 + FastAPI |
| Database | PostgreSQL 16 (partitioned event log, RLS) |
| Realtime | WebSockets, Redis pub/sub fan-out |
| Auth | Supabase Auth for users; Argon2id-hashed API keys for agents |
| Integrations | Modular adapter plugins discovered via entry points |

## The idea in one diagram

```
your agents  --(REST / webhook / SDK)-->  canonical events  -->  reducers  -->  live state
                                                                                  |
                                                        +-------------------------+
                                                        |                         |
                                                   dashboards              island projector
                                                                                  |
                                                                            Phaser scene
```

Four properties the whole design is arranged to protect:

1. **No provider in the core.** Nothing under `domain/` may import a provider SDK, and CI
   fails if it does.
2. **Agents own their identity.** An `agent_key` the caller chooses, so a restarted or
   scaled-out worker needs no persisted state.
3. **State is derived.** Agents emit events; only reducers write state. Replaying the
   event log reproduces current state exactly, and a test asserts it.
4. **The island is a projection.** Deleting the entire visual layer changes no backend
   behaviour and breaks no backend test.

These are mechanically enforced, not left to discipline:

| Guarantee | Enforced by | Verified by |
|-----------|-------------|-------------|
| `domain/` imports no framework or provider SDK | import-linter contract + a pytest AST guard | planting `import anthropic` fails both |
| Dependencies point inward only | import-linter layers contract | — |
| Only repositories speak SQL | import-linter forbidden contract (direct imports) | — |
| Adapters never touch persistence | import-linter forbidden contract | — |
| Nothing outside `world/` imports Phaser | eslint `no-restricted-imports` | planting the import fails lint |
| `world/projector` stays pure | eslint, scoped to that directory | planting `import react` fails lint |
| A tenant cannot see another's data | `TenantScope` in the type signature **and** PostgreSQL RLS | every endpoint called with a foreign id returns 404 |
| RLS is not silently inert | the app connects as a non-superuser without `BYPASSRLS` | asserted before any other RLS test runs |
| The audit log cannot be rewritten | `SELECT, INSERT` grants and nothing more | `UPDATE` and `DELETE` are refused by the database |
| Exactly one owner per workspace | partial unique index | a second owner insert is refused |
| A key secret is unrecoverable | Argon2id with a pepper, never stored in clear | every text column searched for the plaintext |
| Each role can do exactly what the spec says | one `authorize()` call, one table | every endpoint × every role, over HTTP |
| Models, migrations and enums agree | Alembic `compare_metadata` + a `pg_enum` comparison | runs against a freshly migrated database |

## Next step

Phase 2: the agent protocol and event ingest — the canonical event envelope, the
append-only partitioned event log, the reducers that are the only writers of current
state, and the replay-equivalence test that keeps event sourcing honest. By the end of it
the whole product works through `curl`, with no frontend in existence.
