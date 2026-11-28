# AgentWorld — what is built, and what is left

> **Measured, not remembered.** Every claim here was checked against the worktree on
> **2026-11-20**, at commit `ad4a189` plus the ledger reconciliation committed with this
> file. Where a plan document disagreed with the code, the code won — and on 2026-11-20
> the disagreements were written back into the plan: the eight decisions are P1-P8 in
> [`proposals/2026-11-ecosystem-builder/07-DECISION-RECORD.md`](proposals/2026-11-ecosystem-builder/07-DECISION-RECORD.md)
> §6.
>
> **This file has no date in its name on purpose.** Its predecessor was
> `STATUS-2026-11-17.md`, and seven documents linked to it by that name — so the moment it
> was re-measured, every one of those links pointed at a date that was no longer the
> status. The date lives in the line above instead, where updating it is the same edit as
> updating the content.
>
> This file is the status. The *reasoning* lives in
> [`proposals/2026-11-ecosystem-builder/`](proposals/2026-11-ecosystem-builder/README.md);
> the pre-pivot contract set, [`01`](01-ARCHITECTURE.md)–[`05`](05-ROADMAP.md), is history
> with banners saying so.

---

## 1. One-screen summary

| | | |
|---|---|---|
| **Phase 0** Foundations | ✅ complete | monorepo, compose stack, CI, green from a fresh clone |
| **Phase 1** Tenancy and auth | ✅ complete | Supabase JWT, workspaces, members, roles, API keys, RLS |
| **Phase 2** Core model and protocol | ✅ complete | 30 tables, 26 migrations, event sourcing, replay, outbox. 13 of 14 ledger criteria met, the 14th untestable until Phase 7 |
| **Phase 3** Execution core + island skeleton | 🟡 **Track A mostly done, Track B not started** | Phase 3 is T0–T8 + T13. **30 of 38 ledger boxes ticked**: the 13 of the 2026-11-20 reconciliation, R1-R14 with the realtime spine on 2026-11-26, and T13's three on 2026-11-28. Open: T7a's turn loop, T8's `on_failure = 'fail'`, all four island boxes, the `world/nodes` read API, and the live provider call — which T13 did not close and now covers two providers |
| **Phase 3a** Approvals, MCP tools, dispatchable agents | ⬜ not started | until it lands, consequential actions are refused at publish rather than gated |
| **Phase 4** Vertical slice → M1 | 🟡 **partly built early** | multi-step, delegation, `agent.test` and `execution.cancel` all exist; its real remainder is the island's I2 and the builder prototype |
| **Phases 5–11** | ⬜ not started | real workflows, approvals/tools/integrations, external agents, full workspace, full island, complete builder, hardening |

**The headline, and it has moved since 2026-11-17.** The backend can create, configure,
run, cancel, test and review agents and workflows. **The browser can now do all of that
too** — an agent can be created, edited, archived, restored, tested against a real model
and watched through its executions, and a workspace's credentials and model profiles can
be managed, without `curl`. What the browser still cannot do is see anything change
without a reload: there is no realtime feed, and there is no island.

---

## 2. What is done

### 2.1 Backend — 71 routes, twelve `v1` routers plus health

| Area | Routes | Notes |
|---|---|---|
| Identity | 2 | Supabase Auth client-side; the first `GET /v1/me` materialises the user and reports `needs_onboarding` |
| Workspaces and members | 9 | roles `viewer`→`owner`, one owner per workspace by partial unique index, promotion is a transfer in one transaction |
| Projects | 5 | |
| API keys | 4 | hashed, scoped (`ingest`, `full`), revoke and delete |
| Credentials | 5 | envelope encryption, AAD bound to workspace **and** credential id, rotate, revoke |
| Agents | 15 | list, create, read, patch, archive, restore, `test`, status history, runs, executions; model profiles CRUD |
| Workflows | 14 | connections and grants, workflows, versions, draft `GET`/`PUT`, `validate`, `publish`, `restore`, archive |
| Executions | 7 | run, list, read, cancel, and the step tree |
| Dispatch review | 2 | the queue at `viewer`, `resolve` at `admin` under `dispatch.resolve_review` |
| Ingest | 4 | events (partial success), heartbeat, logs, metrics |
| Self and meta | 2 | |
| Health | 2 | |

