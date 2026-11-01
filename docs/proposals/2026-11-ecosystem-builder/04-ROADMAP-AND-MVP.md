# Revised roadmap, MVP and deferrals

> Proposal, 2026-11-01. Deliverables 10 and 11.

## 1. Why the ordering changes

The previous roadmap was **horizontal**: all of the protocol, then all of the control
plane, then all of realtime, then the UI, then the island. For an observability product
that was right — the protocol was the product, and the dashboard was a view of it.

For this product it fails in two ways. Nothing is demonstrable until Phase 6, so the
first time anyone sees whether agents collaborating is any good is after most of the
budget. And the orchestration model — the part most likely to be wrong — is not exercised
until everything is built around it.

So the revision goes **vertical**: foundations that cannot be avoided, then one narrow
thread that actually runs end to end, then widen it.

One ordering principle from the previous roadmap is kept, because it is still correct:

> The protocol and the execution core are built and proved **before** anything visual.
> That is the only ordering under which "the island is a projection" can be verified
> rather than hoped for.

The island therefore remains late (Phase 8), and by then the whole product works through
`curl`. The conversational builder is later still (Phase 9), because it *writes* the domain
model and building it before the model settles means building it twice.

| Phase | Theme | Est. | Demonstrable outcome |
|-------|-------|------|---------------------|
| 0 ✅ | Foundations | done | skeleton, boundaries enforced |
| 1 ✅ | Tenancy and auth | done | sign in, workspace, project, API key |
| 2 | Domain model and protocol | 1.5–2 w | the whole ecosystem exists as data; replay is proved |
| 3 | **The vertical slice** | 2–3 w | **one managed agent runs one step, triggered by hand** |
| 4 | Real workflows | 2–3 w | multi-step, parallel, conditions, retries, timeouts |
| 5 | Approvals, tools, integrations | 2–3 w | a gated Slack post through MCP; credentials encrypted |
| 6 | External agents and triggers | 1.5–2 w | a Python worker outside AgentWorld does a step; 07:00 schedule fires |
| 7 | The workspace | 2.5–3 w | the whole product usable in a browser, no island |
| 8 | The island | 3–4 w | Build Mode creates, Live Mode watches |
| 9 | The conversational builder | 2–3 w | one sentence becomes a working ecosystem |
| 10 | Hardening and launch | 2–3 w | load-tested, secured, documented, deployed |

Roughly 20–26 weeks to v1. **MVP is reached at the end of Phase 9**; Phases 7–9 are what
make it usable by the brief's intended audience.

---

## Phase 2 — Domain model and protocol

The unavoidable foundation. No execution yet; this phase makes the ecosystem *expressible*
and proves the event pipeline honest.

**Build**
- Migrations `0006`–`0010`: agents (with `interaction_mode`), model_profiles, workflows,
  workflow_versions, workflow_steps, workflow_edges, connections, tools, integrations,
  credentials, knowledge_sources, knowledge_documents, triggers, workflow_executions,
  execution_steps, approvals, dispatches, tasks, runs, run_steps, agent_status_history,
  and the partitioned `events` / `logs` / `metric_samples` with the maintenance function.
- Every new tenant table into `RLS_PROTECTED_TABLES`; `events`/`logs` granted
  `SELECT, INSERT` only, as `audit_log` already is.
- `domain/protocol/envelope.py` + `types.py`: `subject`, `correlation`, the full registry.
- Reducers for `agent.*`, `task.*`, `run.*`, `tool.*`, `workflow.*`, `approval.*`.
- `services/ingest.py`: validate → resolve refs → dedupe → append → fold → publish, with
  per-project `seq` allocated inside the transaction.
- `POST /v1/ingest/events` with partial success; `/heartbeat`, `/logs`, `/metrics`.
- CRUD for every definition object, with `authorize()` rows and audit rows.
- `POST /v1/workflows/{id}/validate` and `domain/orchestration/validate.py`.
- `domain/expressions/` — the evaluator, fuzzed.

