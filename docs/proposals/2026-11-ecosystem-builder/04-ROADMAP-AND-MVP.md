# Revised roadmap, milestones, MVP and deferrals

> Proposal, 2026-11-01, revised after the refinement brief. Deliverables 10 and 11 of the
> original brief; deliverables 1–8 of the refinement.

## 1. What changed in this revision, and what it costs

The first version of this roadmap was **sequential**: foundations, then a thin execution
slice, then widen, with the island at Phase 8 and the conversational builder at Phase 9.
Its defence was that both are *writers* or *projections* of the domain model, and building
either before the model settles means building it twice.

That reasoning holds, and it is not the whole picture. Both are also the product's
differentiators, and a plan where nobody touches either until week 20 validates the
backend while leaving the actual user experience untested. The refinement brief is right
about that.

So the roadmap now runs **three tracks in parallel**, with early prototypes of both
differentiators:

| Track | What | Where it appears |
|-------|------|------------------|
| **A — Foundation** | model, protocol, orchestration, execution, security | Phases 2–7, 11 |
| **B — Visual** | workspace pages and the island | island I1 at Phase 3, I2 at Phase 4, full at 8–9 |
| **C — Builder** | conversational ecosystem creation | prototype at Phase 4, complete at Phase 10 |

### The trade-off, stated plainly

**Cost: roughly four weeks.** 24–30 weeks to the MVP, against 20–26 for the sequential
plan. The extra is real and breaks down as:

| Source | Cost | Why it is not avoidable |
|--------|------|------------------------|
| The island is touched twice | +1.5 w | The prototype is built on the final `projector` / `interpreter` / `dispatch` split, so none of it is thrown away — but Phase 9 still needs most of its budget for animation, interventions and performance |
| The builder is touched twice | +1 w | Phase 10 adds capabilities to an enum and tool definitions to a prompt. The plan schema, the propose/apply split and the permission model are final from Phase 4, so the rework is narrow but not zero |
| Minimal web pages needed early | +1 w | The island at I1 opens the real agent form, so a Simple-mode agent form exists in Phase 3 instead of Phase 8. Phase 8 then refines rather than creates |
| Parallel-track context switching | +0.5 w | One developer alternating between Postgres and Phaser in a week pays a real tax |

**Gain: a demonstrable product at about week 8 instead of week 20.** The user-facing
interaction model is validated before fifteen more weeks are spent behind it, and the
builder's assumptions about the domain model are tested while the model is still cheap to
change. Three specific risks move from "discovered late" to "discovered early":

- whether a beginner can understand an ecosystem spatially at all,
- whether the domain model is shaped the way a natural-language request decomposes,
- whether the island and the plain pages can stay in sync as two views of one model.

Two things were considered and **rejected**:

- **Starting the island before the domain model (Phase 2).** There would be nothing real to
  render, so it would be built against a mock and then rebuilt. The prototype is worth
  shipping early precisely *because* it renders real backend objects; a mocked island
  validates nothing.
- **Pulling the complete builder forward.** It writes workflows, integrations, triggers,
  knowledge and grants. Those do not exist until Phase 7, so it would be written against a
  model that changes under it — which is the original objection, still correct.

One ordering principle from the first version survives unchanged, because the refinement
brief does not contradict it and it is what keeps the island honest:

> The island renders **real backend objects from the first commit of it**. There is no
> phase in which it has its own state, its own mock, or its own source of truth.

## 2. Phases

| Phase | Theme | Track | Est. | Milestone |
|-------|-------|-------|------|-----------|
| 0 ✅ | Foundations | A | done | |
| 1 ✅ | Tenancy and auth | A | done | **M0** |
| 2 | Core model and protocol | A | 2–2.5 w | |
| 3 | Execution core + island skeleton | A, B | 3 w | |
| 4 | **The vertical slice** | A, B, C | 3 w | **M1** |
| 5 | Real workflows | A | 2.5 w | |
| 6 | Approvals, tools, integrations | A | 3 w | **M2** |
| 7 | External agents and triggers | A | 2 w | **M3** |
| 8 | Full workspace | B | 2.5–3 w | |
| 9 | Full island | B | 3 w | **M4** |
| 10 | Complete builder | C | 2.5 w | **M5 — MVP** |
| 11 | Hardening and launch | — | 2.5 w | **M6** |

---

### Phase 2 — Core model and protocol  *(Track A, 2–2.5 w)*

Makes the ecosystem expressible and proves the event pipeline honest. No execution yet.