**Orchestration.** `advance()` is a pure decision function — no clock, no I/O, asserted
from its own syntax tree — and `services/executions.py` applies its decisions as one
transaction: event, state, outbox row. Dispatch rows are written from exactly one file,
enforced by a containment allow-list split by verb.

**Workers.** `agentworld-worker` and `agentworld-reaper` are real processes with `--once`
and continuous modes, outside the API lifespan. Claiming is `FOR UPDATE SKIP LOCKED` plus
a lease. `side_effect_started_at` is written **before** the outbound call, so an expired
lease can tell "never sent" from "might have landed": the first is retried, the second
becomes `needs_review` and waits for a human.

**Outbound security.** One guarded HTTP client: resolve, validate every resolved address,
pin the connection to a validated one, no redirects followed. Both IP families, the
metadata addresses and names, NAT64 and the short/hex/octal loopback forms are all
refused. `httpx` is confined to `infra/net/` by an import contract with one named Supabase
exemption.

**Event sourcing.** Append-only `events` with per-project `seq`, dedup keys, partitioning,
reducers with out-of-order tolerance, and the two properties that keep it honest:
refolding the stored log reproduces the state, and folding it creates **zero** dispatch
rows.

**Tests and enforcement.** 48 integration and 27 unit test files, 1,246 test functions;
8 import-linter contracts; schema and enum drift tests against a freshly migrated
database; an exhaustive role matrix with a non-vacuity guard; migrations applied from
scratch in CI. Three CI jobs — `api`, `web`, `compose` — and the compose job asserts the
application role cannot bypass RLS.

### 2.2 Web app — 12 pages

| Page | What it does |
|---|---|
| `/` | the one page that handles every session state: setup instructions, sign-in, a refused account, an API that is down, or the workspace |
| `/login` | Supabase sign-in, deep-link preservation, the rejected-token recovery path |
| `/auth/callback`, `/auth/signout` | code exchange; sign-out at the library's global scope, answering 303 |
| `/onboarding` | create the first workspace and project |
| `/w/{ws}` | project list |
| `/w/{ws}/keys` | mint and revoke API keys |
| `/w/{ws}/settings/credentials` | provider credentials: add, rotate, revoke, delete |
| `/w/{ws}/settings/model-profiles` | model profiles: create, re-point, delete |
| `/w/{ws}/p/{p}/agents` | the project's agents, archived included on request |
| `/w/{ws}/p/{p}/agents/new` | create an agent |
| `/w/{ws}/p/{p}/agents/{id}` | one agent: status, heartbeat, **a test panel**, executions, details, archive and restore |
| `/w/{ws}/p/{p}/agents/{id}/edit` | edit an agent |
| `/w/{ws}/p/{p}/executions/{id}` | one execution and what its steps did |

Typed clients for tenancy, agents, credentials, model profiles and executions; a
problem-details-aware fetch wrapper; middleware that forwards the real return path so a
deep link survives sign-in; **577 tests across 27 files** with `tsc --noEmit` and
`eslint .` clean.

### 2.3 Infrastructure

Docker compose stack with Postgres, a health-gated startup, an application role that
cannot bypass RLS, Alembic migrations with a from-scratch CI check, and runner tasks for
every loop a developer needs (`npm run dev`, `test`, `lint`, `typecheck`, `verify`,
`api:worker`, `api:reaper`, `infra:up`).

---

## 3. What is not done

### 3.1 Phase 3, Track A — two items

> **Re-characterised 2026-11-20**; the first item closed on **2026-11-26** and the second
> on **2026-11-28**. The edition before that listed four items as though they were
> comparable. They are not: one was a week of work with no code and, until 2026-11-20, no
> acceptance criteria either; one is two days; and T8 turned out to be seven-ninths done
> once it was measured against its own criteria. Sizes below are the point of the list.