**Tests** — this is where the budget goes, as before.
- Reducer unit tests, table-driven, one case per event type including stale and
  out-of-order.
- **Replay equivalence**: ingest a recorded stream, snapshot state, truncate current-state
  tables, replay, assert byte-identical.
- **Promise 7**: replay with the dispatch workers running; assert **zero** `dispatches`
  rows are created. The test that makes orchestration safe to add in Phase 3.
- Idempotency: the same batch three times yields one event per `dedup_key`.
- Validation: every finding code has a test producing it, including `observed_agent_assigned`,
  `tool_not_granted`, `cycle` and `unreachable_step`.
- Expression evaluator: no attribute escape, no unbounded recursion, no I/O.
- Tenant isolation extended to every new endpoint — the existing conformance test, widened.
- Schema drift and enum drift against a freshly migrated database.

**Done when** an ecosystem — agents, a workflow, connections, a trigger — can be created
through the API, `validate` explains exactly why an unrunnable one will not run, and replay
reproduces state while causing nothing.

**Affects Phase 0/1:** additively. New enums, ~45 new `Action` members, new tables in
`RLS_PROTECTED_TABLES`, one `ALTER TYPE agent_status ADD VALUE 'paused'`. No existing code
changes behaviour.

---

## Phase 3 — The vertical slice

The most important phase in this roadmap. One managed agent, one single-step workflow, run
by hand, with a real LLM call. Deliberately narrow: no parallelism, no conditions, no
approvals, no tools, no UI beyond what proves it worked.

**Build**
- `infra/providers/`: `base.py` Protocol, `anthropic.py`, registry. One provider.
- `infra/secrets/`: `SecretStore`, envelope encryption, `CREDENTIAL_MASTER_KEY` required
  outside local and test.
- `domain/orchestration/advance.py` + `decisions.py` — the pure decision function.
- `services/executions.py` — apply decisions in one transaction: event + state + outbox.
- `workers/step_runner.py` — claim a `managed_agent_turn`, call the provider, emit the run
  trace, report `workflow.step_succeeded`.
- `workers/dispatch_reaper.py` — leases, backoff, dead-lettering.
- `POST /v1/workflows/{id}/run`, `GET /v1/executions/{id}`.
- The two new import-linter contracts from `03-API-AND-FRONTEND.md` §2, each verified by
  planting a violation.

**Tests**
- `advance()` unit tests with no database: ready-step selection, idempotency under repeated
  calls, terminal detection.
- Outbox: a double `advance` produces one dispatch (constraint violation, not two calls).
- Crash recovery: kill the worker mid-dispatch; the lease expires and the step completes.
- Provider failure: 429 then success — one step, two attempts, one output.
- A recorded execution replayed produces identical state and zero dispatches.

**Done when** `POST /v1/workflows/{id}/run` on a one-step workflow calls Anthropic, writes
a run trace, completes the execution, and `GET /v1/executions/{id}` shows it — all through
`curl`, with no frontend.

**Affects Phase 0/1:** `Settings` gains `CREDENTIAL_MASTER_KEY` with the same fail-fast
validator `API_KEY_PEPPER` has. Nothing else.

---

## Phase 4 — Real workflows

**Build**
- Step kinds: `condition`, `transform`, `wait`; parallel fan-out and join.
- `input_mapping` evaluation; step outputs addressable as `$.steps.<key>.output`.
- Retry with backoff, `max_attempts`, `timeout_seconds`, `on_failure` ∈ fail/continue/route.
- `workers/timeout_sweeper.py`.
- Draft/publish versioning; running executions pinned to their version.
- Cancel, pause, resume, single-step retry.
- `connections` of kind `delegate` — agent-to-agent handoff as a real step assignment.