**Build**
- Migrations `0006`–`0010`: `credentials`, `model_profiles`, `agents`, `connections`,
  `workflows`, `workflow_versions`, `workflow_steps`, `workflow_edges`,
  `workflow_executions`, `execution_steps`, `dispatches`, `tasks`, `runs`, `run_steps`,
  `agent_status_history`, partitioned `events` / `logs` / `metric_samples`, and the four
  presentation tables.
  *Deferred to their own phases:* `tools`, `integrations`, `knowledge_*`, `triggers`,
  `approvals`.
- Every new tenant table into `RLS_PROTECTED_TABLES`; `events` and `logs` granted
  `SELECT, INSERT` only, as `audit_log` already is.
- `domain/protocol/envelope.py` + `types.py`: `subject`, `correlation`, the full registry.
- Reducers for `agent.*`, `task.*`, `run.*`, `tool.*`, `workflow.*`.
- `services/ingest.py`: validate → resolve refs → dedupe → append → fold → publish, with
  per-project `seq` inside the transaction.
- `POST /v1/ingest/events`, `/heartbeat`, `/logs`, `/metrics`.
- Definition CRUD: agents, model profiles, connections, workflows and drafts.
- `domain/orchestration/validate.py` and `POST /v1/workflows/{id}/validate`.
- `domain/expressions/` — the evaluator, fuzzed.
- The permission matrix rows for everything above (`05-PERMISSIONS.md`).

**Acceptance criteria**
- [ ] An ecosystem — two agents, a connection, a draft workflow — is created through the
      API, and `GET` returns it.
- [ ] `validate` produces every documented finding code, each with a test, including
      `observed_agent_assigned`, `tool_not_granted`, `cycle` and `unreachable_step`.
- [ ] **Replay equivalence:** ingest a recorded stream, snapshot state, truncate
      current-state tables, replay, assert byte-identical.
- [ ] **Protocol promise 7:** the same replay with dispatch workers running creates
      **zero** `dispatches` rows.
- [ ] The same batch ingested three times yields one event per `dedup_key`.
- [ ] Reducer table-driven tests pass for every event type, including stale and
      out-of-order delivery.
- [ ] The expression evaluator resists attribute escape, unbounded recursion and I/O.
- [ ] Tenant isolation: every new endpoint returns 404 for a foreign id, and the owner
      succeeds on the same call. The coverage guard fails if a route is missing from the
      table.
- [ ] Schema and enum drift tests pass against a freshly migrated database.
- [ ] The presentation tables have no execution-state columns (the forbidden-name test).

**Affects Phase 0/1:** additively. New enums, new `Action` rows, new tables in
`RLS_PROTECTED_TABLES`, one `ALTER TYPE agent_status ADD VALUE 'paused'`. No existing
behaviour changes.

---

### Phase 3 — Execution core + island skeleton  *(Tracks A and B, 3 w)*

The first phase where something runs, and the first where something is visible.

**Build — Track A**
- `infra/secrets/`: `SecretStore`, envelope encryption, AAD binding, KEK map and rotation;
  `Settings` fails fast without `CREDENTIAL_KEKS` outside local and test.
- `infra/providers/`: `base.py` Protocol, `anthropic.py`, registry. **The
  OpenAI-compatible implementation is the last task of this phase**, written specifically
  to prove the Protocol is not shaped around one vendor while the first one is still fresh.
- `domain/orchestration/advance.py` + `decisions.py` — the pure decision function.
- `services/executions.py` — apply decisions in one transaction: event + state + outbox.
- `workers/step_runner.py`, `workers/dispatch_reaper.py`.
- `POST /v1/workflows/{id}/run`, `GET /v1/executions/{id}`.
- `infra/event_bus.py` + the WebSocket gateway: snapshot, deltas, cursors, `resume`,
  `resync_required`.
- The two new import-linter contracts, each verified by planting a violation.

**Build — Track B: island milestone I1**
- `world/types.ts`, `projector/projectBuild` + `projectLive`, `interpreter/`, `dispatch/`,
  `phaser/`, `bridge/` — the final architecture, minimal content.
- Static isometric grid, one sprite per `building_kind`, agents placed from `world_nodes`.
- Click to select → inspector panel → **the real agent form in Simple mode**.
- Status rendered from live WebSocket deltas.
- Build / Live toggle, distinct from the start: Build allows select; Live is read-only.
- Minimal pages: agent list and form, execution detail.

**Acceptance criteria**
- [ ] `POST /v1/workflows/{id}/run` on a one-step workflow calls a real provider, writes a
      run trace, and completes — through `curl`, with no frontend.