1. ~~**The event bus and the WebSocket gateway.**~~ **Done, 2026-11-26.**
   `infra/event_bus.py` holds `InProcessEventBus` and `RedisEventBus`; `api/ws/` holds the
   gateway, the frame builder and the publisher; `POST /v1/projects/{id}/ws-ticket` mints
   a 60-second single-use ticket at `viewer` through the new
   `Action.REALTIME_SUBSCRIBE`. **R1-R14 all pass**, each with a named test, including R13
   on two `uvicorn` processes over the `redis` bus (P4) - which the `api` CI job now
   starts Redis for. The four questions the approved design left open - where the
   publisher runs (P10.5), R11's operational replay window, where R13 runs in CI, and the
   snapshot's isolation level - are answered in
   `docs/proposals/2026-11-ecosystem-builder/21-M5-REALTIME-SPINE.md`. **It does not close
   the phase**: the two items below and steps 7-9 remain.
2. ~~**The OpenAI-compatible provider (T13).**~~ **Done, 2026-11-28.**
   `infra/providers/` holds `base.py`, `anthropic.py`, `openai_compatible.py` and
   `registry.py`, and `registry.available()` serves `anthropic` and `openai`. The shared
   conformance suite exists: `tests/unit/test_provider_conformance.py`, parametrised over
   `provider_cases.CASES`, extracted from `test_providers.py` first so the second adapter
   could arrive as a case rather than as a file of new tests — which is what it did, in
   one new adapter plus one registry entry. Each provider's wire shape stays beside it.
   **T13's three ledger boxes are ticked; the live call is not one of them.** Nothing has
   reached a real OpenAI-compatible server, and a self-hosted one is unreachable in
   deployment while `EgressPolicy()` blocks loopback and RFC 1918 with nothing configuring
   otherwise (`workers/worker.py` says so itself).
3. **T7a, the managed-agent turn loop — in scope and untouched.** One model call per step
   is what exists; `services/dispatch.py` says so itself. No `max_turns`, no wall clock,
   and no token counts or durations reaching `metric_samples` — though `metric.recorded`
   and the table both exist, so that half is emission, not plumbing. **T7's three tool
   criteria are no longer here:** they became T7b in Phase 3a with T10 (P2), because a
   `tool_call` step cannot be published until a `tools` table exists.
4. **T8 — seven of nine met; what is left is `on_failure = 'fail'`.** Retries, the
   classification gate, bounded jittered backoff, uncertain outcomes and the review queue
   are all built and all now ticked in `08-PHASE-3-READINESS.md`, each naming its test.
   Two criteria were amended there rather than implemented — the human-retry idempotency
   token (P1) and the consequential `max_attempts` default (P7) — and the step-timeout
   criterion moved to T11 (P5). The genuine gap is `workflow_steps.on_failure`: a column
   with a check constraint and no reader since T0. `continue` and `route` are Phase 5's
   (P6).

**A fifth item the earlier list omitted.** `08-PHASE-3-READINESS.md`'s T6 criterion *"a
worker that needs longer extends its own lease rather than taking a long one"* is open:
there is no `extend` or `renew` on a dispatch claim anywhere in `services/dispatch.py` or
`repositories/dispatches.py`. Small, and it belongs to a task otherwise closed, which is
exactly how it went unlisted.

### 3.2 Phase 3, Track B — nothing built

`apps/web/world/` holds a README. Phaser and Zustand are in no `package.json`. The
`world_nodes`, `world_routes`, `world_districts` and `world_layouts` tables exist with no
endpoints in front of them.

**What has changed here since 2026-11-17 is the "minimal pages" half, which is now
closed.** The agent form, the edit page and the execution detail page all exist, so the
sentence that used to stand here — an agent cannot be created or configured without
`curl` — is no longer true.

### 3.3 Everything after

Phase 3a (approvals, MCP tool execution, dispatchable agents), the rest of Phase 4, and
Phases 5–11. Nothing in them has started.

---

## 4. Open gaps and risks

**Reliability**

