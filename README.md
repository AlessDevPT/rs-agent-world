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

**Phase 0 complete** — the skeleton runs, and the architecture contracts that protect
the design are live and verified. Phase 1 (tenancy and auth) is next.

| Phase | State |
|-------|-------|
| 0 — Foundations | done |
| 1 — Tenancy and auth | next |
| 2–8 | see [the roadmap](docs/05-ROADMAP.md) |

What works today: the API boots and serves `/healthz`, `/readyz` and `/v1/meta`; every
error is an RFC 9457 problem document carrying a request id; the web shell renders and
reports its live connection to the API; `docker compose` brings up Postgres and Redis.
No agents, no events, no island yet.

## Quick start

```bash
cp .env.example .env
npm run api:install     # creates apps/api/.venv and installs the API
npm install             # installs the web workspace
npm run infra:up        # Postgres + Redis
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

Properties 1 and 4 are mechanically enforced as of Phase 0, not left to discipline:

| Guarantee | Enforced by | Verified |
|-----------|-------------|----------|
| `domain/` imports no framework or provider SDK | import-linter contract + a pytest AST guard | planting `import anthropic` fails both |
| Dependencies point inward only | import-linter layers contract | — |
| Adapters never touch persistence | import-linter forbidden contract | — |
| Nothing outside `world/` imports Phaser | eslint `no-restricted-imports` | planting the import fails lint |
| `world/projector` stays pure | eslint, scoped to that directory | planting `import react` fails lint |

## Next step

Phase 1: tenancy and auth — the schema's tenancy tables, Supabase JWT verification,
workspaces and projects, Argon2id-hashed API keys, and row-level security from the start.