- [ ] `advance()` unit tests pass with no database and no network; called twice on identical
      state it returns identical decisions.
- [ ] A double `advance` produces **one** dispatch; the loser hits the unique constraint.
- [ ] A worker killed mid-dispatch: the lease expires and the step completes exactly once.
- [ ] A 429 then success: one step, two attempts, one output.
- [ ] A credential row copied into another workspace at the SQL level cannot be decrypted.
- [ ] KEK rotation re-wraps every DEK with no plaintext access and no `ciphertext` change.
- [ ] **The island displays real agents** from the backend, with their real statuses, and
      opens the real configuration form. No mock data path exists in the codebase.
- [ ] An agent going `offline` stays visible and renders shuttered.
- [ ] Deleting `world/phaser/` leaves the backend suite and the non-world web suite green.
- [ ] Planting `import anthropic` in `services/` fails the new import contract.

---

### Phase 4 — The vertical slice  *(Tracks A, B and C, 3 w)* → **M1**

The milestone the refinement brief asks for. Everything here is narrow in each dimension
and complete end to end.

**Build — Track A**
- Multi-step sequential execution: step output → next step input via `input_mapping`.
- A `delegate` connection compiles to a two-step `workflow_version`. **A "team" is not a
  new entity and not a second execution path** — one orchestrator, always.
- `agent.test`, `execution.cancel`.

**Build — Track B: island milestone I2**
- Add objects: drag from a palette → `CreateAgent` → the ordinary `POST`.
- Move objects: drag → `MovePlacement` → `PUT …/world/nodes/…`.
- Connections drawn from `connections`, routed per `world_routes`; create by dragging
  building to building.
- Live Mode: the executing step's building lit, its name on the banner.
- Broken connections drawn dashed and amber with the `validate` finding as the tooltip.

**Build — Track C: builder prototype**
- `services/builder.py` with the final plan schema and propose/apply split.
- `BuilderCapability` limited to `CREATE_AGENT`, `CREATE_CONNECTION`, `CREATE_TEAM`.
- Plan rendered as a list and previewed on the island.
- Apply in one transaction, leaving workflows unpublished.
- Per-plan object caps, per-session rate limits, `builder.apply` bounded by the caller's
  own permissions and refused **at plan time**.

**M1 — the demonstration**

> A user types *"I want a researcher that finds competitor news and a writer that turns it
> into a short brief."* AgentWorld proposes two agents and the connection between them,
> explains what it will create, and creates nothing yet. The user confirms. Two buildings
> appear on the island with a road between them. The user drags one to a better spot and
> opens it to read its instructions. They press Run. The researcher's building lights up,
> its status goes to `working`, the road carries the handoff, the writer runs, and the
> result is on screen — with every status change arriving over the WebSocket from a real
> folded event.

**Acceptance criteria**
- [ ] The sentence above produces a plan containing exactly two agents and one connection.
- [ ] Nothing is created until `apply`; rejecting the plan leaves the project empty.
- [ ] Applying creates the objects in **one transaction**; an induced failure mid-apply
      leaves nothing behind.
- [ ] The applied objects appear on the island and in the agents list without a reload.
- [ ] The two-step execution completes, the second agent's input is the first's output, and
      both status transitions are visible live.
- [ ] A "team" execution runs on the same `advance()` as a hand-built workflow — asserted
      by there being one orchestrator entry point in the codebase.
- [ ] Moving a building produces **no** row in `events` and **no** row in `audit_log`.
- [ ] A `viewer` can watch the island and cannot move anything.
- [ ] The builder asked for a workflow with branches explains that it cannot do that yet
      and what to do instead — it does not attempt it.
- [ ] A `member` whose plan would require an admin action is told at plan time, naming the
      action.
- [ ] The whole demo is reproducible from a script in `docs/demo/`.

---

### Phase 5 — Real workflows  *(Track A, 2.5 w)*

**Build**
- Step kinds `condition`, `transform`, `wait`; parallel fan-out and join.
- Retry with jittered backoff, `max_attempts`, `timeout_seconds`, `on_failure`
  ∈ fail/continue/route.
- `workers/timeout_sweeper.py`; the full dispatch state machine including `dead` and
  `needs_review`.
- Draft/publish versioning; running executions pinned to their version.
- Cancel, pause, resume, single-step retry.

**Acceptance criteria**
- [ ] A diamond graph completes once, and the join sees both branches.
- [ ] A condition routes to one branch; the other branch's steps become `skipped`.
- [ ] Editing a workflow mid-execution does not change the running copy.
- [ ] A step that times out while its agent is still working: the late report is dropped as
      a duplicate.