- **Stalled executions.** Settling or resolving a dispatch commits in one transaction;
  `advance_once` runs in a second, because it decides from committed state. A process that
  dies between them leaves an execution complete, consistent and **stalled**. T6c narrows
  this to "somebody comes back within the sweep interval plus the grace period"; it is not
  closed.
- **`needs_review` is terminal without a human.** A lease that expires mid-flight is never
  retried automatically — by design, because nobody knows whether the effect landed.
  `POST .../dispatches/{id}/resolve` is the only way out, at `admin`, with a mandatory
  note.

**Capability**

- `tool_call` steps cannot be published: the check constraint needs a `tool_id` and there
  is no `tools` table until Phase 6. `advance()`'s tool_call branch is tested and
  unreachable from a published workflow.
- Consequential external actions are **refused at publish**, not gated, until Phase 3a
  replaces the refusal with an approval.

**Security, stated honestly**

- Prompt injection choosing an *allowed* destination is not solved. The guarded client
  stops an agent reaching an internal address; it does not stop a malicious document
  telling an agent to POST to an attacker's public URL. The mitigations are a **gate on
  Phase 6**, including an adversarial test that asserts the exfiltration *succeeds*, so
  the limitation lives in CI rather than in memory.

**Web**

- **No `.tsx` file is under test.** Vitest runs in `node` with no jsdom, so page markup is
  covered by live probes and comments only. Anything whose correctness must be *asserted*
  belongs in a pure `.ts` module — which is why the redirect decisions live in
  `lib/auth/destination.ts` and the agent-test panel's decisions in
  `lib/agents/test-panel.ts`. **This is a standing decision, not a defect to fix in
  passing:** adding jsdom and a Testing Library is a dependency choice with its own cost,
  and it has been declined each time it came up.
- The agent test panel spends real money on a real provider, and its duplicate-submit
  protection is a convenience rather than a guarantee: `POST .../agents/{id}/test` has no
  idempotency key and no rate limit, so two requests are two executions and two charges.

**Closed on 2026-11-19**, and listed only so that a reader of the previous edition does not
go looking: the 403 reported as `api-unreachable`, the raw `"1 field(s) failed validation"`
on a malformed id, the client-side copy of the liveness rule, the untested `outcomeOf`
agent branch, the unasserted bearer token, the `{}` passed as `nextConfig`, and the stale
`.pill--warn` comment. Each now has a test that fails if it comes back.

---

## 5. The roadmap from here

### Next steps, in order

> **Renumbered 2026-11-20** to match the ten steps in §6, which is the sequence that
> actually closes the phase. The previous edition's N1 and N2 — the agent pages and the
> execution detail page — are done; its ledger reconciliation step is now step 10, and
> the three island boxes it omitted are step 8.

| | Step | Size | Done when |
|---|---|---|---|
| **1** | Settle the realtime architecture decisions | done — P3-P8 | the seven questions the gateway cannot be written without are answered and recorded |
| **2** | Write the gateway's acceptance criteria | done — R1-R14 | the largest open item in the phase can be ticked at all |
| **3** | The realtime spine: event bus, `ws-ticket`, gateway | done — 2026-11-26 | R1-R14 pass, including R13 on two processes over the `redis` bus |
| **4** | T13: extract the conformance suite, add the OpenAI-compatible provider | done — 2026-11-28 | its three boxes are ticked, each naming its test: one parametrised suite over both, no import outside `infra/providers/`, and one published workflow run through both. **The live call is step 9's, not this step's** |
| **5** | T7a: the turn loop, `max_turns`, the wall clock, metric emission | ~1 week | a step takes several turns under a budget and a wall clock, and token counts reach `metric_samples` through `metric.recorded` |
| **6** | T8: `on_failure = 'fail'` | ~½ session | a dead dispatch applies `fail`, asserted; `continue` and `route` are Phase 5's |
| **7** | The `world/nodes` read API | ~1 session | placements and the layout are readable at `viewer`, and `test_world_separation.py` still passes |
| **8** | Island skeleton I1 | ~1 week | the four island boxes pass: real agents from `world_nodes`, status equal to the REST view and arriving as a delta, `offline` shuttered, and `world/phaser/` deletable with both suites green. **Needed 3, which is done; still needs 7** |
| **9** | The live provider smoke run | minutes, once a key exists | `scripts/live_provider_smoke.py` passes against the real API. Needs an `ANTHROPIC_API_KEY`, not code. **Two providers now, and the script covers one**: it builds `anthropic.SLUG`, so a live call through the OpenAI-compatible adapter needs a key *and* a few lines. A self-hosted target needs more than a key — `EgressPolicy()` blocks loopback and RFC 1918 and has no setting wired to allow them |
| **10** | Reconcile the ledger and declare Phase 3 closed | ~½ session | all 38 boxes are ticked or recorded as wrong, with a named test each |