**Tests**
- A diamond graph (fan-out, two branches, join) completes once, with the join seeing both.
- A condition routing to either branch; the unchosen branch's steps become `skipped`.
- Editing a workflow mid-execution does not change the running copy.
- A step that times out while its agent is still working: the late report is dropped as a
  duplicate, not applied.
- Cancel: pending dispatches cancelled, in-flight work marked cancelled when it reports.
- 20 concurrent executions of the same workflow interleave without cross-contamination.

**Done when** a four-step workflow with a branch and a retry runs to completion, and a
deliberately failed step behaves per each `on_failure` policy.

---

## Phase 5 — Approvals, tools, integrations

**Build**
- `approvals` as a gate: `RequestApproval`, the decide endpoint, `approval.*` events,
  expiry worker. **No dispatch exists until granted** — the structural property.
- `infra/tools/`: `base.py`, `executor.py`, builtins (`http_request`, `web_search`,
  `knowledge_search`), and `mcp_client.py`.
- Tool discovery and refresh from an MCP server; `tools` rows with `source='mcp'`.
- `side_effect` classification; `consequential` defaults to requiring approval.
- OAuth2 flow: start, callback, token storage, refresh; Slack as the first integration.
- Secret redaction: `secret_fields` on tool schemas, enforced before persist and publish.
- Knowledge v1: upload, text extraction, PostgreSQL full-text `knowledge_search`.

**Tests**
- A `consequential` tool with no granted approval produces **no dispatch row** — asserted
  on the table, not on a response.
- Denying an approval fails the step per policy; granting resumes and dispatches once.
- An approval mid-turn parks the dispatch and restores the message history on grant.
- A revoked `grant_tool` connection takes effect on the next call, mid-run.
- **A known secret appears in no `events`, `logs`, `execution_steps` or `dispatches` row** —
  the same shape as the Phase 1 test that searched every text column for a key plaintext.
- OAuth refresh on an expired token, without re-prompting the user.
- MCP: tools discovered, invoked, and a server going away degrades to a clear tool error.

**Done when** a workflow drafts a message, stops for approval, and posts to Slack only
after a human grants it — and the credential is unreadable in every log and every event.

---

## Phase 6 — External agents and triggers

**Build**
- `dispatchable` agents: `agent_push` (signed POST) and `agent_task_offer` (claim/poll,
  `FOR UPDATE SKIP LOCKED`), `GET /v1/self/*`.
- Commands for dispatchable agents: capability-gated, `409` when not advertised.
- `observed` agents: ingest-only, refused for step assignment.
- Triggers: `scheduler` worker, `schedule` with cron and timezone, `webhook` with its token
  plane, `integration_event`, `workflow_completed`.
- `heartbeat_sweeper`: agents past TTL → `offline` with a synthetic status event.
- `packages/sdk-python` — thin: client, heartbeat, buffered flush, retry with jitter,
  `run()` context manager, command polling. Never raises into caller control flow.

**Tests**
- A Python worker outside AgentWorld completes a workflow step, via push and via poll.
- An `observed` agent assigned a step: `422` naming the mode, and `validate` says so first.
- A trigger firing twice in one minute yields one execution (`idempotency_key`).
- A webhook token is rate-limited and can start only its own workflow.
- A bash-and-curl agent completes a step in CI, which proves "no SDK required" is true.
- SDK parity: the same lifecycle through the SDK and through raw HTTP produces identical
  event streams.

**Done when** a scheduled workflow at 07:00 hands a step to a worker running on a laptop
behind a firewall, and the whole run is visible over `curl`.

---

## Phase 7 — The workspace

**Build**
- TypeScript client generated from `/openapi.json`, replacing the hand-written one.
- Realtime client: ticket refresh, backoff, cursor resume, a Zustand store.
- Lists and detail pages for agents, workflows, executions, approvals, integrations,
  knowledge, triggers.
- The workflow editor: a left-to-right DAG canvas with a step inspector and inline
  `validate` findings.