- [ ] A 422 goes to `dead` on the first attempt, with no second request made.
- [ ] 100 simulated retries do not share a wake-up instant (jitter).
- [ ] Every `dead` dispatch has a corresponding `workflow.step_failed` event.
- [ ] **No-stuck-executions invariant:** after a randomised fault-injection run — killed
      workers, provider errors, expired leases, cancellations — no execution remains in a
      live state with no live dispatch, no pending approval and no terminal step. Asserted
      by a query.
- [ ] A worker killed mid-call on a `consequential` dispatch leaves it `needs_review`, and
      **no worker or timer retries it**.
- [ ] 20 concurrent executions of one workflow do not cross-contaminate.

---

### Phase 6 — Approvals, tools, integrations  *(Track A, 3 w)* → **M2**

**Build**
- `approvals` table and the gate: `RequestApproval`, the decide endpoint, `approval.*`
  events, the expiry worker, `required_role` per approval.
- `infra/tools/`: `base.py`, `executor.py`, builtins (`http_request`, `web_search`,
  `knowledge_search`), `mcp_client.py`.
- MCP tool discovery and refresh; `tools` rows with `source='mcp'`.
- `side_effect` classification; `consequential` defaults to requiring approval.
- OAuth2: start, callback, token storage, refresh. **Slack** as the first integration.
- Secret redaction: the `Secret` wrapper, the structlog scrubber, `secret_fields` on tool
  schemas, sanitised errors, allow-listed metric labels.
- Knowledge v1: upload, extraction, PostgreSQL full-text `knowledge_search`.
- `connection.create` elevation to admin for `consequential` grants.

**M2 — a gated external action.** A workflow drafts a message, stops, and posts to Slack
only after a human grants it.

**Acceptance criteria**
- [ ] A `consequential` tool with no granted approval produces **no dispatch row** —
      asserted against the table, not a response.
- [ ] Denying fails the step per policy; granting resumes and dispatches **once**.
- [ ] An approval mid-turn parks the dispatch and restores the message history on grant.
- [ ] A revoked `grant_tool` connection takes effect on the next call, mid-run.
- [ ] **The canary test:** a credential whose secret is a unique string is used by a
      failing-then-succeeding workflow. The string appears in no table row, no log line, no
      `events` payload, no WebSocket frame, no problem document and no metric label.
- [ ] A revoked credential cannot be decrypted; the failing step names `credential_revoked`.
- [ ] A referenced credential cannot be deleted; the `409` lists the referents.
- [ ] A deleted credential's row holds no key material, and audit rows still resolve.
- [ ] No schema in the OpenAPI document reachable from a credentials response has a
      secret-shaped property.
- [ ] OAuth refresh happens on an expired token without re-prompting.
- [ ] An MCP server disappearing degrades to a clear tool error, not a hung step.
- [ ] A `member` cannot grant an agent a `consequential` tool.

---

### Phase 7 — External agents and triggers  *(Track A, 2 w)* → **M3**

**Build**
- `dispatchable` agents: `agent_push` (signed POST, with the SSRF deny-list enforced at
  request time) and `agent_task_offer` (claim/poll with `FOR UPDATE SKIP LOCKED`).
- `GET /v1/self/*`; capability-gated commands; `observed` agents refused for step
  assignment.
- `triggers` table, the `scheduler` worker, cron with timezone, the webhook token plane,
  `integration_event`, `workflow_completed`.
- `heartbeat_sweeper`.
- `packages/sdk-python` — thin, never raising into caller control flow, forwarding
  `X-AgentWorld-Idempotency-Key`.

**M3 — a credible private beta.** Scheduled, collaborative, gated, with external workers.

**Acceptance criteria**
- [ ] A Python worker outside AgentWorld completes a step, via push and via poll.
- [ ] An `observed` agent assigned a step: `422` naming the mode, and `validate` says so
      first.
- [ ] A trigger firing twice in a minute yields one execution (`idempotency_key`).
- [ ] A webhook token is rate-limited and can start only its own workflow.
- [ ] A bash-and-curl agent completes a step in CI — "no SDK required" is true.
- [ ] SDK and raw HTTP produce identical event streams for the same lifecycle.
- [ ] `agent.configure` pointing `dispatch.url` at a loopback, link-local or private
      address is refused at save time **and** at request time.
- [ ] A dispatchable agent that never reports is resolved by the step timeout.

---

### Phase 8 — Full workspace  *(Track B, 2.5–3 w)*