**Why this order.** The spine comes before the island because the island's statuses are
specified to come from live deltas, and building the island first builds that feed twice.
T13 is independent of everything. Step 9 needs a person with a key and can happen at any
point. **The spine alone does not close the phase** — steps 3 and 4 still leave the four
island boxes and the live call open, which is what the previous edition of §6 got wrong.
Step 3 landing on 2026-11-26 is that sentence being right rather than wrong: the largest
open item in the phase is closed and six steps are still open behind it.

### After that

| Phase | What it adds | Watch for |
|---|---|---|
| **3a** | approvals, MCP tool execution, dispatchable agents | replaces the publish-time refusal of consequential work with a gate; `RequestApproval` is the decision `advance()` deliberately does not yet emit |
| **4 → M1** | the vertical slice: a described ecosystem proposed, created, run and watched | most of its Track A exists; the remainder is the island's I2 and the builder prototype, and I2 needs I1 |
| **5** | real workflows: branches, joins, retries at policy | |
| **6 → M2** | approvals, tools, integrations | the injection mitigations are a gate on this phase, not a later audit |
| **7 → M3** | external agents and triggers | `DispatchKind.agent_task_offer` and the claim/poll shape already exist |
| **8** | the full workspace | |
| **9 → M4** | the full island | the gate: can the island be deleted without one backend test changing? |
| **10 → M5** | the complete builder — **the MVP** | |
| **11 → M6** | hardening and launch | |

### Three questions still open from the proposal

| | Question | Default | Needed by |
|---|---|---|---|
| O1 | First real integration: Slack or Gmail? | Slack | Phase 6 |
| O2 | MCP as the primary tool mechanism? | Yes | Phase 6 |
| O3 | Polish the plain workspace before the island? | Yes | Phase 8 |

---

## 6. What closes Phase 3, and what does not

Phase 3 is T0–T8 and T13. Its ledger carries **38 boxes** after the 2026-11-20
reconciliation — the original 17, plus R1-R14 for the gateway, six for T7a/T8/T13, and
one tightening the island's status claim. **30 are ticked**: 13 at that
reconciliation, R1-R14 on 2026-11-26 when the realtime spine landed, and T13's three on
2026-11-28 when the second provider did.

**Closing the phase means closing the 8 that remain**, and four of them are Track B. The previous edition of this section said
*"to declare Phase 3 complete: N2 → N3 → N4, then N1"*, which was wrong: completing all
four of those leaves the island boxes untouched, so the ledger still could not be ticked.
The sequence in §5 is the corrected one.

**The spine is necessary and not sufficient**, and 2026-11-26 is where that sentence
earned itself: the largest open item in the phase closed and eleven boxes were still open
behind it. Step 4 closed T13's three on 2026-11-28 and left eight; steps 5 and 6 leave T7a
and T8, step 8 - the island - needed step 3 and still needs step 7's `world/nodes` read
API, and step 9 needs a person with an API key.

**No criterion carries into Phase 4 (P8).** Four of Phase 4's own acceptance criteria
depend on live deltas — *"every status change arriving over the WebSocket from a real
folded event"*, the applied objects appearing *"without a reload"*, and both status
transitions being *"visible live"*. Carrying the spine forward would make Phase 4
unclosable too, so Phase 4 begins when Phase 3's ledger is closed and not before.
