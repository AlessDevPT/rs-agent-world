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

**Design complete, implementation not started.** The documents below are the plan the
code will be written against.

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
2. **Agents own their identity.** A `agent_key` the caller chooses, so a restarted or
   scaled-out worker needs no persisted state.
3. **State is derived.** Agents emit events; only reducers write state. Replaying the
   event log reproduces current state exactly, and a test asserts it.
4. **The island is a projection.** Deleting the entire visual layer changes no backend
   behaviour and breaks no backend test.

## Next step

Phase 0 of the roadmap: monorepo skeleton, compose stack, health endpoints, and the
module-boundary contracts that keep the properties above true.