**Build**
- TypeScript client generated from `/openapi.json`, replacing the hand-written one.
- Lists and detail pages for every object; the DAG workflow editor with inline `validate`
  findings; the execution timeline; the approval queue.
- Simple/Advanced on every form from one field-level schema; the copy lint over the
  Simple-mode string catalogue.
- Guided integration connect, including OAuth.

**Acceptance criteria**
- [ ] Playwright: create two agents → build a three-step workflow → publish → run →
      approve → see it complete, with no reload.
- [ ] A `publishable: false` workflow cannot be started from the UI and says what is
      missing.
- [ ] Every object is fully manageable **without opening the island**.
- [ ] Simple mode writes no value Advanced cannot display.
- [ ] The Simple-mode catalogue contains none of: LLM, token, temperature, prompt, schema,
      webhook.
- [ ] Keyboard and screen-reader pass on the approval queue and the workflow editor.

---

### Phase 9 — Full island  *(Track B, 3 w)* → **M4**

Extends I2; replaces nothing.

**Build**
- The animation table from `03-API-AND-FRONTEND.md` §5, driven by `recent: Event[]`.
- Interventions from the island: approve, retry, cancel, answer.
- Districts, route editing, themes, landmark placement.
- Object pooling, an animation budget, a static island under `prefers-reduced-motion`.

**Acceptance criteria**
- [ ] Projector unit tests: every status and every event type maps to the documented visual
      state, with no Phaser imported.
- [ ] Determinism: identical state and layout always produce an identical `WorldState`.
- [ ] `interpret()` tests: every gesture produces the documented `Intent`, with no canvas.
- [ ] **The isolation test:** backend and non-world web suites stay green with
      `world/phaser/` removed.
- [ ] Playwright: ingesting `workflow.step_dispatched` makes the scene receive a `walk_to`
      for that entity.
- [ ] 50 agents at 20 events/second stays above 50 fps on a mid-range laptop.
- [ ] Every animation on screen traces to a row in `events` — audited by a test that
      asserts the projector has no timer and no random source.
- [ ] A stranger watching the island can describe what the agents are doing.

---

### Phase 10 — Complete builder  *(Track C, 2.5 w)* → **M5, the MVP**

**Build**
- Enable the remaining `BuilderCapability` members: workflows, triggers, integrations,
  knowledge, tool grants, modifying what exists.
- Reuse detection across existing agents, tools, integrations and knowledge, with the
  reason shown.
- `requires_consent` for every consequential grant.
- Conversational revision of a plan before apply.

**Acceptance criteria**
- [ ] The refinement brief's own sentence — research, hand off, approve, publish — produces
      a plan that passes `validate` and, once published, runs.
- [ ] The builder cannot create a credential, widen an existing grant, enable a trigger or
      publish a workflow.
- [ ] A prompt-injected document in a knowledge source cannot make the builder grant a
      tool: its inputs are data, not instructions.
- [ ] 400 agents in one sentence hits the cap and says so.
- [ ] Modifying an existing ecosystem conversationally produces a plan that shows a diff,
      not a replacement.
- [ ] Nothing in Phase 10 changed the plan schema, the apply transaction or the permission
      model — asserted by the Phase 4 builder tests still passing unmodified.

---

### Phase 11 — Hardening and launch  *(2.5 w)* → **M6**

**Build**
- Load test: 100 agents, 50 concurrent executions, 1,000 events/s. Publish p50/p95/p99 for
  ingest, decision latency and dispatch pickup, plus DB growth.
- Query tuning from `pg_stat_statements`; partition rollover across a month boundary;
  retention jobs.
- Security pass: dependency audit, secrets scan, credential rotation runbook, CORS and
  WebSocket origin review, expression-evaluator fuzzing, SSRF deny-list review, and **a
  prompt-injection review of every path where model output reaches a tool call**.
- Per-key and per-workspace rate limits and execution quotas.
- Observability: the signals in `02-ORCHESTRATION.md` §8, with their alert thresholds.
- Operations: deployment manifests, migration runbook, backup and restore **rehearsed**.
- Docs: five-minute quickstart, protocol reference, tool and provider authoring guides,
  self-hosting.

**Acceptance criteria**
- [ ] The quickstart works for someone who has never seen the repo.
- [ ] Load-test numbers are published, not assumed.
- [ ] Backup and restore has been performed, not just configured.
- [ ] Every alert in the observability table fires in a drill.

## 3. Dependencies between the tracks