- The execution timeline: steps, inputs, outputs, events, retry.
- The approval queue, decided inline.
- Simple/Advanced on every form, driven by one field-level schema; the copy lint over the
  simple-mode catalogue.
- Guided integration connect, including OAuth.

**Tests**
- Store reducer unit tests for every delta frame type.
- Playwright: create two agents → build a three-step workflow → publish → run → approve →
  see it complete, without a page reload.
- A `publishable: false` workflow cannot be started from the UI, and says what is missing.
- Keyboard and screen-reader pass on the approval queue and the workflow editor.

**Done when** the whole product is usable in a browser with **no island**, and shipping
here would be a credible v0.

---

## Phase 8 — The island

Only now, against a backend that does not know it exists.

**Build**
- `world/types.ts`, `projector/` (`projectBuild`, `projectLive`), `interpreter/`
  (`interpret`), `dispatch/`, `phaser/`, `bridge/`.
- `world_placements` widened to `(project_id, node_kind, node_id)`; migration `0011`;
  deterministic autolayout on creation.
- Phaser: isometric tilemap, camera (pan, pinch, wheel, bounds), depth sorting.
- Buildings and characters per kind; idle, walk, work, alert, slump animations.
- Build Mode: the gesture table from `03-API-AND-FRONTEND.md` §5, broken lines for invalid
  connections, unlit factories for unpublishable workflows.
- Live Mode: the animation table, driven by `recent: Event[]`.
- Interventions from the island: approve, retry, cancel, answer.
- Object pooling, an animation budget, and a static island under
  `prefers-reduced-motion`.

**Tests**
- Projector unit tests: every status and event type maps to the documented visual state,
  with no Phaser imported.
- Determinism: the same state and layout always produce the same `WorldState`.
- `interpret()` unit tests: every gesture produces the documented `Intent`, with no canvas.
- **The isolation test**: the backend suite and the non-world web suite stay green with
  `world/phaser/` removed from the build.
- Playwright: ingest `workflow.step_dispatched` and assert the scene receives a `walk_to`
  for that entity.
- A 50-agent, 20-events/second soak above 50 fps on a mid-range laptop.

**Done when** a stranger can watch the island and correctly describe what the agents are
doing, every animation traces to a row in `events`, and Build Mode creates real objects
without the island being the source of truth.

---

## Phase 9 — The conversational builder

**Build**
- `services/builder.py`: sessions, the propose/apply split, domain schemas as tool
  definitions.
- Plan rendering: as a list, and as a preview on the island.
- Reuse detection over existing agents, tools, integrations and knowledge.
- `requires_consent` for every consequential grant; per-plan object caps; per-session rate
  limits.
- Conversational revision of a plan before apply.
- Apply in one transaction, leaving workflows unpublished and triggers disabled.

**Tests**
- The brief's own sentence produces a plan that passes `validate` and, once published,
  runs.
- A plan is never applied without an explicit `apply`.
- The builder cannot create a credential, widen a grant, enable a trigger or publish.
- A `member` applying an integration-creating plan is refused *at plan time*, with the
  reason, not at apply.
- A prompt-injected document in a knowledge source cannot cause the builder to grant a
  tool — the builder's inputs are data, not instructions.
- 400 agents in one sentence hits the cap and says so.

**Done when** one sentence becomes a working ecosystem the user can inspect, edit, publish
and run — and nothing was activated without them saying so.

---

## Phase 10 — Hardening and launch

**Build**
- Load test: 100 agents, 50 concurrent executions, 1,000 events/s. Publish p50/p95/p99 for
  ingest, decision latency and dispatch pickup, and DB growth.
- Query tuning from `pg_stat_statements`; partition rollover verified across a month
  boundary; retention jobs verified.
- Security pass: dependency audit, secrets scan, credential rotation runbook, CORS and
  WebSocket origin review, expression-evaluator fuzzing, a documented abuse path, **a
  prompt-injection review of every place model output reaches a tool call**.
