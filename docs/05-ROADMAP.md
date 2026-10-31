# AgentWorld — Development Roadmap

> Revision 1 (2026-10-31). Each phase ends in something demonstrable and leaves the repo
> green. No phase depends on a later phase's code.

## Ordering principle

The protocol and the ingest pipeline are built and proved **before** anything visual.
That is not aesthetic preference — it is the only ordering under which goal G8 ("visuals
are a projection") can be verified rather than hoped for. By the end of Phase 4 the whole
product works through `curl` with no frontend in existence; the island added in Phase 6
can then be deleted without touching a backend test.

| Phase | Theme | Est. | Demo at the end |
|-------|-------|------|-----------------|
| 0 | Foundations | 2–3 d | `make bootstrap && make dev && make test` all green |
| 1 | Tenancy and auth | 4–5 d | Sign up, create workspace and project, mint an API key |
| 2 | Protocol and ingest | 5–7 d | `curl` an event batch; agent and task state appear; replay is identical |
| 3 | Control plane | 4–5 d | Full agent/task/log/metric management over REST |
| 4 | Realtime | 3–4 d | Two `websocat` clients see the same live deltas; reconnect replays |
| 5 | Web app (no island) | 5–7 d | Usable dashboard: projects, agents, tasks, activity, keys |
| 6 | The island | 7–10 d | Agents on an isometric island reacting to real backend events |
| 7 | SDKs and adapters | 5–7 d | A LangGraph agent and a headless Node worker monitored end to end |
| 8 | Hardening | 5–7 d | Load-tested, rate-limited, observable, documented, deployed |

Total: roughly 8–10 weeks of focused single-developer work. Phases 5–7 can run in
parallel once Phase 4 lands, since they share no files.

---

## Phase 0 — Foundations

**Build**

- Monorepo skeleton per `04-PROJECT-STRUCTURE.md`; pnpm workspace, uv project.
- `docker compose` with Postgres 16 and Redis 7.
- FastAPI app factory with `/healthz`, `/readyz`, `/v1/meta`; structlog with request ids;
  RFC 9457 exception handlers.
- Next.js app with the shell layout and an empty dashboard.
- `Makefile`, `.env.example`, pre-commit hooks.
- CI: lint + test jobs for both apps (trivially green, but wired).
- **The import-linter and eslint-boundaries contracts, from day one.** Adding them later
  means retrofitting; adding them now means the rules shape the code.

**Done when** a fresh clone reaches a running stack and green tests with one command, and
CI fails if `domain/` imports SQLAlchemy.

---

## Phase 1 — Tenancy and auth

**Build**

- Migrations `0001`–`0003` (extensions, enums, tenancy).
- Supabase JWT verification: JWKS fetch and cache, RS256, audience and expiry checks,
  behind the `AuthProvider` interface.
- `GET/PATCH /v1/me` with the `users` mirror upsert and `needs_onboarding`.
- Workspaces, members, invitations, roles; the `authorize()` policy function.
- Projects CRUD.
- API keys: mint, Argon2id hash with pepper, prefix lookup, scopes, allowlist, revoke.
- `audit_log` writes on every mutating control-plane call.
- RLS migration (`0008` brought forward) plus the session variable in `infra/db.py`.
- Web: Supabase login/callback, workspace switcher, project list, create-project form,
  API-key page with the show-once secret dialog.

**Tests**

- Role matrix: every endpoint × every role, asserting allow/deny.
- Tenant isolation: foreign ids return `404` on every control-plane endpoint.
- Key verification: wrong secret, revoked, expired, wrong plane — all `401`.
- A JWT signed by the wrong key, with the wrong audience, or expired — all `401`.

**Done when** a new user can sign up, create a workspace and project, and hold an API key
whose plaintext the server cannot recover.

---

## Phase 2 — Protocol and ingest (the core)

The most important phase. Nothing after it is allowed to widen the protocol.

**Build**

- `domain/protocol/envelope.py` — `CanonicalEvent`, the full type registry and payload
  models, `PROTOCOL_VERSION`.
- Migrations `0004`–`0005`: agents, tasks, runs, run_steps, status history, and the
  partitioned `events` / `logs` / `metric_samples` with the partition-maintenance
  function.
- `services/ingest.py`: validate → resolve-or-upsert refs → dedupe → append → fold →
  publish. Per-project `seq` allocation inside the transaction.
- Reducers for `agent.*`, `task.*`, `run.*`, `tool.*`, with out-of-order tolerance.
- `POST /v1/ingest/events` with partial success, `/heartbeat`, `/logs`, `/metrics`.
- `GET /v1/self`, `POST /v1/self/tasks/claim` (`FOR UPDATE SKIP LOCKED`).
- Per-key rate limiting.
- `heartbeat_sweeper` worker: agents past TTL become `offline` and emit a synthetic
  `agent.status_changed`.
- `packages/protocol` generation plus the drift check.

**Tests** — this is where the test budget goes:

- Reducer unit tests, table-driven, one case per event type including the stale-event
  cases.
- **Replay equivalence:** ingest a recorded stream, snapshot current state, truncate
  current-state tables, replay `events` through the reducers, assert byte-identical
  state. This test is the one that keeps event sourcing honest.
- Idempotency: the same batch three times yields one event per `dedup_key`.
- Partial failure: a 100-event batch with 3 bad events accepts 97.
- Out-of-order: shuffled delivery converges to the same state as ordered delivery.
- Concurrency: 20 agents claiming from 10 pending tasks assigns each exactly once.
- Clock skew: future timestamps rejected past the 300s bound, past timestamps accepted.
- Unknown types and unknown payload fields are stored and retrievable.

**Done when** the full agent lifecycle — register, heartbeat, claim, run, progress,
complete, fail, go offline — can be driven with `curl` alone, and replay reproduces state
exactly.

---

## Phase 3 — Control plane

**Build**

- Agents: list with filters, detail, patch, maintenance toggle, archive, status history.
- Commands: issue, capability check, `GET /v1/self/commands` with long-poll, ack.
- Tasks: CRUD, cancel, retry, dependencies, timeline.
- Events, logs (with full-text search), metrics reads; NDJSON export.
- `GET /v1/projects/{id}/overview`.
- Rollup materialised view and its refresh job; retention job.

**Tests**

- Command rejected when the agent has not advertised the capability.
- Cancel while running produces the command and the terminal task state.
- Cursor pagination is stable while new events arrive mid-page.
- Overview counts match a hand-built fixture.

**Done when** everything an operator needs is reachable over REST, with no WebSocket and
no UI.

---

## Phase 4 — Realtime

**Build**

- `EventBus` interface, `InProcessEventBus` and `RedisEventBus`.
- `POST /v1/projects/{id}/ws-ticket` (60s, single-use, Redis-backed).
- WebSocket gateway: snapshot, channel subscribe/unsubscribe, deltas with cursors,
  `resume`, `resync_required`, ping/pong, per-connection bounded queue.
- Outbound webhooks: subscriptions, HMAC signing, the dispatcher worker with backoff and
  a dead state, deliveries log, `POST /v1/integrations/{id}/test`.

**Tests**

- Snapshot-then-delta ordering: no entity is ever delivered twice for one change.
- Resume from an old cursor replays exactly the missed deltas.
- Resume beyond the replay window yields `resync_required`.
- A slow consumer is dropped to `resync_required` without unbounded server memory.
- Webhook signature verification against an independent implementation.
- Fan-out with the Redis bus across two API processes reaches both sockets.

**Done when** two `websocat` sessions on different API processes show identical live
deltas, and killing one mid-stream loses nothing on reconnect.

---

## Phase 5 — Web app (deliberately no island)

**Build**

- Generated TypeScript client; fetch wrapper with problem-details parsing.
- Realtime client: ticket refresh, exponential backoff, cursor resume, Zustand store.
- Project dashboard: counters, throughput chart, recent activity.
- Agents list and detail (status, runtime, current task, runs, status history).
- Tasks table with filters, task detail with its event timeline, create and cancel.
- Live activity feed (virtualised) and log viewer with search.
- Settings: API keys, integrations, webhooks and their delivery log.
- Design-system primitives; accessible status colours with non-colour indicators too.

**Tests**

- Store reducer unit tests for every delta frame type.
- Playwright: login → create project → mint key → ingest via API → the agent appears and
  its status changes without a page reload.

**Done when** the product is fully usable and genuinely real-time with no Phaser in the
bundle. Shipping here would be a credible v0.

---

## Phase 6 — The island

Only now, and built against a backend that does not know it exists.

**Build**

- `world/types.ts`: `WorldState`, `WorldEntity`, `AnimationIntent`.
- `world/projector/`: the pure `project(domain, layout)` and the rules table from
  `01-ARCHITECTURE.md` section 6.
- `world_layouts` / `world_placements` migration (`0007`), the world API namespace, and
  deterministic autolayout on agent registration.
- Phaser: isometric tilemap, camera (pan, pinch and wheel zoom, bounds), depth sorting.
- Buildings per `building_kind`; characters per `character_kind` with idle, walk, work,
  alert, slump animations.
- Event-driven animation: `task.started` walks the character to its building,
  `run.step_finished` adds a banner notch, `task.completed` sends a resource token to the
  warehouse, `task.failed` puffs smoke.
- Interaction: click a building for the agent panel, hover for a tooltip, a filter that
  dims agents outside the current label selection.
- Settings → World: drag a building to place it, pick a character, pick a theme, reset to
  autolayout.
- Performance: object pooling for tokens and effects, animation budget, degradation to a
  static island when `prefers-reduced-motion` is set.

**Tests**

- Projector unit tests: every status and every event type maps to the documented visual
  state, with no Phaser imported.
- A determinism test: the same `DomainState` and layout always produce the same
  `WorldState`.
- **The isolation test:** the backend suite and the non-world web suite stay green with
  `world/phaser/` removed from the build.
- Playwright: ingest `task.started` for an agent and assert the scene receives a
  `walk_to` intent for that agent's entity.
- A 50-agent, 20-events/second soak staying above 50 fps on a mid-range laptop.

**Done when** a stranger can watch the island and correctly describe what the agents are
doing — and when every animation on screen traces to a row in `events`.

---

## Phase 7 — SDKs and adapters

**Build**

- `packages/sdk-python`: client, `AgentHandle`, heartbeat thread, buffered flush, retry
  with jitter and `Retry-After`, `run()` context manager, command polling, types
  generated from `packages/protocol`. Never raises into caller control flow.
- `packages/sdk-typescript`: the same surface for Node and the browser.
- Adapters: `generic-rest`, `generic-webhook` (HMAC + JSONPath mapping), `langgraph`,
  `openai-assistants`, `anthropic-sdk`; registry entry points; the `/v1/adapters`
  listing that drives the config form.
- Examples: a LangGraph Python agent, a headless Node queue worker, a bash-and-curl
  agent, and a webhook-only integration.
- Generated protocol docs in `docs/protocol/`.

**Tests**

- SDK parity: the same lifecycle driven through the SDK and through raw HTTP produces
  identical event streams.
- Adapter unit tests from recorded real payloads for each provider.
- Resilience: with the API returning 500 for 60s, the SDK buffers and loses nothing, and
  the monitored agent's own work is unaffected.
- The bash-and-curl example runs in CI, which proves "no SDK required" is true.

**Done when** two agents in different languages using different providers appear side by
side on one island, and neither imported anything that knows what an island is.

---

## Phase 8 — Hardening and launch

**Build**

- Load test: 100 agents, 1,000 events/s sustained; record p50/p95/p99 ingest latency,
  WebSocket fan-out latency and DB growth; publish the numbers in `docs/`.
- Query tuning from the load test's `pg_stat_statements` output.
- Partition automation verified across a month boundary; retention jobs verified.
- Security pass: dependency audit, a secrets scan, CORS review, WebSocket origin check,
  key-rotation runbook, a documented abuse-response path.
- Observability: dashboards for ingest rate, reducer latency, WS connections, webhook
  failures; alerts on ingest error rate and bus lag.
- Operations: deployment manifests, migration runbook, backup and restore **rehearsed**
  (an untested restore is not a backup), incident runbook.
- Docs: quickstart ("monitored in 5 minutes"), protocol reference, adapter authoring
  guide, self-hosting guide, API reference from OpenAPI.
- Staging and production deploys, custom domain, status page.

**Done when** the five-minute quickstart works for someone who has never seen the repo,
and the load-test numbers are published rather than assumed.

---

## What is deliberately deferred

Each of these is a real feature that would dilute v1. Listed so they are decisions, not
oversights.

| Deferred | Why | Earliest |
|----------|-----|----------|
| Alerting rules and notifications (email, Slack, PagerDuty) | Needs a rule engine; webhooks cover the urgent case | v1.1 |
| Cost and token analytics | Generic metric ingest already stores the data; the UI can wait | v1.1 |
| Agent-to-agent coordination | Changes the protocol from observational to transactional | v2 |
| Multi-island / cross-project world view | Scene complexity before the single island is proven | v1.2 |
| Mobile-native app | The web island is responsive enough to start | v2 |
| Self-serve billing | No pricing model yet | pre-GA |
| SSO/SAML, SCIM | Enterprise-gated; Supabase Auth covers it until then | on demand |
| Trace import (OpenTelemetry spans → runs) | Valuable, but it is a whole adapter class of its own | v1.2 |

## Milestone gates

Three points where it is worth stopping to re-read the goals:

1. **End of Phase 2** — is the protocol something you would be happy to version forever?
   Breaking it later costs every integrator.
2. **End of Phase 5** — is the product useful *without* the island? If not, the island is
   compensating for a weak dashboard rather than adding to a strong one.
3. **End of Phase 6** — can the island be deleted without a single backend test changing?
   If not, G8 has already been lost and the fix is cheapest right then.