```
        Track A (foundation)          Track B (visual)         Track C (builder)
        ────────────────────          ────────────────         ─────────────────
P2   core model + protocol ─────┬──────────┐
                                │          │
P3   execution core + WS ───────┼───► island I1 (renders real agents)
                                │          │
P4   multi-step + delegate ─────┴───► island I2 ◄──── builder prototype ──► M1
                                              ▲              │
P5   real workflows                           │              │ (plan preview
                                              │              │  uses I2)
P6   approvals, tools, Slack ──► M2           │              │
                                              │              │
P7   external agents, triggers ──► M3         │              │
          │                                   │              │
P8        └──────────────► full workspace ────┘              │
                                   │                         │
P9                                 └──► full island ──► M4   │
                                                 │           │
P10                                              └──► complete builder ──► M5 = MVP
                                                                  │
P11                                               hardening ──────┴──► M6
```

Hard dependencies, the ones that cannot be reordered:

| Dependency | Why |
|------------|-----|
| B needs A's model and WebSocket | the island renders real objects from the first commit; there is no mock path |
| C needs A's definition CRUD and `validate` | the builder is a client of the ordinary services and must not propose what cannot run |
| C's preview needs B's I2 | a plan shown as a diagram needs a diagram to show it on; without I2 the prototype falls back to the list view, which is a usable degradation |
| Phase 8 before Phase 9 | the plain workspace must be proved usable before the island gets its polish, or the island becomes load-bearing |
| Phase 10 after Phase 7 | the complete builder writes triggers and integrations, which do not exist until then |
| Phase 6 before Phase 7 is **soft** | nothing in approvals or tools depends on external agents. Swap them if an external-agent demo is needed sooner |

## 4. Milestones

| | End of | What it proves | Shippable as |
|---|--------|----------------|--------------|
| **M0** | Phase 1 ✅ | multi-tenant, authenticated, isolated | nothing user-facing |
| **M1** | Phase 4 | **the product concept works end to end** | an internal demo |
| **M2** | Phase 6 | consequential actions are really gated | a design-partner preview |
| **M3** | Phase 7 | external agents and unattended schedules | a credible private beta |
| **M4** | Phase 9 | the island is a real workspace | a public beta |
| **M5** | Phase 10 | one sentence builds a working ecosystem | **the MVP / v1** |
| **M6** | Phase 11 | it survives load, failure and an operator | general availability |

M1 is where the plan stops being a bet. If the demo at M1 is unconvincing, the cheapest
moment to change direction is there — about eight weeks in, with the domain model written
and almost nothing built on top of it.

## 5. Revised MVP

The MVP is **M5**, at the end of Phase 10.

| MVP requirement | Lands | Demonstrated by |
|---|---|---|
| Authentication, workspace and project management | **1 ✅** | done |
| At least two agents | 3 | two managed agents with distinct roles |
| Ability to connect agents | 4 | a `delegate` connection, compiled to a real workflow |
| One executable multi-step workflow | 5 | four steps, a branch, a retry |
| At least one working integration or tool | 6 | Slack via MCP, plus the `web_search` builtin |
| At least one trigger | 7 | a weekday 07:00 schedule |
| Human approval for a workflow step | 6 | the gate before posting |
| Live execution status | 3 (API), 8 (UI) | the execution timeline, live over WebSocket |
| Interactive island, Build and Live | 4 (prototype), 9 (full) | the gesture and animation tables |
| Conversational builder | 4 (prototype), 10 (full) | the refinement brief's own sentence |
| **A real end-to-end demonstration** | **4 (M1), 10 (full)** | below |

### The v1 demonstration

> It is 07:00 on a Tuesday. A schedule fires. The *Competitor researcher* walks out of its
> workshop, the library lights up as it searches, and a crate of findings travels the road
> to the *Brief writer*'s studio. The writer produces an article. The crate stops at the
> town hall and the bell rings: an approval is pending and nothing has been published. Ada
> opens the queue, reads the draft, approves it. The gate lifts, the crate reaches the Slack
> portal, and the brief appears in `#market-intel`.
>
> The whole ecosystem was created from one sentence the day before. Every animation above
> corresponds to a row in `events`. Replaying those rows reproduces the final state and
> posts nothing.

The abbreviated version of this — two agents, one handoff, no approval, no schedule, no
animation — is **M1 at week eight**.

## 6. Timeline and scope assumptions

The estimate is 24–30 weeks to M5, and it assumes:

| Assumption | If it breaks |
|-----------|--------------|
| One developer, effectively full time | the range scales close to linearly; the parallel tracks do not shorten with one person, they only reorder risk |
| Anthropic plus one OpenAI-compatible provider, nothing else | each additional provider is ~2 days once the Protocol is proved |
| Slack, generic REST/webhook and MCP; no other bespoke integration | each OAuth integration is 3–5 days plus any app-review wait, which is calendar time rather than work |
| Supabase Auth stays the identity provider | swapping it is ~1 week behind the existing `AuthProvider` interface |
| One PostgreSQL, no broker, no Kubernetes before Phase 11 | introducing any of them early adds a week and an operational surface |
| Phaser for the island | the prototype at I1 is the point at which that choice is cheap to revisit |
| Art is placeholder until Phase 9 | commissioned art is calendar time, not developer time; start it at Phase 6 if it is wanted for M4 |
| No compliance or certification work in v1 | SOC 2 or similar is its own project |

**The honest lever on the timeline is §7's scope table, not the phase list.** Compressing
phases produces the same work with less testing, and the two things this product cannot
afford to ship untested are the approval gate and tenant isolation.

## 7. Initial supported scope

| | In v1 | Not in v1 |
|---|---|---|
| **Providers** | Anthropic, OpenAI-compatible | Gemini, Bedrock, Vertex, local models |
| **Agent modes** | managed, dispatchable (push and poll), observed | per-framework adapters (LangGraph, Assistants, CrewAI) |
| **Workflow features** | sequence, parallel fan-out/join, condition, transform, wait, approval, retry, timeout, `on_failure` | loops, map-over-collection, sub-workflows, dynamic agent selection, compensation |
| **Connections** | `grant_tool`, `grant_knowledge`, `delegate`, `trigger`, `publish` | typed channels, queues between agents |
| **Tools** | builtins, MCP, integration-provided | a bespoke adapter per SaaS product |
| **Integrations** | Slack, generic REST, generic webhook, MCP servers | Gmail, Drive, Notion, GitHub, Discord, databases |
| **Triggers** | manual, schedule, webhook, integration_event, workflow_completed | email arrival, file watch, DB change capture |
| **Knowledge** | uploaded files, extraction, full-text search | pgvector, folder sync, DB sources, re-ranking |
| **Approvals** | per step, per tool, per connection; role-gated; expiry | multi-approver, quorum, delegation, escalation, four-eyes |
| **Builder** | agents, connections, teams (P4); workflows, triggers, integrations, knowledge, grants, modification (P10) | debugging an existing ecosystem conversationally, cost estimation, A/B proposals |
| **Island** | one project, Build and Live, districts | multi-project world, multiplayer cursors, custom assets |

One reliable instance of each category rather than many incomplete ones. Each right-hand
cell is reachable without architectural change: a provider is a file, an integration is a
credential plus discovery, a workflow feature is a `Decision` variant, a builder capability
is an enum member.

## 8. Security and reliability requirements, by phase

Consolidated from `01-DOMAIN-AND-PROTOCOL.md` §6 and §10, `02-ORCHESTRATION.md` §8, and
`05-PERMISSIONS.md`. Each is a gate on the phase it lands in, not a Phase 11 cleanup.

| Requirement | Phase | Specified in |
|-------------|-------|--------------|
| Every new tenant table under RLS, with the non-bypassing-role guard | 2 | 00 §3 K7 |
| Append-only enforced by privilege on `events` and `logs` | 2 | 01 §1 |
| Permission matrix total over actions; route coverage guard | 2 | 05 §6 |
| Tenant isolation 404 on every new endpoint, with a coverage guard | 2 | 05 §6 |
| Presentation tables hold no execution state | 2 | 01 §10 |
| Replay causes no work (protocol promise 7) | 2 | 01 §9 |
| Expression evaluator cannot escape its scope | 2 | 02 §4 |
| Envelope encryption with AAD tenant binding | 3 | 01 §6 |
| KEK rotation without plaintext access | 3 | 01 §6 |
| Fail-fast settings for `CREDENTIAL_KEKS` | 3 | 01 §6 |
| Provider SDKs confined to `infra/providers` | 3 | 03 §2 |
| Orchestration imports no I/O | 3 | 03 §2 |
| Idempotent dispatch, three layers | 3 | 02 §3 |
| Lease-based crash recovery | 3 | 02 §8 |
| Retry classification: terminal errors not retried | 5 | 02 §8 |
| Dead letters always resolve their step | 5 | 02 §8 |
| No-stuck-executions invariant under fault injection | 5 | 02 §8 |
| `needs_review` for ambiguous consequential work | 5 | 02 §8 |
| Approval gate is the absence of a dispatch | 6 | 02 §7 |
| Secret redaction in logs, events, errors, telemetry | 6 | 01 §6 |
| The canary test | 6 | 01 §6 |
| Revocation and deletion behaviour | 6 | 01 §6 |
| No secret-shaped property in the OpenAPI document | 6 | 01 §6 |
| Consequential grants require admin | 6 | 05 §4 |
| SSRF deny-list on `dispatch.url`, enforced at request time | 7 | 05 §5 |
| Webhook token rate limits | 7 | 03 §1 |
| Builder bounded by the caller's permissions, at plan time | 4 | 05 §4 |
| Builder inputs are data, not instructions | 10 | 04 §P10 |
| Prompt-injection review of model-output-to-tool-call paths | 11 | 04 §P11 |
| Load, quota and rate-limit verification | 11 | 04 §P11 |
| Rehearsed backup and restore | 11 | 04 §P11 |