- Per-key and per-workspace rate limits and execution quotas.
- Observability: dashboards for ingest rate, decision latency, dispatch queue depth, lease
  expiries, approval age, provider errors and spend.
- Operations: deployment manifests, migration runbook, backup and restore **rehearsed**.
- Docs: a five-minute quickstart, the protocol reference, tool and provider authoring
  guides, self-hosting.

**Done when** the quickstart works for someone who has never seen the repo, and the
load-test numbers are published rather than assumed.

---

## 2. Dependencies

```
2 (model + protocol)
└── 3 (vertical slice: provider, orchestrator, outbox)
    ├── 4 (real workflows)
    │   ├── 5 (approvals, tools, integrations)
    │   │   └── 6 (external agents, triggers)
    │   │       └── 7 (workspace)  ──┬── 8 (island)
    │   │                            └── 9 (builder)   ← needs 2–7; 8 only for the preview
    └──────────────────────────────────────────── 10 (hardening)
```

Phases 8 and 9 can run in parallel after 7 — they share no files, as the previous roadmap
observed about its own 5–7. Phase 6 could precede 5 if an external-agent demo is needed
earlier; nothing in 5 depends on it.

## 3. MVP

The brief's MVP list, mapped to where each item lands:

| MVP requirement | Phase | How it is demonstrated |
|---|---|---|
| Authentication, workspace and project management | **1 ✅** | done |
| At least two agents | 3 | two managed agents with different roles |
| Ability to connect agents | 4 | `delegate` connection; researcher → writer |
| One executable multi-step workflow | 4 | four steps, a branch, a retry |
| At least one working integration or tool | 5 | Slack via MCP, plus `web_search` builtin |
| At least one trigger | 6 | weekday 07:00 schedule |
| Human approval for a workflow step | 5 | the gate before posting |
| Live execution status | 7 | the execution timeline, live over WebSocket |
| Interactive island, Build and Live | 8 | the gesture and animation tables |
| Conversational builder for a simple ecosystem | 9 | the brief's own sentence |
| **A real end-to-end demonstration** | **9** | below |

### The demonstration

> It is 07:00 on a Tuesday. A schedule fires. The *Competitor researcher* walks out of its
> workshop, the library lights up as it searches the web, and a crate of findings travels
> the road to the *Brief writer*'s studio. The writer produces an article. The crate stops
> at the town hall and the bell rings: an approval is pending, and nothing has been
> published. Ada opens the queue, reads the draft, and approves it. The gate lifts, the
> crate reaches the Slack portal, and the brief appears in `#market-intel`.
>
> The whole ecosystem was created from one sentence the day before. Every animation above
> corresponds to a row in `events`. Replaying those rows reproduces the final state and
> posts nothing.

That last sentence is the one worth keeping, because it is the difference between this
product and a demo.

## 4. Initial supported scope

The brief asks for a realistic scope rather than everything. In v1:

| | In v1 | Not in v1 |
|---|---|---|
| **Providers** | Anthropic, OpenAI-compatible (one interface) | Gemini, Bedrock, Vertex, local models |
| **Agent modes** | managed, dispatchable (push and poll), observed | per-framework adapters (LangGraph, Assistants, CrewAI) |
| **Workflow features** | sequence, parallel fan-out/join, condition, transform, wait, approval, retry, timeout, `on_failure` | loops, map-over-collection, sub-workflows, dynamic agent selection, compensation |
| **Connections** | `grant_tool`, `grant_knowledge`, `delegate`, `trigger`, `publish` | typed channels, message queues between agents |
| **Tools** | builtins (`http_request`, `web_search`, `knowledge_search`), MCP, integration-provided | a bespoke adapter per SaaS product |
| **Integrations** | Slack, generic REST, generic webhook, MCP servers | Gmail, Drive, Notion, GitHub, Discord, databases |
| **Triggers** | manual, schedule, webhook, integration_event, workflow_completed | email arrival, file watch, DB change capture |
| **Knowledge** | uploaded files, extraction, full-text search | pgvector, folder sync, DB sources, re-ranking |
| **Approvals** | per step, per tool, per connection; role-gated; expiry | multi-approver, quorum, delegation, escalation |
| **Island** | one project, Build and Live | multi-project world, multiplayer cursors, custom assets |