## 9. Deliberately deferred past v1

| Deferred | Why | Earliest |
|----------|-----|---------|
| Loops and map-over-collection | Turns an orchestrator into a workflow language: iteration state, fan-out limits, partial-failure semantics | v1.1 |
| Sub-workflows | Nested cancellation and approval inheritance | v1.1 |
| Four-eyes and multi-approver approvals | A single approver covers the demo and most real cases. Note that v1 **permits self-approval**: the author of a workflow can approve its steps | v1.1 |
| Per-service integration adapters (Gmail, Drive, Notion, GitHub) | Each is OAuth scopes, API quirks and app review; MCP covers most demand for one implementation | v1.1, demand-ordered |
| pgvector knowledge retrieval | Full-text is adequate at first-user volumes, and swapping it is behind the `knowledge_search` tool | v1.1 |
| Cost and token analytics | The data is already in `metric_samples`; this is a UI | v1.1 |
| Pending invitations for users who have not signed up | Needs a token, expiry, accept flow and a mail path (carried from Phase 1) | v1.1 |
| Conversational debugging of a running ecosystem | Needs the builder to read execution state, which is a different and larger tool surface | v1.2 |
| Agent and workflow templates, shared between workspaces | Export/import, versioning and a trust model for shared definitions | v1.2 |
| Multi-project island | Scene complexity before one island is proven | v1.2 |
| OpenTelemetry trace import | Valuable, and a whole adapter class of its own | v1.2 |
| Alerting rules and notifications | Needs a rule engine; the attention queue and webhooks cover the urgent case | v1.2 |
| Mobile-native app | The web workspace is responsive enough to start | v2 |
| SSO/SAML, SCIM | Enterprise-gated; Supabase Auth covers it until then | on demand |
| Self-serve billing | No pricing model yet | pre-GA |
| Agent-to-agent negotiation beyond `delegate` | Real multi-agent protocols need a conversation model, not a handoff | v2 |

**Not deferred**, reversing the original roadmap: agent-to-agent collaboration and workflow
execution. They were v2 on the grounds that they "change the protocol from observational to
transactional". That was correct, and it is now the product.

## 10. Risks

| Risk | Mitigation |
|------|-----------|
| The orchestration model is wrong | Phase 3 proves it on one step before anything is built on it; `advance()` being pure makes changing it a unit-test exercise |
| **The product concept does not land** | M1 at week eight, with the domain model written and almost nothing on top of it. This is the risk the revision exists to address |
| The early prototypes become throwaway work | Both are built on the final architecture: the island on the real projector/interpreter/dispatch split, the builder on the final plan schema with a capability enum. Phase 10's acceptance criteria include the Phase 4 builder tests passing unmodified |
| Parallel tracks fragment attention | Phases 3 and 4 are the only ones with more than one track, and each has a single milestone to converge on |
| Prompt injection reaching a tool call | Grants checked at call time, `consequential` tools gated by approval, model output never an authorization input, a dedicated Phase 11 review |
| The builder creates something harmful or expensive | Propose/apply, `requires_consent`, object caps, nothing published or enabled on apply, bounded by the caller's role at plan time |
| Beginners still find it too complex | Simple mode with a vocabulary lint, safe-by-default approvals, and Phase 8 before Phase 9 so the plain UI is tested first |
| The island becomes the source of truth | `interpret()` is pure and cannot fetch; `world/dispatch/` is the only layer that may call an API; the isolation test keeps the product working without Phaser; the layout tables hold no execution state |
| Secrets leak | Entities with no field for them, AAD tenant binding, declared `secret_fields`, four redaction channels, and the canary test |
| Scope expands until nothing ships | §7's right-hand column, and one reliable instance per category |