The pattern is one reliable instance of each category rather than many incomplete ones —
the brief's *"Prefer one or two reliable agent execution approaches initially rather than
many incomplete adapters."* Each row's right-hand column is reachable without
architectural change: a provider is a file, an integration is a credential plus discovery,
a workflow feature is a `Decision` variant.

## 5. Deliberately deferred past v1

Each is a real feature, listed so it is a decision rather than an oversight.

| Deferred | Why | Earliest |
|----------|-----|---------|
| Loops and map-over-collection in workflows | Turns an orchestrator into a workflow language; needs iteration state, fan-out limits and partial-failure semantics | v1.1 |
| Sub-workflows | Needs nested cancellation and approval inheritance | v1.1 |
| Per-service integration adapters (Gmail, Drive, Notion, GitHub) | Each is OAuth scopes, API quirks and app review. MCP covers most of the demand for one implementation | v1.1, demand-ordered |
| pgvector knowledge retrieval | Full-text is adequate at first-user document volumes; swapping it is behind the `knowledge_search` tool | v1.1 |
| Cost and token analytics | The data is already in `metric_samples`; this is a UI | v1.1 |
| Multi-approver and quorum approvals | Single approver covers the demo and most real cases | v1.1 |
| Agent and workflow templates, and sharing between workspaces | Needs export/import, versioning and a trust model for shared definitions | v1.2 |
| Multi-project / cross-project island | Scene complexity before one island is proven | v1.2 |
| OpenTelemetry trace import | Valuable, and a whole adapter class of its own | v1.2 |
| Alerting rules and notifications | Needs a rule engine; the attention queue and webhooks cover the urgent case | v1.2 |
| Pending invitations for users who have not signed up | Needs a token, an expiry, an accept flow and a mail path (carried over from Phase 1) | v1.1 |
| Mobile-native app | The web workspace is responsive enough to start | v2 |
| SSO/SAML, SCIM | Enterprise-gated; Supabase Auth covers it until then | on demand |
| Self-serve billing | No pricing model yet | pre-GA |
| Agent-to-agent negotiation beyond `delegate` | Genuine multi-agent protocols need a conversation model, not a handoff | v2 |

**Explicitly *not* deferred**, reversing the previous roadmap: agent-to-agent collaboration
and workflow execution. They were in the old deferred table as v2 on the grounds that they
"change the protocol from observational to transactional". That was correct, and it is now
the product.

## 6. Risks

| Risk | Mitigation |
|------|-----------|
| The orchestration model is wrong | Phase 3 proves it end to end on one step before anything is built on it. `advance()` being pure means changing it is a unit-test exercise |
| Prompt injection via knowledge, tool results or integration payloads reaching a tool call | Tool grants checked at call time; `consequential` tools gated by approval; model output is never an authorization input; a dedicated review in Phase 10 |
| The builder creates something harmful or expensive | Propose/apply split, `requires_consent`, object caps, nothing published or enabled on apply, bounded by the user's own role |
| Beginners still find it too complex | Simple mode with a lint on its vocabulary, safe-by-default approvals, and Phase 7 shipping before the island so the plain UI is tested first |
| The island becomes the source of truth | `interpret()` is pure and cannot fetch; `world/dispatch/` is the only layer permitted to call the API; the isolation test keeps the product working without Phaser |
| Provider lock-in creeps in | Two import-linter contracts, each verified by planting a violation |
| Secrets leak into events or logs | Entities with no field for them, declared `secret_fields`, and a test that searches every table for a known secret |
| Scope expands until nothing ships | §4's right-hand column, and the rule that each category gets one reliable instance in v1 |
